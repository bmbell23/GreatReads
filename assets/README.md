# App icon — single source of truth

**One file drives every icon in the project.** To change the app icon anywhere,
edit the master and run the generator. Do **not** hand-edit any of the derived
files listed below — they are overwritten by the script.

## The master
- `assets/app-icon.png` — the ONE source image (1024×1024 recommended, RGB/RGBA).
  A full-bleed square design is fine; black/near-black rounded corners are
  automatically cropped to transparent (every platform masks the corners anyway).
- **The icon's own background is respected (#241).** The generator samples the
  master's background colour and writes it as the Android adaptive **background**
  layer (`ic_launcher_bg.xml`), then extracts just the central bookmark for the
  **foreground** — so the launcher tile shows *your* background, not a hard-coded
  gradient. Bookmark extraction keys on **blue > green** (works when the design is
  the pink→purple→blue bookmark on a warm/parchment background); if you switch to a
  radically different colour scheme, revisit `extract_bookmark()` in the generator.

## Change the icon everywhere
```bash
# 1. Replace the master
cp /path/to/new-icon.png assets/app-icon.png

# 2. Regenerate all derived icons from it
python3 scripts/gen-icons.py

# 3. Make it live
#    - Web + app favicons: live on next page load (served from disk).
#    - APK launcher icon:  ./build-app.sh   (rebuild + re-stage the APK)
```
If a browser/WebView still shows the old favicon (favicon cache is sticky), bump
the `?v=N` query on the favicon `<link>`s (base.html + web/player.html,
web/reader.html, web/about.html).

## What the generator writes (derived — do not hand-edit)
`scripts/gen-icons.py` regenerates, all from `assets/app-icon.png`:

| Target | Files |
|---|---|
| Web shell (`serve.py :8090`) | `web/icon-32.png`, `web/icon-192.png`, `web/icon-512.png` |
| FastAPI app (`:8092`) | `greatreads/src/greatreads/static/favicon.png`, `…/favicon_app_icon.png` |
| APK launcher | `simple-app/app/src/main/res/mipmap-{mdpi,hdpi,xhdpi,xxhdpi,xxxhdpi}/{ic_launcher,ic_launcher_round,ic_launcher_foreground}.png` |
| Adaptive background | `simple-app/app/src/main/res/drawable/ic_launcher_bg.xml` (solid colour sampled from the master, #241) |

Not regenerated (static): the two adaptive XMLs in `mipmap-anydpi-v26/` (they just
reference `@drawable/ic_launcher_bg` + `@mipmap/ic_launcher_foreground`).
`static/favicon.svg` is an old unreferenced FontAwesome book — base.html uses
`favicon.png`, so the SVG can be ignored.

## Which references point where (so nothing is missed)
- App pages use `static/favicon.png` (tab) + `favicon_app_icon.png` (apple-touch) via `base.html`.
- Reader/player/about pages use `web/icon-32.png` + `web/icon-192.png`.
- The APK manifest uses `@mipmap/ic_launcher` / `@mipmap/ic_launcher_round`
  (adaptive on API 26+ = `ic_launcher_bg.xml` background + `ic_launcher_foreground.png`).

_History: #227 (new icon), #227 follow-up (single-source generator + favicon fix)._
