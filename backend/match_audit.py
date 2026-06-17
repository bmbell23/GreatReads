#!/usr/bin/env python3
"""Audiobook<->ebook matching audit.

A repeatable health-check for the Calibre/ABS matching pipeline. It pulls the
live library from the RUNNING backend (so it needs no ABS credentials of its
own — the backend already has them) and runs the exact production grouping /
matching logic from server.py against it, then reports:

  * counts (books, ABS items, editions, matched works, audio-only)
  * multi-edition works (e.g. ebook + audiobook + dramatized)
  * multi-part editions that were stitched (auto or via links.json)
  * ORPHAN PARTS — single-part editions whose title says "N of M" (total>1):
    their sibling part has a divergent title/author so auto-grouping missed it.
    These are the cases that need a manual links.json entry; a ready-to-paste
    snippet is emitted for each suspected pairing.

Usage:  python3 match_audit.py [--base URL]   (default http://localhost:8091)
Exit status is 0 always — this is a report, not a gate.
"""
import argparse
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as server  # noqa: E402  (reuse the real grouping/matching logic; app.py replaced server.py)


def fetch(base):
    books = requests.get(f'{base}/api/books', params={'limit': 10000}, timeout=60)
    books.raise_for_status()
    abks = requests.get(f'{base}/api/audiobooks', timeout=60)
    abks.raise_for_status()
    return books.json().get('books', []), abks.json().get('audiobooks', [])


def orphan_groups(editions):
    """Single-part editions whose title still parses a multi-part marker,
    grouped by a loose base-title key so likely siblings cluster together."""
    orphans = []
    for ed in editions:
        if len(ed['parts']) != 1:
            continue
        idx, tot = server._parse_part(ed['parts'][0]['title'])
        if idx and tot and tot > 1:
            orphans.append((ed, idx, tot))
    clusters = {}
    for ed, idx, tot in orphans:
        title = ed['parts'][0]['title']
        base = server._norm(server._strip_edition(server._strip_part(title.split(':')[0])))
        # Loose key: first two significant words of the base title.
        key = ' '.join(base.split()[:2])
        clusters.setdefault(key, []).append((ed, idx, tot))
    return clusters


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='http://localhost:8091')
    args = ap.parse_args()

    try:
        books, abs_items = fetch(args.base)
    except Exception as e:
        print(f"❌ Could not reach backend at {args.base}: {e}")
        print("   Start it first:  cd backend && ./run.sh &")
        return 0

    links_norm = server._normalize_links(server._load_links())
    editions, forced = server._group_editions(abs_items, links_norm)
    merged = server.match_works(books, abs_items, include_audio_only=True)

    matched = [m for m in merged if m.get('mediaTypes') == ['ebook', 'audiobook']]
    audio_only = [m for m in merged if m.get('mediaTypes') == ['audiobook']]
    multipart = [e for e in editions if len(e['parts']) > 1]

    print("=" * 64)
    print("  MATCHING AUDIT")
    print("=" * 64)
    print(f"  Calibre books        : {len(books)}")
    print(f"  ABS items            : {len(abs_items)}")
    print(f"  Editions (grouped)   : {len(editions)}  "
          f"({len(multipart)} multi-part, {len(forced)} forced via links.json)")
    print(f"  Matched works        : {len(matched)}")
    print(f"  Audio-only editions  : {len(audio_only)}")
    print()

    multi = [m for m in matched if len(m.get('audioEditions', [])) > 1]
    print(f"-- Works with multiple audio editions ({len(multi)}) " + "-" * 20)
    for m in multi:
        kinds = ', '.join(f"{e['label']} ({len(e['parts'])}p)" for e in m['audioEditions'])
        print(f"  • {m['title']} — {m['author']}: {kinds}")
    print()

    print(f"-- Stitched multi-part editions ({len(multipart)}) " + "-" * 24)
    for e in multipart:
        tag = ' [forced]' if e['editionId'] in forced else ''
        print(f"  • {e['label']}{tag}: {len(e['parts'])} parts")
        for p in e['parts']:
            print(f"      {p['index'] + 1}. {p['title']}  [{p['absId']}]")
    print()

    clusters = orphan_groups(editions)
    n_orphans = sum(len(v) for v in clusters.values())
    print(f"-- ORPHAN PARTS needing a manual link ({n_orphans}) " + "-" * 16)
    if not n_orphans:
        print("  (none — every multi-part set is grouped)")
    for key, lst in sorted(clusters.items()):
        lst.sort(key=lambda t: t[1])
        print(f"  ~ '{key}…':")
        for ed, idx, tot in lst:
            p = ed['parts'][0]
            print(f"      part {idx}/{tot}: {p['title']}  [{p['absId']}]  ({ed['kind']})")
        snippet = {"<calibre_id>": {"editions": [{
            "kind": lst[0][0]['kind'],
            "label": lst[0][0]['label'],
            "parts": [ed['parts'][0]['absId'] for ed, _, _ in lst],
        }]}}
        print("      suggested links.json entry:")
        print("      " + json.dumps(snippet, indent=2).replace("\n", "\n      "))
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
