#!/usr/bin/env python3
"""Web panel for a vanilla Terraria dedicated server.

Runs TerrariaServer inside a pseudo-terminal (so it behaves exactly like it
does in tmux), streams its console to the browser and edits serverconfig.txt.
Standard library only, so it runs on the stock python image with no build.

DATA_DIR (/data in the container) layout:
    server/             Linux server files (TerrariaServer.bin.x86_64 ...)
    worlds/             .wld world files
    serverconfig.txt    created on first start if missing
    panel.py            this file
"""

import base64
import codecs
import collections
import fcntl
import glob
import hmac
import json
import os
import pty
import queue
import re
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

DATA_DIR = os.environ.get("DATA_DIR", "/data")
WORLDS_DIR = os.path.join(DATA_DIR, "worlds")
CONFIG_PATH = os.path.join(DATA_DIR, "serverconfig.txt")
BINARY = os.environ.get("TERRARIA_BINARY", os.path.join(DATA_DIR, "server", "TerrariaServer.bin.x86_64"))
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8080"))
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
AUTOSTART = os.environ.get("AUTOSTART", "1") != "0"
GAME_PORT = "7777"  # fixed inside the container; change the published port in CasaOS instead

HISTORY_CHARS = 200_000
STOP_TIMEOUT = 60
RESTART_DELAY = 10
CRASH_LIMIT = 5  # stop auto-restarting after this many crashes...
CRASH_WINDOW = 300  # ...within this many seconds

ESCAPE_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|[()][0-9A-Za-z]|[=>78cM])")
CLEAR_RE = re.compile(r"\x1b\[[0-9;]*2J|\x1bc")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
JOIN_RE = re.compile(r"^(.{1,20}) has joined\.$")  # player names are at most 20 characters
LEAVE_RE = re.compile(r"^(.{1,20}) has left\.$")


# Config file ------------------------------------------------------------------

SPECIAL_SEEDS = (  # seed_<name>=1 turns one on for autocreate; skyblock needs 1.4.5+
    "celebration", "drunk", "fortheworthy", "notthebees", "theconstant",
    "notraps", "remix", "zenith", "skyblock",
)
JOURNEY_POWERS = (  # journeypermission_<power>: 0 locked, 1 host only, 2 everyone
    "time_setfrozen", "time_setdawn", "time_setnoon", "time_setdusk", "time_setmidnight",
    "time_setspeed", "godmode", "increaseplacementrange", "setdifficulty", "setspawnrate",
    "biomespread_setfrozen", "rain_setstrength", "rain_setfrozen", "wind_setstrength", "wind_setfrozen",
)
LANGUAGES = ("en-US", "de-DE", "it-IT", "fr-FR", "es-ES", "ru-RU", "zh-Hans", "zh-Hant", "pt-BR", "pl-PL", "ja-JP", "ko-KR")
NUMBER_SETTINGS = {  # key: (label, min, max)
    "maxplayers": ("Max players", 1, 255),
    "npcstream": ("NPC stream", 0, 60),
    "priority": ("Priority", 0, 5),
    "worldrollbackstokeep": ("World backups", 0, 99),
}


def read_config_text():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def parse_config(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip()
    return values


def update_config(changes, text=None):
    """Apply {key: value} to the config, keeping comments and unknown keys.

    A value of None removes the key. port and worldpath are always enforced so
    the server keeps matching the container's port mapping and volume.
    """
    changes = {**changes, "port": GAME_PORT, "worldpath": WORLDS_DIR}
    lines, seen = [], set()
    for line in (read_config_text() if text is None else text).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip().lower()
            if key in changes:
                if key not in seen and changes[key] is not None:
                    lines.append(f"{key}={changes[key]}")
                seen.add(key)
                continue
        lines.append(line)
    lines += [f"{k}={v}" for k, v in changes.items() if k not in seen and v is not None]
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, CONFIG_PATH)


def list_worlds():
    worlds = []
    for path in sorted(glob.glob(os.path.join(WORLDS_DIR, "*.wld"))):
        st = os.stat(path)
        worlds.append({
            "name": os.path.basename(path)[:-4],
            "path": path,
            "size": st.st_size,
            "modified": int(st.st_mtime),
        })
    return worlds


def ensure_config():
    """Write a starter config (first world found, or a new medium world)."""
    os.makedirs(WORLDS_DIR, exist_ok=True)
    if os.path.exists(CONFIG_PATH):
        return
    worlds = list_worlds()
    name = worlds[0]["name"] if worlds else "World"
    update_config({
        "world": os.path.join(WORLDS_DIR, name + ".wld"),
        "autocreate": "2",
        "worldname": name,
        "difficulty": "0",
        "maxplayers": "8",
        "password": "",
        "motd": "",
    }, text="")


def one_line(value):
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def save_config(body):
    """Validate the settings form (or raw file) from the browser and write it."""
    if "raw" in body:
        update_config({}, text=str(body["raw"]))
        return None

    changes = {}
    for key, (label, low, high) in NUMBER_SETTINGS.items():
        try:
            value = int(body.get(key))
        except (TypeError, ValueError):
            return f"{label} must be a number"
        if not low <= value <= high:
            return f"{label} must be between {low} and {high}"
        changes[key] = str(value)

    language = body.get("language")
    if language not in LANGUAGES:
        return "Pick a language"

    perms = body.get("journey") if isinstance(body.get("journey"), dict) else {}
    for power in JOURNEY_POWERS:
        value = str(perms.get(power, 2))
        if value not in ("0", "1", "2"):
            return "Journey permissions must be 0, 1 or 2"
        changes["journeypermission_" + power] = value

    changes.update(
        password=one_line(body.get("password")),
        motd=one_line(body.get("motd")),
        language=language,
        secure="1" if body.get("secure") else "0",
        slowliquids="1" if body.get("slowliquids") else "0",
    )
    update_config(changes)
    return None


