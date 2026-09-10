# NAS PDF Reader

A lightweight, NAS-first PDF library and reader designed for large personal collections and touch-first use on iPad.

Version: **0.2.0**

## Why this exists

The app keeps original PDFs in their existing NAS folders. It indexes the existing paths, stores library metadata and reading state in SQLite, and keeps generated thumbnails/previews in a separate cache. Importing a PDF never copies the source file.

## Highlights in v0.2.0

- Tap a book card to resume reading immediately; use the three-dot button for details/editing.
- iPad-first full-screen reader with `Fit page` and `Fit width`.
- Single page, real double-page spread, vertical, LTR and RTL modes.
- Auto-hiding reader controls, loading state, neighboring-page preload and swipe navigation.
- Dynamic categories: create, rename, hide and remove them from Settings.
- NAS Import browser: register selected existing PDFs without duplicating them.
- Rescan uses the PDF path as identity. Existing paths keep user-edited metadata and progress.
- If a file disappears, its record is hidden immediately and kept for 14 days by default before cleanup.
- If the same path returns during the grace period, its metadata/progress is restored.
- If a PDF is renamed, the old path becomes missing and the new path is indexed as a new book.
- A mounted source that cannot be read is never treated as an empty folder, preventing accidental mass-missing state.
- Password can be changed from Settings.
- v0.1 databases migrate in place.

## Library model

Docker mounts source folders below `/library`. The first folder name is used only as the **initial category** for newly discovered PDFs. Afterwards, the logical category can be changed freely in the app without moving the PDF.

Example:

```text
/library/Novel/Example Series/Example Series 01.pdf
/library/Manga/Another Series/Another Series 01.pdf
```

For a new PDF, the scanner initially infers:

```text
source folder: Novel
category: Novel
series: Example Series
```

Changing the category in the app does not rename or move the source file.

## Import vs Rescan

**Import** lets you browse mounted NAS folders and register selected PDFs immediately. It stores only the existing relative path; no PDF is uploaded or copied.

**Rescan** synchronizes the index with all readable mounted source folders:

```text
existing path -> keep metadata/progress, refresh technical file info if needed
new path      -> add
missing path  -> hide and start the grace period
unreadable source -> skip missing detection for that source
```

Missing records are retained for `MISSING_RETENTION_DAYS` (default `14`) and then purged together with their generated cache. Settings also provides a manual `Clean missing paths now` action.

## Docker setup

Copy the example configuration:

```bash
cp .env.example .env
```

Edit `.env` for your machine:

```env
PUID=1000
PGID=1000
PORT=8020

NOVEL_PATH=/path/to/novels
MANGA_PATH=/path/to/manga

LIBRARY_ACCESS=ro
ALLOW_DELETE_FILES=false
MISSING_RETENTION_DAYS=14
```

Add additional source mounts in `docker-compose.yml` when needed, for example:

```yaml
- "/path/to/artbooks:/library/Artbook:ro"
```

The next Rescan will discover the PDFs and create `Artbook` as an initial logical category if it does not exist yet.

On Linux/NAS systems, find the UID/GID of the account that can read the library folders with:

```bash
id your-user
```

Build and start:

```bash
docker compose up -d --build
```

Open:

```text
http://<server-ip>:8020
```

On first run, create the single administrator account.

## Source-file safety

The default configuration is read-only:

```env
LIBRARY_ACCESS=ro
ALLOW_DELETE_FILES=false
```

`Remove from library` removes only the index record and ignores that exact path on future rescans until you explicitly Import it again.

Permanent source-file deletion requires both a read-write mount and `ALLOW_DELETE_FILES=true`.

## Data locations

The app writes only to:

```text
./data   -> SQLite database, library metadata and reading state
./cache  -> generated thumbnails, previews and rendered pages
```

Original PDFs stay in the mounted source folders.

## Private remote access

The intended deployment is private LAN or private VPN/Tailscale access rather than direct public exposure.

For plain HTTP on a private network:

```env
COOKIE_SECURE=false
```

Behind HTTPS:

```env
COOKIE_SECURE=true
```

## Git safety

Do not commit `.env`, SQLite databases, source PDFs, cache contents, keys, certificates, credentials, tokens, or machine-specific private paths.
