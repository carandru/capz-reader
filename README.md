# NAS PDF Reader

A lightweight, self-hosted PDF and EPUB library designed for NAS deployment and touch-first reading on tablets.

Version: **0.3.4**

## What it does

- Indexes existing PDF and EPUB files without copying the source books.
- Stores library metadata, categories, series, tags, favorites, and reading progress in SQLite.
- Uses generated thumbnails/page renders in a separate cache.
- Keeps source-library mounts read-only by default.
- Supports touch-first PDF reading, EPUB reading, series navigation, bulk actions, and private single-user authentication.

## Reader highlights

- PDF: Single, Double, Vertical, Fit Page / Fit Width, LTR / RTL, swipe navigation, page filmstrip, and progress resume.
- EPUB: chapter navigation, table of contents, font size, line spacing, themes, and reading-position resume.
- Persistent low-opacity Show/Hide UI control for distraction-free reading.
- Recent-reading device cache: short visits expire after 3 hours; actively read books use a 24-hour sliding cache window.
- PDF whole-book caching is progressive and yields to interactive reading.
- Generated NAS page/preview cache is cleaned by age and soft size limits.
- Series-aware Back and next-volume flow.
- Thai-aware natural sorting with numeric volume ordering.

## Library behavior

Docker mounts one library root at `/library`. The first folder below that root is used only as the **initial logical category** for newly discovered books. Categories can later be changed in the app without moving source files.

Generic example:

```text
/library/Books/Example Series/Example Series 01.pdf
/library/Comics/Another Series/Another Series 02.epub
```

A newly discovered file can initially infer:

```text
source folder: Books
category: Books
series: Example Series
```

Changing the category or series in the app does not rename or move the source file.

## Import and Rescan

**Import** registers selected existing PDF/EPUB paths from the mounted library. It does not upload or duplicate the file.

**Rescan** synchronizes the index:

```text
unchanged path -> no database write and no unnecessary media work
changed path   -> refresh technical metadata while preserving user metadata
new path       -> add
missing path   -> hide and start the grace period
unreadable source -> skip destructive missing detection for that source
```

Missing records are retained for `MISSING_RETENTION_DAYS` (default `14`) and then purged with generated cache. A suspicious large source drop is protected from automatic mass-missing changes until explicitly confirmed.

## Docker setup

Copy the example configuration:

```bash
cp .env.example .env
```

Edit `.env` for your host:

```env
PUID=1000
PGID=1000
PORT=8020
LIBRARY_PATH=/path/to/library
LIBRARY_ACCESS=ro
ALLOW_DELETE_FILES=false
```

On Linux/NAS systems, use the UID/GID of an account that can read the library directory.

Build and start:

```bash
docker compose up -d --build
```

Open the app at:

```text
http://<server-address>:8020
```

On first run, create the single administrator account.

## Source-file safety

The default configuration is read-only:

```env
LIBRARY_ACCESS=ro
ALLOW_DELETE_FILES=false
```

`Remove from library` removes only the index record and ignores that exact path on future rescans until it is explicitly imported again.

Permanent source-file deletion requires both a read-write mount and `ALLOW_DELETE_FILES=true`.

## Runtime data

The app writes runtime state only to the configured data/cache mounts:

```text
/data   -> SQLite database, metadata, sessions, reading state
/cache  -> generated thumbnails, previews, rendered pages, EPUB cover cache
```

Original PDF/EPUB files remain in `/library` and are not copied by Import.

## Remote access

The recommended deployment is a private LAN or private VPN/mesh network. Do not expose the application directly to the public internet unless you place it behind an appropriately configured HTTPS reverse proxy and apply normal host/network hardening.

For private HTTP:

```env
COOKIE_SECURE=false
```

Behind HTTPS:

```env
COOKIE_SECURE=true
```

## Security model

- Single-user authentication with Argon2 password hashing.
- Protected API routers fail closed by default.
- Detailed diagnostics require authentication.
- Media cache responses are private.
- Source paths are validated to stay within the configured library root.
- Library mounts are read-only by default.

## Repository hygiene

Do not commit `.env`, runtime databases, cache contents, source books, credentials, keys/certificates, tokens, or host-specific private paths.

The repository intentionally contains only application source, dependency/configuration templates, empty runtime-directory placeholders, and third-party license notices.
