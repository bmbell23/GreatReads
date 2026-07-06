#!/usr/bin/env python3
"""Regenerate every app icon from a master image.

Usage:
  python3 scripts/gen-icons.py             # LIVE: assets/app-icon.png -> web + app favicons + APK launcher
  python3 scripts/gen-icons.py --variants  # every assets/app-icon-<name>.png -> assets/generated/<name>/ set + preview
  python3 scripts/gen-icons.py --preview    # contact sheet of the live icon + all variants (no writes to app)

Approach (#243): the design (bookmark + its icons/shadow) sits on a solid background
and is centred. We sample that background from the border (the bookmark is centred,
so the edges are pure background), then EXTRACT the design as everything that differs
from the background — dropping the near-black rounded corners. The design is composited
onto a clean solid background inside Android's safe zone. No per-colour assumptions, so
it works for a white / coloured / multi-colour bookmark on a solid background. (A
gradient background — e.g. the *inverse* variant — is an unsupported edge case here and
will look approximate; those want a full-bleed background bitmap instead.)
"""
from PIL import Image, ImageDraw
import numpy as np, os, sys, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, 'assets')

# ── core image ops ───────────────────────────────────────────────────────────
def clean_bg(a):
    """Median background colour from the border, skipping near-black corners."""
    H, W, _ = a.shape
    px = []
    for g in np.linspace(0.2, 0.8, 13):
        for (x, y) in ((4, int(H*g)), (W-5, int(H*g)), (int(W*g), 4), (int(W*g), H-5)):
            r, gg, b = a[y, x]
            if min(r, gg, b) > 60:
                px.append((r, gg, b))
    return np.median(np.array(px), axis=0) if px else np.array([255, 255, 255])

# The bookmark's bounds as fractions of the master, shared by the whole icon family
# (every variant is the same artwork recoloured). Cropping to this FIXED region — not
# a per-variant auto-detected bbox — keeps all variants identical in size and position.
# Auto-detect can't do this uniformly: a white-on-white bookmark (the MAIN variant) has
# no detectable body, so its bbox collapses to the coloured icons + stray edge pixels
# and the design ends up smaller. (Arbitrary uploads with different geometry — #244 —
# would need detection or their own region.)
DESIGN_REGION = (0.332, 0.098, 0.672, 0.923)   # left, top, right, bottom

def extract_design(a, bg):
    """The design on transparent: everything that differs from the background, minus
    the near-black rounded corners, cropped to the shared bookmark region."""
    H, W, _ = a.shape
    dist = np.sqrt(((a - bg) ** 2).sum(axis=2))
    yy, xx = np.mgrid[0:H, 0:W]
    near = (np.minimum(xx, W-1-xx) < W*0.20) & (np.minimum(yy, H-1-yy) < H*0.20)
    black_corner = (a.min(axis=2) < 45) & near
    alpha = np.clip((dist - 22) * 7, 0, 255).astype('uint8')
    alpha[black_corner] = 0
    rgba = np.dstack([a, alpha]).astype('uint8')
    l, t, r, b = (int(W*DESIGN_REGION[0]), int(H*DESIGN_REGION[1]),
                  int(W*DESIGN_REGION[2]), int(H*DESIGN_REGION[3]))
    return Image.fromarray(rgba, 'RGBA').crop((l, t, r, b))

def prep(master):
    a = np.array(Image.open(master).convert('RGB')).astype(int)
    bg = clean_bg(a)
    return extract_design(a, bg), tuple(int(v) for v in bg)

