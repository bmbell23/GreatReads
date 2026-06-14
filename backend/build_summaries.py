#!/usr/bin/env python3
"""Build a chapter-summary set JSON from a compendium source.

The "Malazan Book of the Fallen Compendium" (highnessatharva.github.io/Malazan-
Compendium, exported via Typora) ships in several formats in books_staging/. The
.epub split files have lost their smart punctuation to U+FFFD replacement chars;
the .mht archive preserves it perfectly. So we parse the .mht: a single HTML
document whose <h1> are book titles and <h2> are chapter titles ("Prologue",
"Chapter One", ... "Epilogue"). Each chapter's summary is the markup between its
<h2> and the next heading.

Output (committed asset, NOT runtime state — lives outside backend/data/):

    backend/summaries/<id>.json
    {
      "id": "malazan",
      "title": "Malazan Book of the Fallen",
      "source": "Malazan Book of the Fallen Compendium.mht",
      "books": [
        {"title": "Gardens of the Moon",
         "chapters": [{"title": "Prologue", "html": "<p>...</p>"}, ...]},
        ...
      ]
    }

The reader matches a Calibre book to a `books[]` entry by normalized title, then
matches the current chapter to a `chapters[]` entry by normalized title (falling
back to ordinal). See backend/server.py /api/summaries/<bookId>.

Usage:
    python3 build_summaries.py SOURCE.mht OUTPUT.json --id malazan \
        --title "Malazan Book of the Fallen"
"""
import argparse
import email
import json
import re
import sys

# Front-matter <h1>s that are not books.
FRONT_MATTER = {"malazan book of the fallen compendium", "about", "contents",
                "table of contents", "introduction"}

# Tags we keep inside a summary body. Everything else is unwrapped to its text.
KEEP_TAGS = {"p", "em", "strong", "i", "b", "br", "blockquote", "ul", "ol", "li"}


def load_mht_html(path):
    """Decode the single text/html part out of a .mht MIME archive."""
    msg = email.message_from_bytes(open(path, "rb").read())
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            cs = part.get_content_charset() or "utf-8"
            return part.get_payload(decode=True).decode(cs, errors="replace")
    raise SystemExit("no text/html part found in %s" % path)


def strip_tags_keep(html):
    """Reduce arbitrary markup to a clean paragraph subset.

    - drop <script>/<style> and the Typora md-toc block
    - drop "Back to top" anchors and all other links (keep their text)
    - strip every attribute from kept tags
    - unwrap any tag not in KEEP_TAGS
    """
    # Remove script/style wholesale.
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    # Drop "Back to top" navigation links entirely (text included). The source
    # wraps the label in inner markup, so match any <a> whose content contains
    # the phrase rather than requiring it to be the anchor's only text.
    html = re.sub(r"<a\b[^>]*>.*?</a>",
                  lambda m: "" if re.search(r"back to top", m.group(0), re.I)
                  else m.group(0),
                  html, flags=re.S | re.I)

    def repl(m):
        closing = m.group(1) == "/"
        tag = m.group(2).lower()
        if tag in KEEP_TAGS:
            if closing:
                return "</%s>" % tag
            if tag == "br":
                return "<br/>"
            return "<%s>" % tag
        return ""  # unwrap: keep inner text, drop the tag

    html = re.sub(r"<(/?)([a-zA-Z0-9]+)\b[^>]*>", repl, html)
    # Belt-and-suspenders: if the "Back to top" label survived as bare text
    # (anchor inner markup the rule above didn't see), strip it wherever it
    # trails a block. Never touches it mid-prose.
    html = re.sub(r"[ \t\n]*Back to top[ \t\n]*(</(?:p|li|blockquote|ul|ol)>|$)",
                  r"\1", html, flags=re.I)
    # Collapse whitespace, drop empty paragraphs.
    html = re.sub(r"<p>\s*</p>", "", html)
    html = re.sub(r"[ \t]*\n[ \t]*", "\n", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def html_to_text(html):
    txt = re.sub(r"<[^>]+>", " ", html)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()


def parse(html, set_id, set_title, source_name):
    # Locate every <h1>/<h2> heading in document order: (start, end, level, text).
    heads = []
    for m in re.finditer(r"<h([12])\b[^>]*>(.*?)</h\1>", html, flags=re.S | re.I):
        text = html_to_text(m.group(2))
        if text:
            heads.append((m.start(), m.end(), int(m.group(1)), text))

    books = []
    current = None
    for i, (hstart, hend, level, text) in enumerate(heads):
        nxt = heads[i + 1][0] if i + 1 < len(heads) else len(html)
        if level == 1:
            if text.strip().lower() in FRONT_MATTER:
                current = None
                continue
            current = {"title": text.strip(), "chapters": []}
            books.append(current)
        else:  # h2 == chapter
            if current is None:
                continue  # an h2 before any book heading: skip
            body = strip_tags_keep(html[hend:nxt])
            if not body:
                continue
            current["chapters"].append({"title": text.strip(), "html": body})

    books = [b for b in books if b["chapters"]]
    return {"id": set_id, "title": set_title, "source": source_name,
            "books": books}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("output")
    ap.add_argument("--id", required=True)
    ap.add_argument("--title", required=True)
    args = ap.parse_args()

    html = load_mht_html(args.source)
    src_name = args.source.rsplit("/", 1)[-1]
    data = parse(html, args.id, args.title, src_name)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    print("Wrote %s" % args.output)
    print("Books: %d" % len(data["books"]))
    for b in data["books"]:
        print("  %-22s %3d chapters" % (b["title"][:22], len(b["chapters"])))


if __name__ == "__main__":
    main()
