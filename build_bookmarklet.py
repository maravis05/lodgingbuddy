#!/usr/bin/env python3
"""
Turn bookmarklet.js into a pasteable `javascript:` URL.

Comments are stripped, but line breaks are kept and the whole thing is
percent-encoded. Aggressively collapsing whitespace risks automatic-semicolon
surprises for a few hundred bytes of saving, which isn't a trade worth making
for something you paste once.

Three outputs, because installing a bookmarklet goes wrong in three different
ways: a page to drag the link off, a bookmark file to import, and the bare URL
for pasting into a bookmark editor by hand.

    python3 build_bookmarklet.py    (`python` on Windows)
"""

import html
import re
import urllib.parse

import config

SRC = config.BOOKMARKLET_SRC
OUT = config.BOOKMARKLET_OUT
HTML_OUT = config.BOOKMARKLET_HTML
INSTALL_OUT = config.BOOKMARKLET_INSTALL

# Netscape bookmark format: the thing every browser's importer speaks. Nothing
# in here but what a parser expects — no styling, no instructions, no stray
# markup that might put an importer off. The page that explains itself is the
# other file.
BOOKMARK_FILE = """\
<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
    <DT><A HREF="{url}">{title}</A>
</DL><p>
"""

# The other install route, and the one worth reaching for first. Dragging a
# *file* onto the bookmarks bar bookmarks the file — which is the mistake this
# page exists to stop, so it says which thing to drag and where. Placeholders
# are substituted rather than formatted, because the CSS is all braces.
INSTALL_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Install the lodgingbuddy bookmarklet</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #fbfaf8; --fg: #1a1a1a; --muted: #5c5c5c;
    --edge: #e2ddd5; --card: #fff; --accent: #7a4b2a;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1a1917; --fg: #ececec; --muted: #a3a3a3;
      --edge: #35332f; --card: #232220; --accent: #d9a172;
    }
  }
  body {
    margin: 0; padding: 2.5rem 1.25rem 4rem; background: var(--bg); color: var(--fg);
    font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  }
  main { max-width: 34rem; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin: 0 0 .4rem; letter-spacing: -.01em; }
  .sub { color: var(--muted); margin: 0 0 2rem; }
  .drop {
    background: var(--card); border: 1px solid var(--edge); border-radius: 10px;
    padding: 1.75rem; text-align: center; margin: 0 0 2rem;
  }
  .grab {
    display: inline-block; padding: .7rem 1.4rem; border-radius: 8px;
    background: var(--accent); color: #fff; text-decoration: none;
    font-weight: 600; cursor: grab;
  }
  @media (prefers-color-scheme: dark) { .grab { color: #1a1917; } }
  .drop p { color: var(--muted); font-size: .875rem; margin: 1rem 0 0; }
  ol { padding-left: 1.25rem; }
  li { margin: 0 0 .6rem; }
  h2 { font-size: 1rem; margin: 2.5rem 0 .75rem; }
  code {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: .875em; background: var(--card); border: 1px solid var(--edge);
    border-radius: 4px; padding: .1em .35em;
  }
  .note {
    border-left: 3px solid var(--accent); padding: .1rem 0 .1rem 1rem;
    color: var(--muted); margin: 1.5rem 0;
  }
</style>
<main>
  <h1>Install the bookmarklet</h1>
  <p class="sub">Drag the button onto your bookmarks bar. That is the whole install.</p>

  <div class="drop">
    <a class="grab" href="__URL__">__TITLE__</a>
    <p>Drag this button &mdash; not this file &mdash; onto the bar.</p>
  </div>

  <div class="note">
    Dragging <em>the file</em> from your file manager onto the bar just
    bookmarks the file. It has to be the button above, dragged out of this
    page while the page is open in the browser.
  </div>

  <h2>If your bookmarks bar is hidden</h2>
  <ol>
    <li>Chrome and Edge: <code>Ctrl</code>+<code>Shift</code>+<code>B</code>
      (<code>&#8984;</code>+<code>Shift</code>+<code>B</code> on a Mac).</li>
    <li>Firefox: right-click the toolbar &rarr; <em>Bookmarks Toolbar</em>
      &rarr; <em>Always Show</em>.</li>
    <li>Safari: <em>View</em> &rarr; <em>Show Favourites Bar</em>.</li>
  </ol>

  <h2>If dragging won't work</h2>
  <ol>
    <li>Import <code>__BOOKMARK_FILE__</code> instead, from the same folder:
      in Chrome and Edge via the bookmark manager's &#8942; menu &rarr;
      <em>Import bookmarks</em>; in Firefox via <em>Manage bookmarks</em>
      &rarr; <em>Import and Backup</em>; in Safari via <em>File</em> &rarr;
      <em>Import From</em>. Pick the HTML-file option, not the
      import-from-another-browser one.</li>
    <li>Or add a bookmark by hand and paste the contents of
      <code>__TXT_FILE__</code> into its <strong>URL</strong> field. It has to
      go in through the bookmark editor: browsers strip <code>javascript:</code>
      pasted into the address bar.</li>
  </ol>

  <h2>Then</h2>
  <p>Open a listing on Booking.com, Sykes, cottages.com or Hoseasons and click
     the bookmark. It copies what it found to your clipboard as JSON. Paste
     that at the <code>stays&gt;</code> prompt.</p>
</main>
</html>
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

    safe_url = html.escape(url, quote=True)
    INSTALL_OUT.write_text(
        INSTALL_PAGE
        .replace("__URL__", safe_url)
        .replace("__TITLE__", html.escape(config.BOOKMARKLET_TITLE))
        .replace("__BOOKMARK_FILE__", html.escape(HTML_OUT.name))
        .replace("__TXT_FILE__", html.escape(OUT.name)),
        encoding="utf-8",
    )

    print(f"source   {len(SRC.read_text()):>7,} bytes")
    print(f"stripped {len(code):>7,} bytes")
    print(f"encoded  {len(url):>7,} bytes")
    if len(url) > config.BOOKMARKLET_MAX_BYTES:
        print("warning: some browsers baulk at bookmarks this long")

    print(f"""
Wrote {OUT.name}, {HTML_OUT.name}, {INSTALL_OUT.name}

To install, open this in your browser:

  {INSTALL_OUT}

and drag the button off the page onto your bookmarks bar. Dragging the *file*
onto the bar only bookmarks the file — it has to be the button, out of the
opened page. The page explains the other two routes if that won't do.""")


if __name__ == "__main__":
    main()
