#!/usr/bin/env python3
"""Regenerate EVERY app icon from ONE master file: assets/app-icon.png.

Change that single file and run:  python3 scripts/gen-icons.py
It updates: the APK launcher (all densities + adaptive foreground), the web-shell
favicons (web/icon-*.png), and the FastAPI app favicons (static/favicon*.png).
Black rounded corners in the master are cropped to transparent (every platform
masks the corners anyway), so the icon reads clean on tab, home screen, and launcher.
"""
from PIL import Image, ImageDraw
import numpy as np, os
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(ROOT, 'assets', 'app-icon.png')
im = Image.open(MASTER).convert('RGB'); W, H = im.size
maxc = np.array(im).astype(int).max(axis=2)
alpha = np.clip((maxc - 14) * 10, 0, 255).astype('uint8')      # crop near-black corners
cropped = Image.fromarray(np.dstack([np.array(im), alpha]), 'RGBA')

def circle(img):
    s = min(img.size); img = img.resize((s, s), Image.LANCZOS)
    m = Image.new('L', (s, s), 0); ImageDraw.Draw(m).ellipse((0, 0, s-1, s-1), fill=255)
    o = Image.new('RGBA', (s, s), (0, 0, 0, 0)); o.paste(img, (0, 0), m); return o

# --- Sample the icon's OWN background colour (its base/parchment fill) --------
# The adaptive-icon BACKGROUND layer is set to this solid colour (ic_launcher_bg.xml
# is (re)written below), so the masked bleed area matches the ICON'S own background
# instead of a hard-coded gradient. Sampled from the border ring, skipping the dark
# rounded corners and the saturated central design. (#241)
def sample_bg():
    a = np.array(im).astype(int); H, W, _ = a.shape
    vals = []
    for fx in (0.05, 0.10, 0.90, 0.95):
        for fy in (0.10, 0.30, 0.50, 0.70, 0.90):
            for (x, y) in ((int(W*fx), int(H*fy)), (int(W*fy), int(H*fx))):
                r, g, b = a[y, x]
                if min(r, g, b) > 110:           # skip dark corners + colourful design
                    vals.append((r, g, b))
    if not vals:
        return (240, 218, 170)
    return tuple(int(v) for v in np.median(np.array(vals), axis=0))

BG_RGB = sample_bg()
BG_HEX = '#%02X%02X%02X' % BG_RGB

# --- Extract just the gradient bookmark on transparent -----------------------
# The bookmark is the only region where blue > green: the parchment background is
# warm (G > B) and the dark corners are neutral (B ≈ G), so `B - G` isolates the
# pink→purple→blue bookmark cleanly — no flood-fill, no black-corner ring. The
# bookmark's interior cut-outs (headphones/tablet/book) are parchment-coloured, so
# they fall out of the mask and simply show the parchment BACKGROUND layer through
# — reproducing the original look exactly. (#241)
def extract_bookmark():
    a = np.array(im).astype(int)
    alpha = np.clip((a[:, :, 2] - a[:, :, 1] - 6) * 25, 0, 255).astype('uint8')
    rgba = np.dstack([np.array(im), alpha])
    # Crop to the bookmark's real mass. A few isolated texture specks also pass the
    # blue>green test; ignore columns/rows carrying <3% of the peak count, else the
    # bbox drifts left and the centred bookmark ends up off-centre. (#241)
    strong = alpha > 120
    cc, rc = strong.sum(0), strong.sum(1)
    ct, rt = max(8, int(cc.max() * 0.03)), max(8, int(rc.max() * 0.03))
    cols, rows = np.where(cc > ct)[0], np.where(rc > rt)[0]
    return Image.fromarray(rgba, 'RGBA').crop((cols.min(), rows.min(), cols.max() + 1, rows.max() + 1))

BOOKMARK = extract_bookmark()

def foreground(size):
    """Adaptive foreground: the gradient bookmark alone, scaled to Android's safe
    zone (~66% of the layer) and centred on transparent. The icon's own parchment
    is the BACKGROUND layer (ic_launcher_bg.xml), so the launcher composites the
    whole bookmark over parchment — no crop, no foreign gradient, no dark ring.
    (#241 — replaces the flood-fill extraction that stripped the background.)"""
    canvas = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    target_h = int(round(size * 0.66))
    bw, bh = BOOKMARK.size
    nw = max(1, int(round(bw * target_h / bh)))
    d = BOOKMARK.resize((nw, target_h), Image.LANCZOS)
    canvas.alpha_composite(d, ((size - nw) // 2, (size - target_h) // 2))
    return canvas

def save(img, path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.resize((size, size), Image.LANCZOS).save(path)
    print('  ', os.path.relpath(path, ROOT))

print('web shell (:8090):')
save(cropped, f'{ROOT}/web/icon-32.png', 32)
save(cropped, f'{ROOT}/web/icon-192.png', 192)
save(cropped, f'{ROOT}/web/icon-512.png', 512)
print('FastAPI app (:8092):')
save(cropped, f'{ROOT}/greatreads/src/greatreads/static/favicon.png', 512)
save(cropped, f'{ROOT}/greatreads/src/greatreads/static/favicon_app_icon.png', 192)
print('APK launcher:')
RES = f'{ROOT}/simple-app/app/src/main/res'
LEG = {'mdpi':48,'hdpi':72,'xhdpi':96,'xxhdpi':144,'xxxhdpi':192}
FG  = {'mdpi':108,'hdpi':162,'xhdpi':216,'xxhdpi':324,'xxxhdpi':432}
for d in LEG:
    o = f'{RES}/mipmap-{d}'
    save(cropped, f'{o}/ic_launcher.png', LEG[d])
    save(circle(cropped), f'{o}/ic_launcher_round.png', LEG[d])
    # Adaptive foreground: design in the safe zone (not full-bleed) so Android's
    # mask/zoom doesn't crop it; the gradient is the background drawable.
    foreground(FG[d]).save(f'{o}/ic_launcher_foreground.png')
    print('  ', os.path.relpath(f'{o}/ic_launcher_foreground.png', ROOT))
# Adaptive-icon BACKGROUND = the icon's OWN sampled background colour (#241),
# replacing the old hard-coded pink→purple→blue gradient so the launcher tile
# matches the icon's parchment instead of a foreign gradient.
BG_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<shape xmlns:android="http://schemas.android.com/apk/res/android" android:shape="rectangle">\n'
    f'    <solid android:color="{BG_HEX}"/>\n'
    '</shape>\n'
)
with open(f'{RES}/drawable/ic_launcher_bg.xml', 'w') as f:
    f.write(BG_XML)
print('  ', f'drawable/ic_launcher_bg.xml (solid {BG_HEX})')
print(f'Done. Adaptive background = {BG_HEX}. (APK needs ./build-app.sh to take effect; web/app icons are live on next load.)')
