#!/usr/bin/env python3
"""
Turn bookmarklet.js into a pasteable `javascript:` URL.

Comments are stripped, but line breaks are kept and the whole thing is
percent-encoded. Aggressively collapsing whitespace risks automatic-semicolon
surprises for a few hundred bytes of saving, which isn't a trade worth making
for something you paste once.

    python3 build_bookmarklet.py   ->  writes bookmarklet.txt
"""

import re
import urllib.parse

import config

SRC = config.BOOKMARKLET_SRC
OUT = config.BOOKMARKLET_OUT


def strip_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)          # block comments
    js = re.sub(r"^\s*//.*$", "", js, flags=re.M)          # whole-line comments
    js = re.sub(r"\n\s*\n+", "\n", js)                     # blank runs
    return js.strip()


def main() -> None:
    code = strip_comments(SRC.read_text(encoding="utf-8"))
    url = "javascript:" + urllib.parse.quote(code, safe="")
    OUT.write_text(url + "\n", encoding="utf-8")

    print(f"source   {len(SRC.read_text()):>7,} bytes")
    print(f"stripped {len(code):>7,} bytes")
    print(f"encoded  {len(url):>7,} bytes  ->  {OUT.name}")
    if len(url) > config.BOOKMARKLET_MAX_BYTES:
        print("warning: some browsers baulk at bookmarks this long")
    print("\nAdd a bookmark, paste bookmarklet.txt as the URL, click it on a listing.")


if __name__ == "__main__":
    main()
