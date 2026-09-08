# NAS PDF Reader

A lightweight, NAS-first PDF library and reader designed for large personal collections and touch-first use on iPad.

Version: **0.1.0**

## Why this exists

The app keeps original PDFs in their existing folders instead of uploading/copying them into an application-managed library. It indexes files, stores reading state in SQLite, and caches thumbnails/previews separately.

Key design goals:

- filename is the source of truth for the displayed title;
- direct folder scanning instead of browser bulk uploads;
- separate `Novel`, `Manga`, and `Dou` categories;
- optional series grouping inside each category;
- large-library friendly multi-select and bulk actions;
- touch-friendly iPad UI;
- cached covers/previews so the library view does not repeatedly render PDFs;
- source PDFs are read-only by default;
- suitable for private LAN or Tailscale access.

## Features in v0.1.0

- Scan PDF folders directly from the host/NAS.
- Category tabs: `All`, `Novel`, `Manga`, `Dou`.
- Series and standalone-book views.
- Natural sorting for numbered volumes.
- Search and series filtering.
- Cover thumbnail cache and quick preview cache.
- Continue-reading state and page progress.
- Favorite, archive, and read-state tracking.
- Reader modes for single page, vertical, double page, and RTL.
- iPad/touch support, including long-press selection.
- Desktop Shift-click range selection.
- Bulk actions for category, series, tags, read state, favorites, archive, preview regeneration, and removal from the index.
- Optional permanent source-file deletion, disabled by default.
- Single-admin local authentication with Argon2 password hashing.
- SQLite configured with WAL mode, busy timeout, and serialized writes.
- One preview-render worker to avoid hammering NAS storage.

## Folder model

Each category may live in a completely different location on the host. Docker normalizes them inside the container:

```text
<host novel folder>  -> /library/Novel
<host manga folder>  -> /library/Manga
<host dou folder>    -> /library/Dou
```

A folder directly below a category is treated as a series:

```text
Manga/
└── Example Series/
    ├── Example Series 01.pdf
    ├── Example Series 02.pdf
    └── Example Series 10.pdf
```

A PDF directly inside a category root is treated as standalone.

## Docker setup

Copy the example configuration:

```bash
cp .env.example .env
```

Edit `.env` for your machine. For example:

```env
PUID=1000
PGID=1000
PORT=8020

NOVEL_PATH=/path/to/novels
MANGA_PATH=/path/to/manga
DOU_PATH=/path/to/dou

LIBRARY_ACCESS=ro
ALLOW_DELETE_FILES=false
```

On Linux/NAS systems, find the UID/GID of the account that can read the library folders with:

```bash
id your-user
```

The container starts as root only long enough to prepare `/data` and `/cache`, then drops to the configured `PUID:PGID` before running the web app. This avoids manual `chown` steps for runtime folders while still allowing access to protected NAS home folders when the correct IDs are supplied.

Build and start:

```bash
docker compose up -d --build
```

Open:

```text
http://<server-ip>:8020
```

On first run, create the single administrator account, then scan the library.

## Running on Synology

You can create a Container Manager project from this repository's `docker-compose.yml`. Put machine-specific paths and UID/GID values in `.env`; do not edit them into the repository copy.

If a source category is not currently used, leave its path pointing at the included empty local folder, for example:

```env
MANGA_PATH=./library/Manga
```

## Source-file safety

The default configuration mounts PDF sources read-only:

```env
LIBRARY_ACCESS=ro
ALLOW_DELETE_FILES=false
```

`Remove from library` only removes an item from the index. It does not delete the source PDF.

Permanent source-file deletion requires both:

```env
LIBRARY_ACCESS=rw
ALLOW_DELETE_FILES=true
```

and an explicit confirmation in the UI.

## Private remote access

This app is intended for private access. A common setup is to keep the service off the public internet and reach the NAS through Tailscale or another private VPN.

When using plain HTTP inside a private network, keep:

```env
COOKIE_SECURE=false
```

When placing the app behind HTTPS, set:

```env
COOKIE_SECURE=true
```

## Data locations

The application writes only to:

```text
./data   -> SQLite database and reading state
./cache  -> generated thumbnails/previews/pages
```

Original PDF files remain in the mounted source folders.

## Git safety

Runtime data and machine-specific configuration are intentionally excluded from Git. Do not commit `.env`, SQLite databases, source PDFs, keys, certificates, credentials, or tokens.

## Status

v0.1.0 is an early release intended for personal/self-hosted testing. Back up important data and keep source PDFs read-only until you are comfortable with the deployment.