def set_world(body):
    """Point the config at an existing world, or at a new one to generate."""
    new = body.get("new_world")
    if not isinstance(new, dict):
        world = body.get("world")
        allowed = {w["path"] for w in list_worlds()}
        allowed.add(parse_config(read_config_text()).get("world"))
        if not isinstance(world, str) or not world or world not in allowed:
            return "Pick a world"
        update_config({"world": world})
        return None

    name = one_line(new.get("name"))
    filename = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    if not filename:
        return "Give the new world a name"
    path = os.path.join(WORLDS_DIR, filename + ".wld")
    if os.path.exists(path):
        return f"A world file called {filename}.wld already exists"
    size, difficulty = str(new.get("size", "2")), str(new.get("difficulty", "0"))
    if size not in ("1", "2", "3") or difficulty not in ("0", "1", "2", "3"):
        return "Invalid world size or difficulty"
    specials = new.get("special_seeds") or []
    if not isinstance(specials, list) or any(s not in SPECIAL_SEEDS for s in specials):
        return "Unknown special seed"

    # Clear special seeds left over from the last world so they don't carry over.
    changes = {f"seed_{s}": "1" if s in specials else None for s in SPECIAL_SEEDS}
    changes.update(
        world=path,
        worldname=name,
        autocreate=size,
        difficulty=difficulty,
        seed=one_line(new.get("seed")) or None,
    )
    update_config(changes)
    return None


def config_payload():
    text = read_config_text()
    return {"values": parse_config(text), "raw": text, "worlds": list_worlds()}


def clean_output(raw):
    """Strip terminal control codes. Returns (text, unfinished escape to carry)."""
    carry = ""
    esc = raw.rfind("\x1b")
    if esc != -1 and len(raw) - esc < 32 and not ESCAPE_RE.match(raw, esc):
        raw, carry = raw[:esc], raw[esc:]
    text = CLEAR_RE.sub("\f", raw)  # the browser clears its console on \f
    text = ESCAPE_RE.sub("", text)
    text = text.replace("\r\n", "\n")
    return CONTROL_RE.sub("", text), carry


# Server process ---------------------------------------------------------------


class Server:
    def __init__(self):
        self.lock = threading.RLock()
        self.proc = None
        self.master = None
        self.exited = threading.Event()
        self.exited.set()
        self.state = "stopped"
        self.since = None
        self.wanted = False  # should be running; False means an exit is expected
        self.note = ""
        self.history = ""
        self.subscribers = set()
        self.players = []
        self.partial_line = ""
        self.crashes = collections.deque()

    # Output to browsers and `docker logs`

    def _broadcast(self, event, data):
        for q in self.subscribers:
            q.put((event, data))

    def emit(self, text):
        sys.stdout.write(text)
        sys.stdout.flush()
        with self.lock:
            self.history = (self.history + text)[-HISTORY_CHARS:]
            self._broadcast("out", text)
            self._track_lines(text)

    def log(self, message):
        lead = "" if not self.history or self.history.endswith("\n") else "\n"
        self.emit(f"{lead}[panel] {message}\n")

    def _track_lines(self, text):
        """Watch complete console lines for startup and player joins/leaves."""
        self.partial_line += text
        *complete, rest = self.partial_line.split("\n")
        self.partial_line = rest[-1000:]
        changed = False
        for line in complete:
            line = line.rstrip("\r").rsplit("\r", 1)[-1].replace("\f", "").strip()
            while line.startswith(":"):  # the server prints its ": " prompt in front of messages
                line = line[1:].strip()
            if line.startswith("<"):  # chat message, e.g. "<Bob> hello"
                continue
            if line == "Server started" and self.state == "starting":
                self.state = "running"
                changed = True
            elif m := JOIN_RE.match(line):
                if m[1] not in self.players:
                    self.players.append(m[1])
                    changed = True
            elif m := LEAVE_RE.match(line):
                if m[1] in self.players:
                    self.players.remove(m[1])
                    changed = True
        if changed:
            self._broadcast("status", self.status())

    def status(self):
        with self.lock:
            world = parse_config(read_config_text()).get("world", "")
            return {
                "state": self.state,
                "since": self.since,
                "players": list(self.players),
                "world": os.path.splitext(os.path.basename(world))[0] or None,
                "note": self.note,
                "installed": os.path.isfile(BINARY),
                "binary": BINARY,
            }

    def _set_state(self, state, note=None):
        with self.lock:
            self.state = state
            if note is not None:
                self.note = note
            self._broadcast("status", self.status())

    # Lifecycle

    def start(self):
        """Start the server. Returns an error message, or None."""
        with self.lock:
            if self.proc:
                return None
            if not os.path.isfile(BINARY):
                err = f"Server files not found at {BINARY}"
                self._set_state("stopped", note=err)
                return err
            ensure_config()
            os.chmod(BINARY, os.stat(BINARY).st_mode | 0o111)
            self.log("Starting server…")
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
            try:
                proc = subprocess.Popen(
                    [BINARY, "-config", CONFIG_PATH],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    cwd=os.path.dirname(BINARY),
                    env={**os.environ, "TERM": "xterm"},
                    start_new_session=True,
                )
            except OSError as e:
                os.close(master)
                err = f"Could not start the server: {e}"
                self.wanted = False
                self._set_state("stopped", note=err)
                return err
            finally:
                os.close(slave)
            self.proc, self.master = proc, master
            self.exited.clear()
            self.wanted = True
            self.players = []
            self.partial_line = ""
            self.since = time.time()
            self._set_state("starting", note="")
        threading.Thread(target=self._pump, args=(proc, master), daemon=True).start()
        return None

    def _pump(self, proc, master):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        while True:
            try:
                data = os.read(master, 4096)
            except OSError:  # EIO once the server has exited
                data = b""
            if not data:
                break
            text, pending = clean_output(pending + decoder.decode(data))
            if text:
                self.emit(text)
        code = proc.wait()
        with self.lock:
            os.close(master)
            self.proc = self.master = None
            self.players = []
            self.since = None
            crashed = self.wanted
            if not crashed:
                self._set_state("stopped")
        self.exited.set()
        if crashed:
            self._crashed(code)
        else:
            self.log(f"Server stopped (exit code {code}).")

    def _crashed(self, code):
        now = time.time()
        with self.lock:
            self.crashes.append(now)
            while now - self.crashes[0] > CRASH_WINDOW:
                self.crashes.popleft()
            if len(self.crashes) >= CRASH_LIMIT:
                self.wanted = False
                note = (f"The server crashed {CRASH_LIMIT} times in {CRASH_WINDOW // 60} minutes, "
                        "so auto-restart is paused. Check the console, then press Start.")
                self._set_state("crashed", note=note)
                self.log(note)
                return
            self._set_state("restarting")
        self.log(f"Server exited unexpectedly (exit code {code}). Restarting in {RESTART_DELAY}s…")
        time.sleep(RESTART_DELAY)
        with self.lock:
            if self.wanted and not self.proc:
                self.start()

    def _write(self, line):
        with self.lock:
            if self.master is None:
                return False
            try:
                os.write(self.master, line.encode() + b"\r")
            except OSError:
                return False
            return True

    def command(self, line):
        """Send one console command. Returns an error message, or None."""
        line = one_line(line)
        if not line:
            return "Type a command first"
        if line.split()[0].lower() in ("exit", "exit-nosave"):
            with self.lock:
                self.wanted = False
                if self.proc:
                    self._set_state("stopping")
        if not self._write(line):
            return "The server is not running"
        return None

    def stop(self):
        """Save and stop the server, escalating to signals if it hangs."""
        with self.lock:
            self.wanted = False
            proc = self.proc
            if proc is None:
                if self.state != "stopped":
                    self._set_state("stopped")
                return
            self._set_state("stopping")
        self.log("Saving the world and stopping…")
        self._write("exit")
        if self.exited.wait(STOP_TIMEOUT):
            return
        self.log("The server did not stop in time; terminating it.")
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass
            if self.exited.wait(10):
                return

    def restart(self):
        self.stop()
        self.start()


