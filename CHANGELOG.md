# Changelog

## 0.3.4

- Added Thai-aware natural sorting across Library, Series, filters, and next-volume navigation.
- Numeric ordering keeps volume/page ranges such as `2` before `10`.
- Added a portable server-side Thai-aware natural-sort fallback without OS locale dependencies.

## 0.3.3

- Hardened protected APIs with router-level authentication and separated minimal public health from authenticated diagnostics.
- Reduced NAS load with no-write unchanged-file rescans, thumbnail queue avoidance, generated render-cache cleanup, and safer source-drop detection.
- Added unreadable-file reporting without aborting the full scan.
- Reduced reader/network load with lazy Vertical PDF rendering, throttled/serialized progress saves, low-priority whole-book caching, and sliding recent-reading cache behavior.
- Improved EPUB thumbnail generation and long-session resource cleanup.
- Added compact Library payloads, abortable stale searches, local favorite updates, and Category + Series identity handling.

## 0.3.2

- Split the backend into focused modules for authentication, database access, scanning, PDF rendering, EPUB handling, library APIs, media APIs, and series logic.

## 0.3.1

- Added context-aware Back navigation from Reader to the originating Series.
- Added optional next-volume prompt at the end of a book when a valid next Series item exists.

## 0.3.0

- Added EPUB support without converting or copying source EPUB files.
- Added touch-first Reader UI toggle, swipe-follow-finger PDF paging, thumbnail filmstrip scrubbing, and jump-to-page behavior.
- Added recent-reading device cache with short-visit and active-book lifetimes.
- Added static-asset versioning/cache recovery improvements.

## 0.2.0

- Added dynamic categories, NAS Import, path-based Rescan, missing-file grace period, and the first iPad-first reader overhaul.

## 0.1.0

- Initial public release.
