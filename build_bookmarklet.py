#!/usr/bin/env python3
"""
Turn bookmarklet.js into a pasteable `javascript:` URL.

Comments are stripped, but line breaks are kept and the whole thing is
percent-encoded. Aggressively collapsing whitespace risks automatic-semicolon
surprises for a few hundred bytes of saving, which isn't a trade worth making
for something you paste once.

    python3 build_bookmarklet.py   ->  writes bookmarklet.txt, bookmarklet.html
"""

import html
import re
import urllib.parse

import config

SRC = config.BOOKMARKLET_SRC
OUT = config.BOOKMARKLET_OUT
HTML_OUT = config.BOOKMARKLET_HTML

# Netscape bookmark format: the thing every browser's importer speaks. It is
# also plain HTML, so opening the file renders the link and you can drag it
# onto the bookmarks bar instead of importing. Two install routes, one file.
BOOKMARK_FILE = """\
<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
    <DT><A HREF="{url}">{title}</A>
</DL><p>
"""


def strip_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)          # block comments
    js = re.sub(r"^\s*//.*$", "", js, flags=re.M)          # whole-line comments
    js = re.sub(r"\n\s*\n+", "\n", js)                     # blank runs
    return js.strip()


def main() -> None:
    code = strip_comments(SRC.read_text(encoding="utf-8"))
    url = "javascript:" + urllib.parse.quote(code, safe="")
    OUT.write_text(url + "\n", encoding="utf-8")

    # quote(safe="") already leaves only unreserved characters, so there is no
    # & or " left to escape. Run it through html.escape anyway rather than rely
    # on that staying true if the encoding is ever loosened.
    HTML_OUT.write_text(
        BOOKMARK_FILE.format(
            url=html.escape(url, quote=True),
            title=html.escape(config.BOOKMARKLET_TITLE),
        ),
        encoding="utf-8",
    )

    print(f"source   {len(SRC.read_text()):>7,} bytes")
    print(f"stripped {len(code):>7,} bytes")
    print(f"encoded  {len(url):>7,} bytes  ->  {OUT.name}, {HTML_OUT.name}")
    if len(url) > config.BOOKMARKLET_MAX_BYTES:
        print("warning: some browsers baulk at bookmarks this long")

    print(f"""
Copy {HTML_OUT.name} to the machine running Chrome, then either:

  import  chrome://bookmarks -> the ... menu -> Import bookmarks
  drag    open the file in Chrome, drag "{config.BOOKMARKLET_TITLE}" to the bar

Then click it on a listing page. {OUT.name} still holds the bare URL if you
would rather paste it by hand.""")


if __name__ == "__main__":
    main()