SERVER = Server()


# HTTP -------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "TerrariaPanel/1.0"

    def log_message(self, fmt, *args):
        pass

    def _authorized(self):
        if not PANEL_PASSWORD:
            return True
        scheme, _, encoded = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() == "basic":
            try:
                password = base64.b64decode(encoded).decode("utf-8").partition(":")[2]
            except (ValueError, UnicodeDecodeError):
                password = None
            if password is not None and hmac.compare_digest(password.encode(), PANEL_PASSWORD.encode()):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Terraria panel", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, data):
        self._send(code, json.dumps(data).encode(), "application/json")

    def _result(self, err):
        self._json(400, {"error": err}) if err else self._json(200, {"ok": True})

    def do_GET(self):
        if not self._authorized():
            return
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
        elif path == "/api/events":
            self._events()
        elif path == "/api/status":
            self._json(200, SERVER.status())
        elif path == "/api/config":
            self._json(200, config_payload())
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self):
        if not self._authorized():
            return
        if self.headers.get_content_type() != "application/json":
            return self._json(415, {"error": "Expected JSON"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "Invalid JSON"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "Invalid JSON"})

        path = urlsplit(self.path).path
        if path == "/api/command":
            self._result(SERVER.command(body.get("command")))
        elif path == "/api/start":
            self._result(SERVER.start())
        elif path in ("/api/stop", "/api/restart"):
            action = SERVER.stop if path == "/api/stop" else SERVER.restart
            threading.Thread(target=action, daemon=True).start()
            self._result(None)
        elif path == "/api/config":
            self._result(save_config(body))
        elif path == "/api/world":
            err = set_world(body)
            if not err:  # save the current world, then come back up on the chosen one
                threading.Thread(target=SERVER.restart, daemon=True).start()
            self._result(err)
        else:
            self._json(404, {"error": "Not found"})

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = queue.Queue()
        with SERVER.lock:
            SERVER.subscribers.add(q)
            history, status = SERVER.history, SERVER.status()
        try:
            self._sse("reset", history)
            self._sse("status", status)
            while True:
                try:
                    event, data = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse(event, data)
        except OSError:  # browser went away
            pass
        finally:
            with SERVER.lock:
                SERVER.subscribers.discard(q)

    def _sse(self, event, data):
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()