def tile(size, design, bg, hfrac=0.66, transparent=False):
    """Design centred at hfrac of the tile height, on a solid background (or
    transparent, for the adaptive foreground layer whose background is the bg drawable)."""
    base = Image.new('RGBA', (size, size), (0, 0, 0, 0) if transparent else bg + (255,))
    th = int(size * hfrac)
    bw, bh = design.size
    nw = max(1, int(round(bw * th / bh)))
    base.alpha_composite(design.resize((nw, th), Image.LANCZOS), ((size - nw)//2, (size - th)//2))
    return base

def circle(img):
    s = min(img.size); img = img.resize((s, s), Image.LANCZOS)
    m = Image.new('L', (s, s), 0); ImageDraw.Draw(m).ellipse((0, 0, s-1, s-1), fill=255)
    o = Image.new('RGBA', (s, s), (0, 0, 0, 0)); o.paste(img, (0, 0), m); return o

# ── output sets ──────────────────────────────────────────────────────────────
LEG = {'mdpi': 48, 'hdpi': 72, 'xhdpi': 96, 'xxhdpi': 144, 'xxxhdpi': 192}
FG  = {'mdpi': 108, 'hdpi': 162, 'xhdpi': 216, 'xxhdpi': 324, 'xxxhdpi': 432}

def write_launcher(res, design, bg, quiet=False):
    bg_hex = '#%02X%02X%02X' % bg
    for d in LEG:
        o = f'{res}/mipmap-{d}'; os.makedirs(o, exist_ok=True)
        tile(LEG[d], design, bg).convert('RGB').save(f'{o}/ic_launcher.png')
        circle(tile(LEG[d], design, bg)).save(f'{o}/ic_launcher_round.png')
        # Adaptive foreground: design on transparent (safe zone); background = bg drawable.
        tile(FG[d], design, bg, transparent=True).save(f'{o}/ic_launcher_foreground.png')
    os.makedirs(f'{res}/drawable', exist_ok=True)
    with open(f'{res}/drawable/ic_launcher_bg.xml', 'w') as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n'
                '<shape xmlns:android="http://schemas.android.com/apk/res/android" android:shape="rectangle">\n'
                f'    <solid android:color="{bg_hex}"/>\n</shape>\n')
    if not quiet:
        print('   launcher mipmaps + drawable/ic_launcher_bg.xml (solid', bg_hex + ')')

def masked_preview(design, bg, S=384, kind='squircle'):
    t = tile(S, design, bg)
    m = Image.new('L', (S, S), 0); dr = ImageDraw.Draw(m)
    (dr.ellipse if kind == 'circle' else
     (lambda box, fill: dr.rounded_rectangle(box, radius=int(S*0.22), fill=fill)))((0, 0, S-1, S-1), fill=255)
    out = Image.new('RGBA', (S, S), (0, 0, 0, 0)); out.paste(t, (0, 0), m); return out

def gen_live():
    design, bg = prep(f'{ASSETS}/app-icon.png')
    print('web shell (:8090):')
    for sz, f in ((32, 'icon-32'), (192, 'icon-192'), (512, 'icon-512')):
        tile(sz, design, bg).convert('RGB').save(f'{ROOT}/web/{f}.png'); print('  ', f'web/{f}.png')
    print('FastAPI app (:8092):')
    tile(512, design, bg).convert('RGB').save(f'{ROOT}/greatreads/src/greatreads/static/favicon.png')
    tile(192, design, bg).convert('RGB').save(f'{ROOT}/greatreads/src/greatreads/static/favicon_app_icon.png')
    print('   static/favicon.png + favicon_app_icon.png')
    print('APK launcher:')
    write_launcher(f'{ROOT}/simple-app/app/src/main/res', design, bg)
    print('Done. (APK needs ./build-app.sh; web/app favicons live on next load — bump ?v= in the HTML to bust cache.)')

def gen_variants():
    out_root = f'{ASSETS}/generated'
    for path in sorted(glob.glob(f'{ASSETS}/app-icon-*.png')):
        name = os.path.basename(path)[len('app-icon-'):-4]
        design, bg = prep(path)
        vdir = f'{out_root}/{name}'
        write_launcher(f'{vdir}/res', design, bg, quiet=True)
        tile(192, design, bg).convert('RGB').save(f'{vdir}/favicon.png')
        masked_preview(design, bg).convert('RGB').save(f'{vdir}/preview.png')
        print('  variant', name, '->', os.path.relpath(vdir, ROOT), '(bg #%02X%02X%02X)' % bg)
    print('Done. Variant sets staged under assets/generated/<name>/ (wired into the app by the icon-switch work).')

def gen_preview():
    names = [('app-icon.png', 'MAIN')] + [(os.path.basename(p), os.path.basename(p)[len('app-icon-'):-4])
                                           for p in sorted(glob.glob(f'{ASSETS}/app-icon-*.png'))]
    S, pad = 300, 18
    cols = 4; rows = (len(names) + cols - 1)//cols
    canvas = Image.new('RGB', (cols*(S+pad)+pad, rows*(S+pad+22)+pad), (150, 152, 158))
    d = ImageDraw.Draw(canvas)
    for i, (fn, lbl) in enumerate(names):
        design, bg = prep(f'{ASSETS}/{fn}')
        p = masked_preview(design, bg, S)
        x = pad+(i % cols)*(S+pad); y = pad+(i//cols)*(S+pad+22)
        canvas.paste(p.convert('RGB'), (x, y), p); d.text((x+4, y+S+4), lbl, fill=(0, 0, 0))
    canvas.save('/tmp/icons_all.png'); print('saved /tmp/icons_all.png')

if __name__ == '__main__':
    arg = sys.argv[1] if len(sys.argv) > 1 else ''
    if arg == '--variants': gen_variants()
    elif arg == '--preview': gen_preview()
    else: gen_live()
