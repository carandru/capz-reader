# Changelog

## 0.2.0

- iPad-first reader overhaul with true full-screen landscape Fit Page mode.
- Added Single, Double spread, and Vertical reader modes with clearer segmented controls.
- Added Fit Page / Fit Width, LTR / RTL, preloading, loading indicator, swipe navigation, and auto-hiding reader controls.
- Book cards now open the reader directly; three-dot actions open compact details/edit controls.
- Added password change in Settings.
- Added dynamic categories that can be created, renamed, hidden, and removed from inside the app.
- Added NAS Import browser: registers existing PDF paths without copying PDF files.
- Rescan is now path-based and idempotent: existing paths keep user metadata; new paths are added.
- Missing files are hidden immediately, retained for 14 days by default, then purged with their cache.
- Rescan will not mark books missing when a mounted source cannot be read.
- Added manual cleanup for missing records and cache.
- Existing v0.1 databases are migrated in place; reading progress and library metadata are preserved.

## 0.1.0

- Initial public release.