def main():
    os.makedirs(WORLDS_DIR, exist_ok=True)
    httpd = ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"[panel] Web panel listening on port {PANEL_PORT}", flush=True)
    if not PANEL_PASSWORD:
        print("[panel] PANEL_PASSWORD is empty, so the panel has no login", flush=True)

    if AUTOSTART:
        SERVER.start()

    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    while not stopping.wait(1):
        pass
    SERVER.stop()  # docker stop / CasaOS stop: save the world before exiting
    httpd.shutdown()


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terraria Server</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><rect width='16' height='16' rx='3' fill='%235a3d22'/><rect width='16' height='6' rx='3' fill='%237bc96f'/><rect y='3' width='16' height='3' fill='%237bc96f'/></svg>">
<style>
  :root {
    color-scheme: dark;
    --bg: #0e1013;
    --term: #0a0c0f;
    --panel: #161a20;
    --panel2: #1d222a;
    --line: #272e39;
    --line2: #374052;
    --text: #e7e9ee;
    --muted: #8a93a6;
    --accent: #7bc96f;
    --accent-ink: #0c1a0a;
    --warn: #e3b341;
    --danger: #f0605a;
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  [hidden] { display: none !important; }

  header { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 18px; padding: 14px 20px; background: var(--panel); }
  h1 { font-size: 16px; margin: 0; font-weight: 700; }
  .pill { display: inline-flex; align-items: center; gap: 7px; padding: 3px 11px 3px 9px; border-radius: 999px; background: var(--panel2); border: 1px solid var(--line); font-size: 12.5px; font-weight: 600; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .pill[data-state=running] .dot { background: var(--accent); box-shadow: 0 0 0 3px rgb(123 201 111 / .2); }
  .pill[data-state=starting] .dot, .pill[data-state=stopping] .dot, .pill[data-state=restarting] .dot { background: var(--warn); animation: pulse .9s infinite alternate; }
  .pill[data-state=crashed] .dot { background: var(--danger); }
  @keyframes pulse { to { opacity: .3; } }
  .meta { display: flex; flex-wrap: wrap; gap: 4px 16px; color: var(--muted); font-size: 13px; }
  .meta b { color: var(--text); font-weight: 600; }
  .spacer { flex: 1; }
  .actions { display: flex; gap: 8px; }

  button { font: inherit; font-weight: 600; color: var(--text); background: var(--panel2); border: 1px solid var(--line); border-radius: 8px; padding: 7px 13px; cursor: pointer; white-space: nowrap; }
  button:hover:not(:disabled) { border-color: var(--line2); background: #232934; }
  button:disabled { opacity: .4; cursor: default; }
  button.primary { background: var(--accent); color: var(--accent-ink); border-color: transparent; }
  button.primary:hover:not(:disabled) { background: #8bd47f; }
  button.danger { color: var(--danger); }
  button.armed, button.armed:hover:not(:disabled) { background: var(--danger); color: #fff; border-color: transparent; }
  button.sm { padding: 4px 9px; font-size: 12px; border-radius: 6px; }

  .tabs { display: flex; gap: 2px; padding: 0 14px; background: var(--panel); border-bottom: 1px solid var(--line); }
  .tab { background: none; border: 0; border-bottom: 2px solid transparent; border-radius: 0; padding: 9px 8px 10px; margin: 0 4px; color: var(--muted); }
  .tab:hover:not(:disabled) { background: none; color: var(--text); }
  .tab[aria-selected=true] { color: var(--text); border-bottom-color: var(--accent); }

  main { padding: 16px 20px 24px; max-width: 1440px; margin: 0 auto; }
  .banner { margin: 0 0 16px; padding: 10px 14px; border-radius: 10px; border: 1px solid rgb(227 179 65 / .4); background: rgb(227 179 65 / .08); color: #f3d991; }
  .banner.bad { border-color: rgb(240 96 90 / .45); background: rgb(240 96 90 / .08); color: #ffc0bc; }

  .grid { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 16px; align-items: start; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px; }
  .card h2 { margin: 0 0 10px; font-size: 11.5px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); display: flex; align-items: center; gap: 8px; }
  .card h2 .count { color: var(--text); letter-spacing: 0; }
  .card h2 button { margin-left: auto; text-transform: none; letter-spacing: 0; }

  .console-card { display: flex; flex-direction: column; height: calc(100vh - 160px); min-height: 380px; padding: 0; overflow: hidden; }
  #console { flex: 1; margin: 0; padding: 14px 16px; overflow: auto; background: var(--term); font: 13px/1.5 var(--mono); white-space: pre-wrap; overflow-wrap: anywhere; }
  .cmdline { display: flex; align-items: center; gap: 10px; padding: 8px 10px 8px 16px; border-top: 1px solid var(--line); background: var(--panel); }
  .cmdline .caret { color: var(--accent); font: 700 13px var(--mono); }
  .cmdline input { flex: 1; min-width: 0; padding: 6px 0; border: 0; outline: 0; background: transparent; color: var(--text); font: 13px var(--mono); }

  .side { display: flex; flex-direction: column; gap: 16px; }
  .btns { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
  .row { display: flex; gap: 6px; align-items: center; }
  .row input { flex: 1; min-width: 0; }
  ul.players { list-style: none; margin: 0; padding: 0; }
  ul.players li { display: flex; align-items: center; gap: 6px; padding: 7px 0; border-bottom: 1px solid var(--line); }
  ul.players li:last-child { border-bottom: 0; }
  ul.players .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; font-weight: 600; }
  .empty { margin: 0; color: var(--muted); font-size: 13px; }

  input[type=text], input[type=number], select, textarea { width: 100%; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg); color: var(--text); font: inherit; }
  input:focus-visible, select:focus-visible, textarea:focus-visible, button:focus-visible { outline: 2px solid rgb(123 201 111 / .55); outline-offset: 1px; }
  .cmdline input:focus-visible { outline: 0; }

  .settings { display: grid; gap: 16px; max-width: 760px; }
  .form { display: grid; gap: 16px; }
  .form > h2 { margin: 0; }
  .field { display: grid; gap: 6px; align-content: start; font-size: 13px; font-weight: 600; }
  .hint { color: var(--muted); font-size: 12.5px; font-weight: 400; }
  .two { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .check { display: flex; align-items: center; gap: 10px; font-size: 13px; font-weight: 600; cursor: pointer; }
  .check input { width: 16px; height: 16px; accent-color: var(--accent); }
  .checks { display: flex; flex-wrap: wrap; gap: 10px 24px; }
  .seeds { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px 14px; padding: 4px 0; }
  .perms { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px 14px; }

  ul.worlds { list-style: none; margin: 0; padding: 0; }
  ul.worlds li { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--line); }
  ul.worlds li:last-child { border-bottom: 0; }
  ul.worlds .info { flex: 1; min-width: 0; display: grid; gap: 2px; }
  ul.worlds .name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .badge { padding: 2px 9px; border-radius: 999px; background: rgb(123 201 111 / .14); color: var(--accent); font-size: 12px; font-weight: 600; }
  textarea { min-height: 280px; resize: vertical; font: 13px/1.55 var(--mono); }
  details summary { cursor: pointer; font-weight: 600; }
  details[open] summary { margin-bottom: 12px; }

  #toast { position: fixed; left: 50%; bottom: 20px; transform: translate(-50%, 8px); max-width: calc(100vw - 32px); padding: 10px 16px; border-radius: 10px; border: 1px solid var(--line2); background: var(--panel2); opacity: 0; pointer-events: none; transition: opacity .18s, transform .18s; }
  #toast.show { opacity: 1; transform: translate(-50%, 0); }
  #toast.err { border-color: var(--danger); color: #ffc0bc; }

  @media (max-width: 920px) {
    .grid { grid-template-columns: 1fr; }
    .console-card { height: 60vh; }
  }
  @media (max-width: 600px) {
    header { padding: 12px 16px; }
    main { padding: 12px 16px 20px; }
    .actions { width: 100%; }
    .actions button { flex: 1; }
    .two { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<header>
  <h1>Terraria</h1>
  <span class="pill" id="state" data-state="stopped"><span class="dot"></span><span id="stateText">Connecting…</span></span>
  <div class="meta">
    <span>World <b id="world">—</b></span>
    <span>Players <b id="playerCount">0</b></span>
    <span>Uptime <b id="uptime">—</b></span>
  </div>
  <div class="spacer"></div>
  <div class="actions">
    <button class="primary" id="startBtn" disabled>Start</button>
    <button id="restartBtn" data-confirm="Confirm restart" disabled>Restart</button>
    <button class="danger" id="stopBtn" data-confirm="Confirm stop" disabled>Stop</button>
  </div>
</header>
<div class="tabs" role="tablist">
  <button class="tab" role="tab" data-tab="console" aria-selected="true">Console</button>
  <button class="tab" role="tab" data-tab="worlds" aria-selected="false">Worlds</button>
  <button class="tab" role="tab" data-tab="settings" aria-selected="false">Settings</button>
</div>

<main>
  <div class="banner" id="banner" hidden></div>

  <section id="tab-console" class="grid">
    <div class="card console-card">
      <pre id="console"><span id="out"></span><span id="cur"></span></pre>
      <form class="cmdline" id="cmdForm">
        <span class="caret">&gt;</span>
        <input id="cmd" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Type a server command, e.g. help">
        <button class="sm">Send</button>
      </form>
    </div>

    <div class="side">
      <div class="card">
        <h2>Players <span class="count" id="playerCount2">0</span><button class="sm live" data-cmd="playing">Refresh</button></h2>
        <ul class="players" id="players"></ul>
        <p class="empty" id="noPlayers">Nobody online</p>
      </div>
      <div class="card">
        <h2>World</h2>
        <div class="btns">
          <button class="live" data-cmd="save">Save world</button>
          <button class="live" data-cmd="settle">Settle water</button>
          <button class="live" data-cmd="dawn">Dawn</button>
          <button class="live" data-cmd="noon">Noon</button>
          <button class="live" data-cmd="dusk">Dusk</button>
          <button class="live" data-cmd="midnight">Midnight</button>
          <button class="live" data-cmd="time">Show time</button>
          <button class="live" data-cmd="seed">Show seed</button>
        </div>
      </div>
      <div class="card">
        <h2>Broadcast</h2>
        <form class="row" data-prefix="say ">
          <input type="text" class="live" placeholder="Message to everyone" maxlength="200">
          <button class="live">Say</button>
        </form>
      </div>
      <div class="card">
        <h2>Message of the day</h2>
        <form class="row" data-prefix="motd ">
          <input type="text" class="live" placeholder="Shown when players join" maxlength="200">
          <button class="live">Set</button>
        </form>
      </div>
    </div>
  </section>

  <section id="tab-worlds" class="settings" hidden>
    <div class="card">
      <h2>Worlds</h2>
      <ul class="worlds" id="worldList"></ul>
      <p class="empty" id="noWorlds">No worlds yet. Create one below.</p>
    </div>

    <form class="card form" id="newWorldForm">
      <h2>Create a new world</h2>
      <div class="two">
        <label class="field">Name <input type="text" id="nwName" maxlength="64" placeholder="My World" required></label>
        <label class="field">Seed <input type="text" id="nwSeed" placeholder="Random"></label>
        <label class="field">Size
          <select id="nwSize"><option value="1">Small (4200 × 1200)</option><option value="2" selected>Medium (6400 × 1800)</option><option value="3">Large (8400 × 2400)</option></select>
        </label>
        <label class="field">Difficulty
          <select id="nwDiff"><option value="0">Classic</option><option value="1">Expert</option><option value="2">Master</option><option value="3">Journey</option></select>
        </label>
      </div>
      <div class="field">Special seeds
        <span class="hint">Tick one or more. They override the seed above.</span>
        <div class="seeds" id="nwSeeds">
          <label class="check"><input type="checkbox" value="drunk"> Drunk world</label>
          <label class="check"><input type="checkbox" value="fortheworthy"> For the Worthy</label>
          <label class="check"><input type="checkbox" value="celebration"> Celebration Mk10</label>
          <label class="check"><input type="checkbox" value="notthebees"> Not the Bees</label>
          <label class="check"><input type="checkbox" value="theconstant"> The Constant</label>
          <label class="check"><input type="checkbox" value="notraps"> No Traps</label>
          <label class="check"><input type="checkbox" value="remix"> Don't Dig Up</label>
          <label class="check"><input type="checkbox" value="zenith"> Zenith (Get fixed boi)</label>
          <label class="check"><input type="checkbox" value="skyblock"> Skyblock (1.4.5+)</label>
        </div>
        <span class="hint">Corruption or Crimson is picked at random. The server has no setting for it.</span>
      </div>
      <div class="row">
        <button class="primary" id="createBtn" data-restarts="Confirm restart">Create world</button>
        <span class="hint">The server saves the current world, restarts and generates this one. Large worlds take a few minutes.</span>
      </div>
    </form>
  </section>

  <section id="tab-settings" class="settings" hidden>
    <form class="settings" id="settingsForm">
      <div class="card form">
        <h2>Server</h2>
        <div class="two">
          <label class="field">Max players <input type="number" id="maxplayers" min="1" max="255" required></label>
          <label class="field">Password <input type="text" id="password" autocomplete="off" placeholder="None"></label>
        </div>
        <label class="field">Message of the day <input type="text" id="motd" placeholder="None"></label>
        <label class="field">Language
          <select id="language">
            <option value="en-US">English</option><option value="de-DE">Deutsch</option><option value="it-IT">Italiano</option>
            <option value="fr-FR">Français</option><option value="es-ES">Español</option><option value="ru-RU">Русский</option>
            <option value="zh-Hans">简体中文</option><option value="zh-Hant">繁體中文</option><option value="pt-BR">Português (Brasil)</option>
            <option value="pl-PL">Polski</option><option value="ja-JP">日本語</option><option value="ko-KR">한국어</option>
          </select>
          <span class="hint">The panel reads the English console to track status and players, so those stop updating in other languages.</span>
        </label>
      </div>

      <div class="card form">
        <h2>Performance and protection</h2>
        <div class="two">
          <label class="field">NPC stream <input type="number" id="npcstream" min="0" max="60" required>
            <span class="hint">Lower means less enemy skipping but more bandwidth. 0 is off, 60 is the default.</span>
          </label>
          <label class="field">Process priority
            <select id="priority">
              <option value="0">Realtime</option><option value="1">High (default)</option><option value="2">Above normal</option>
              <option value="3">Normal</option><option value="4">Below normal</option><option value="5">Idle</option>
            </select>
          </label>
          <label class="field">World backups to keep <input type="number" id="rollbacks" min="0" max="99" required>
            <span class="hint">Rolling copies of the world file. 2 is the default.</span>
          </label>
        </div>
        <div class="checks">
          <label class="check"><input type="checkbox" id="secure"> Anti-cheat protection</label>
          <label class="check"><input type="checkbox" id="slowliquids"> Slow liquids <span class="hint">less lag, slower to settle</span></label>
        </div>
      </div>

      <details class="card">
        <summary>Journey mode permissions</summary>
        <div class="form">
          <span class="hint">Who can use each Journey power. Only matters in Journey worlds.</span>
          <label class="field" style="max-width: 220px">Set all
            <select id="jpAll"><option value="">Choose…</option><option value="0">Locked</option><option value="1">Host only</option><option value="2">Everyone</option></select>
          </label>
          <div class="perms" id="perms"></div>
        </div>
      </details>

      <div class="row">
        <button class="primary">Save settings</button>
        <span class="hint" id="saveMsg"></span>
      </div>
    </form>

    <details class="card">
      <summary>Edit serverconfig.txt directly</summary>
      <p class="hint" style="margin-top:0">The panel always sets <code>port</code> and <code>worldpath</code> so they keep matching the container.</p>
      <textarea id="raw" spellcheck="false"></textarea>
      <div class="row" style="margin-top:10px"><button id="saveRaw">Save file</button></div>
    </details>
  </section>
</main>
<div id="toast" role="status"></div>

<script>
const $ = (s) => document.querySelector(s);
const LABELS = { running: 'Running', starting: 'Starting…', stopping: 'Stopping…', restarting: 'Restarting…', stopped: 'Stopped', crashed: 'Crashed' };
let status = null;
let connected = false;

function toast(msg, isError) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'show' + (isError ? ' err' : '');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.className = ''; }, isError ? 5000 : 2200);
}

async function api(path, body) {
  const opts = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

const send = (command) => api('api/command', { command }).catch((e) => toast(e.message, true));

// Buttons with data-confirm need a second click within 3 seconds.
function confirmed(btn) {
  if (!btn.dataset.confirm || btn.classList.contains('armed')) {
    clearTimeout(btn.disarm);
    if (btn.dataset.label) btn.textContent = btn.dataset.label;
    btn.classList.remove('armed');
    return true;
  }
  btn.dataset.label = btn.textContent;
  btn.textContent = btn.dataset.confirm;
  btn.classList.add('armed');
  btn.disarm = setTimeout(() => { btn.textContent = btn.dataset.label; btn.classList.remove('armed'); }, 3000);
  return false;
}

function guarded(btn, action) {
  btn.addEventListener('click', () => { if (confirmed(btn)) action(); });
}

// Buttons with data-restarts only need confirming while the server is up.
function syncRestartGuards() {
  const live = status && ['running', 'starting'].includes(status.state);
  document.querySelectorAll('[data-restarts]').forEach((btn) => {
    if (live) btn.dataset.confirm = btn.dataset.restarts;
    else delete btn.dataset.confirm;
  });
}

// ---- Console -------------------------------------------------------------
const consoleEl = $('#console'), outEl = $('#out'), curEl = $('#cur');
const MAX_CHARS = 250000;
let cur = '', cr = false, outChars = 0;

function feed(text) {
  const atBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 60;
  let done = '';
  for (const ch of text) {
    if (ch === '\n') { done += cur + '\n'; cur = ''; cr = false; }
    else if (ch === '\r') { cr = true; }
    else if (ch === '\f') { done = ''; outEl.textContent = ''; outChars = 0; cur = ''; cr = false; }
    else { if (cr) { cur = ''; cr = false; } cur += ch; }
  }
  if (done) {
    outEl.appendChild(document.createTextNode(done));
    outChars += done.length;
    while (outChars > MAX_CHARS && outEl.firstChild) {
      outChars -= outEl.firstChild.length;
      outEl.removeChild(outEl.firstChild);
    }
  }
  curEl.textContent = cur;
  if (atBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
}

function resetConsole(text) {
  outEl.textContent = ''; curEl.textContent = '';
  outChars = 0; cur = ''; cr = false;
  feed(text);
  consoleEl.scrollTop = consoleEl.scrollHeight;
}

const cmdHistory = [];
let histPos = 0;
$('#cmdForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const input = $('#cmd');
  const command = input.value.trim();
  if (!command) return;
  if (cmdHistory[cmdHistory.length - 1] !== command) cmdHistory.push(command);
  histPos = cmdHistory.length;
  input.value = '';
  consoleEl.scrollTop = consoleEl.scrollHeight;
  send(command);
});
$('#cmd').addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
  e.preventDefault();
  histPos = Math.max(0, Math.min(cmdHistory.length, histPos + (e.key === 'ArrowUp' ? -1 : 1)));
  e.target.value = cmdHistory[histPos] || '';
});

document.querySelectorAll('[data-cmd]').forEach((btn) => btn.addEventListener('click', () => send(btn.dataset.cmd)));
document.querySelectorAll('form[data-prefix]').forEach((form) => form.addEventListener('submit', (e) => {
  e.preventDefault();
  const input = form.querySelector('input');
  if (!input.value.trim()) return;
  send(form.dataset.prefix + input.value.trim());
  input.value = '';
}));

// ---- Status ----------------------------------------------------------------
function fmtDuration(sec) {
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`;
  return `${m}m ${String(s).padStart(2, '0')}s`;
}

function tick() {
  const live = status && status.since && ['running', 'starting'].includes(status.state);
  $('#uptime').textContent = live ? fmtDuration(Date.now() / 1000 - status.since) : '—';
}
setInterval(tick, 1000);

function renderBanner() {
  const b = $('#banner');
  let msg = '', bad = false;
  if (!connected) { msg = 'Lost connection to the panel. Reconnecting…'; bad = true; }
  else if (status && !status.installed) { msg = `Server files not found at ${status.binary}. Copy the Linux server files into the server folder, then press Start.`; bad = true; }
  else if (status && status.note) { msg = status.note; bad = status.state === 'crashed'; }
  b.textContent = msg;
  b.hidden = !msg;
  b.classList.toggle('bad', bad);
}

function renderPlayers(players) {
  const ul = $('#players');
  ul.textContent = '';
  for (const name of players) {
    const li = document.createElement('li');
    const label = document.createElement('span');
    label.className = 'name';
    label.textContent = name;
    const kick = document.createElement('button');
    kick.className = 'sm';
    kick.textContent = 'Kick';
    kick.dataset.confirm = 'Kick?';
    guarded(kick, () => send('kick ' + name));
    const ban = document.createElement('button');
    ban.className = 'sm danger';
    ban.textContent = 'Ban';
    ban.dataset.confirm = 'Ban?';
    guarded(ban, () => send('ban ' + name));
    li.append(label, kick, ban);
    ul.append(li);
  }
  $('#noPlayers').hidden = players.length > 0;
  $('#playerCount').textContent = $('#playerCount2').textContent = players.length;
}

function renderStatus(s) {
  status = s;
  $('#state').dataset.state = s.state;
  $('#stateText').textContent = LABELS[s.state] || s.state;
  $('#world').textContent = s.world || '—';
  $('#startBtn').disabled = !['stopped', 'crashed'].includes(s.state) || !s.installed;
  $('#stopBtn').disabled = !['running', 'starting', 'restarting', 'crashed'].includes(s.state);
  $('#restartBtn').disabled = s.state !== 'running';
  const live = ['running', 'starting'].includes(s.state);
  document.querySelectorAll('.live, #cmd, #cmdForm button').forEach((el) => { el.disabled = !live; });
  syncRestartGuards();
  renderPlayers(s.players);
  renderBanner();
  tick();
}

$('#startBtn').addEventListener('click', () => api('api/start', {}).catch((e) => toast(e.message, true)));
guarded($('#restartBtn'), () => api('api/restart', {}).catch((e) => toast(e.message, true)));
guarded($('#stopBtn'), () => api('api/stop', {}).catch((e) => toast(e.message, true)));

function connect() {
  const es = new EventSource('api/events');
  es.addEventListener('reset', (e) => resetConsole(JSON.parse(e.data)));
  es.addEventListener('out', (e) => feed(JSON.parse(e.data)));
  es.addEventListener('status', (e) => renderStatus(JSON.parse(e.data)));
  es.onopen = () => { connected = true; renderBanner(); };
  es.onerror = () => { connected = false; renderBanner(); };
}

// ---- Worlds ------------------------------------------------------------------
function renderWorlds(worlds, active) {
  const rows = worlds.map((w) => ({
    ...w, meta: `${(w.size / 1048576).toFixed(1)} MB  ·  saved ${new Date(w.modified * 1000).toLocaleString()}`,
  }));
  if (active && !worlds.some((w) => w.path === active)) {
    rows.push({ name: active.split('/').pop().replace(/\.wld$/, ''), path: active, meta: 'Not generated yet. It is created when the server starts.' });
  }
  const ul = $('#worldList');
  ul.textContent = '';
  for (const w of rows) {
    const li = document.createElement('li');
    const info = document.createElement('div');
    info.className = 'info';
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = w.name;
    const meta = document.createElement('span');
    meta.className = 'hint';
    meta.textContent = w.meta;
    info.append(name, meta);
    li.append(info);
    if (w.path === active) {
      const badge = document.createElement('span');
      badge.className = 'badge';
      badge.textContent = 'Current';
      li.append(badge);
    } else {
      const btn = document.createElement('button');
      btn.className = 'sm';
      btn.textContent = 'Switch';
      btn.dataset.restarts = 'Confirm restart';
      guarded(btn, () => applyWorld({ world: w.path }, `Switching to ${w.name}…`));
      li.append(btn);
    }
    ul.append(li);
  }
  $('#noWorlds').hidden = rows.length > 0;
  syncRestartGuards();
}

async function applyWorld(body, message) {
  try { await api('api/world', body); } catch (e) { toast(e.message, true); return false; }
  toast(message);
  openTab('console');
  return true;
}

$('#newWorldForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!confirmed($('#createBtn'))) return;
  const name = $('#nwName').value.trim();
  const body = { new_world: {
    name,
    seed: $('#nwSeed').value,
    size: $('#nwSize').value,
    difficulty: $('#nwDiff').value,
    special_seeds: [...document.querySelectorAll('#nwSeeds input:checked')].map((input) => input.value),
  } };
  if (await applyWorld(body, `Creating ${name}…`)) e.target.reset();
});

// ---- Settings ----------------------------------------------------------------
const JOURNEY = [
  ['time_setfrozen', 'Freeze time'], ['time_setdawn', 'Set dawn'], ['time_setnoon', 'Set noon'],
  ['time_setdusk', 'Set dusk'], ['time_setmidnight', 'Set midnight'], ['time_setspeed', 'Time speed'],
  ['godmode', 'Godmode'], ['increaseplacementrange', 'Placement range'], ['setdifficulty', 'Difficulty slider'],
  ['setspawnrate', 'Enemy spawn rate'], ['biomespread_setfrozen', 'Freeze biome spread'], ['rain_setstrength', 'Rain strength'],
  ['rain_setfrozen', 'Freeze rain'], ['wind_setstrength', 'Wind strength'], ['wind_setfrozen', 'Freeze wind'],
];
for (const [key, label] of JOURNEY) {
  const field = document.createElement('label');
  field.className = 'field';
  const sel = document.createElement('select');
  sel.id = 'jp-' + key;
  sel.append(new Option('Locked', '0'), new Option('Host only', '1'), new Option('Everyone', '2'));
  field.append(label, sel);
  $('#perms').append(field);
}
$('#jpAll').addEventListener('change', (e) => {
  for (const [key] of JOURNEY) $('#jp-' + key).value = e.target.value;
  e.target.value = '';
});

async function loadConfig() {
  let c;
  try { c = await api('api/config'); } catch (e) { toast(e.message, true); return; }
  const v = c.values;
  renderWorlds(c.worlds, v.world);
  $('#maxplayers').value = v.maxplayers || 8;
  $('#password').value = v.password || '';
  $('#motd').value = v.motd || '';
  $('#language').value = v.language || 'en-US';
  $('#npcstream').value = v.npcstream ?? 60;
  $('#priority').value = v.priority ?? 1;
  $('#rollbacks').value = v.worldrollbackstokeep ?? 2;
  $('#secure').checked = v.secure === '1';
  $('#slowliquids').checked = v.slowliquids === '1';
  for (const [key] of JOURNEY) $('#jp-' + key).value = v['journeypermission_' + key] ?? 2;
  $('#raw').value = c.raw;
}

function saved() {
  const running = status && ['running', 'starting'].includes(status.state);
  $('#saveMsg').textContent = running ? 'Saved. Press Restart to apply.' : 'Saved.';
  toast('Settings saved');
  loadConfig();
}

$('#settingsForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = {
    maxplayers: Number($('#maxplayers').value),
    npcstream: Number($('#npcstream').value),
    priority: Number($('#priority').value),
    worldrollbackstokeep: Number($('#rollbacks').value),
    password: $('#password').value,
    motd: $('#motd').value,
    language: $('#language').value,
    secure: $('#secure').checked,
    slowliquids: $('#slowliquids').checked,
    journey: Object.fromEntries(JOURNEY.map(([key]) => [key, $('#jp-' + key).value])),
  };
  try { await api('api/config', body); saved(); } catch (err) { toast(err.message, true); }
});

$('#saveRaw').addEventListener('click', async () => {
  try { await api('api/config', { raw: $('#raw').value }); saved(); } catch (err) { toast(err.message, true); }
});

// ---- Tabs ----------------------------------------------------------------------
const TABS = ['console', 'worlds', 'settings'];
function showTab(name) {
  document.querySelectorAll('.tab').forEach((t) => t.setAttribute('aria-selected', String(t.dataset.tab === name)));
  for (const tab of TABS) $('#tab-' + tab).hidden = tab !== name;
  if (name === 'settings') $('#saveMsg').textContent = '';
  if (name !== 'console') loadConfig();
  if (name === 'console') consoleEl.scrollTop = consoleEl.scrollHeight;
}
function openTab(name) {
  location.hash = name === 'console' ? '' : name;
  showTab(name);
}
document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => openTab(t.dataset.tab)));
showTab(TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'console');
connect();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
