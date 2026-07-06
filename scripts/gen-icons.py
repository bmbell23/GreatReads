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
    save(cropped, f'{o}/ic_launcher_foreground.png', FG[d])   # full-bleed adaptive fg
print('Done. (APK needs ./build-app.sh to take effect; web/app icons are live on next load.)')
