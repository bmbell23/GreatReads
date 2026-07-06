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

# --- Extract the central design (the white bookmark + its icons) on transparent ---
# The gradient fills the tile edges; the bookmark is the border-sealed non-exterior
# region (flood-fill the gradient/black exterior from the borders; what's left is
# the bookmark body + its interior icons). Used for the ADAPTIVE FOREGROUND so it
# can sit inside Android's safe zone instead of being zoomed/cropped full-bleed.
def extract_design():
    S = 512
    a = np.array(im.resize((S, S), Image.LANCZOS)).astype(int)
    nonwhite = a.min(axis=2) <= 185
    vis = np.zeros_like(nonwhite, bool); dq = deque()
    for x in range(S):
        for y in (0, S-1):
            if nonwhite[y, x] and not vis[y, x]: vis[y, x] = True; dq.append((y, x))
    for y in range(S):
        for x in (0, S-1):
            if nonwhite[y, x] and not vis[y, x]: vis[y, x] = True; dq.append((y, x))
    while dq:
        y, x = dq.popleft()
        for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
            ny, nx = y+dy, x+dx
            if 0 <= ny < S and 0 <= nx < S and nonwhite[ny, nx] and not vis[ny, nx]:
                vis[ny, nx] = True; dq.append((ny, nx))
    design = ~vis
    rgb = np.array(im.resize((S, S), Image.LANCZOS))
    rgba = np.dstack([rgb, np.where(design, 255, 0).astype('uint8')])
    img = Image.fromarray(rgba, 'RGBA')
    ys, xs = np.where(design)
    return img.crop((xs.min(), ys.min(), xs.max()+1, ys.max()+1))

DESIGN = extract_design()   # tight-cropped bookmark on transparent

def foreground(size):
    """Adaptive foreground: the design sized to Android's SAFE ZONE (~60% of the
    108dp layer) so the launcher's mask/zoom shows it whole, not cropped. The
    gradient comes from the background drawable (ic_launcher_bg.xml)."""
    canvas = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    target_h = int(round(size * 0.60))
    bw, bh = DESIGN.size
    nw = max(1, int(round(bw * target_h / bh)))
    d = DESIGN.resize((nw, target_h), Image.LANCZOS)
    canvas.paste(d, ((size - nw) // 2, (size - target_h) // 2), d)
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
print('Done. (APK needs ./build-app.sh to take effect; web/app icons are live on next load.)')
