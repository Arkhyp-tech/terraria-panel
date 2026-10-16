# Terraria Panel

A vanilla Terraria dedicated server with a small web panel, packaged as a CasaOS / Docker Compose app.

The panel runs `TerrariaServer` inside a pseudo-terminal and streams its console to the browser. From the panel you can:

- send console commands
- start, stop and restart the server
- see who is online
- switch worlds
- edit `serverconfig.txt`, including special seeds and Journey mode permissions

It uses only the Python standard library, so it runs on the stock `python:3.13-slim` image with nothing to build. If the server crashes it restarts on its own, and it gives up after 5 crashes in 5 minutes.

## Setup

1. Download the Linux dedicated server from [terraria.org](https://terraria.org) and put its files in `/DATA/AppData/terraria/server/`. You need `TerrariaServer.bin.x86_64` in that folder.
2. Copy `panel.py` to `/DATA/AppData/terraria/panel.py`.
3. Change `PANEL_PASSWORD` in `docker-compose.yml`.
4. Start it with `docker compose up -d`, or import `docker-compose.yml` as a custom app in CasaOS.

Data folder layout:

```
/DATA/AppData/terraria/      (mounted at /data)
├── server/                  Linux server files
├── worlds/                  .wld world files
├── serverconfig.txt         created on first start if missing
└── panel.py
```

## Ports

| Port | What |
| ---- | ---- |
| 7777 | Terraria game port |
| 8777 | Web panel (8080 inside the container) |

## Environment variables

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `PANEL_PASSWORD` | empty | Password for the web panel (any username works). Empty means no login. |
| `TZ` | | Time zone |
| `AUTOSTART` | `1` | Set to `0` so the server does not start with the container |
| `PANEL_PORT` | `8080` | Panel port inside the container |
| `DATA_DIR` | `/data` | Data folder inside the container |
| `TERRARIA_BINARY` | `$DATA_DIR/server/TerrariaServer.bin.x86_64` | Path to the server binary |

The panel uses HTTP Basic auth over plain HTTP. Keep it on your LAN or behind a reverse proxy with HTTPS, and don't expose port 8777 to the internet directly.

Terraria is made by Re-Logic. This repo contains only the panel and does not include the game server files.
