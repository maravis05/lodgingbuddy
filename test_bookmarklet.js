/*
 * Tests the bookmarklet's extractor against saved page HTML, so the logic is
 * verified before it ever goes in a browser bookmark.
 *
 *   node test_bookmarklet.js <saved.html> <original-url>
 *
 * Builds the same context object the browser would, but from a file: pulls the
 * ld+json and __NEXT_DATA__ blobs out with regexes, and approximates innerText
 * by stripping tags.
 */

const fs = require("fs");
const { extract } = require("./bookmarklet.js");

function contextFromFile(path, url) {
  const html = fs.readFileSync(path, "utf8");

  const jsonld = [];
  const ldRe = /<script[^>]*application\/ld\+json[^>]*>([\s\S]*?)<\/script>/g;
  let m;
  while ((m = ldRe.exec(html)) !== null) {
    try { jsonld.push(JSON.parse(m[1])); } catch (e) { /* skip */ }
  }

  let next = null;
  const nd = /<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/.exec(html);
  if (nd) { try { next = JSON.parse(nd[1]); } catch (e) { /* skip */ } }

  const text = html
    .replace(/<script[\s\S]*?<\/script>/g, " ")
    .replace(/<style[\s\S]*?<\/style>/g, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&pound;/g, "£")
    .replace(/\s+/g, " ");

  return { url, host: new URL(url).hostname, jsonld, next, text,
           dom: domFromFile(html) };
}

// The browser hands `extract` a handful of DOM lookups it can't do itself.
// Approximating them here with regexes is what makes the Booking.com path —
// which has no JSON-LD and no __NEXT_DATA__ to read — testable at all: save
// the page from your browser with Ctrl-S and point this at it.
function domFromFile(html) {
  const strip = (s) => s.replace(/<[^>]+>/g, "\n").replace(/&amp;/g, "&")
    .replace(/&#39;|&apos;/g, "'").replace(/&quot;/g, '"').replace(/&nbsp;/g, " ")
    .split("\n").map((x) => x.trim()).filter(Boolean).join("\n").trim();

  // Non-greedy to the next same-tag close: nested markup inside one testid
  // block is exactly what we want, and blocks don't nest into each other.
  const blocks = (testid, cap = 40) => {
    const re = new RegExp(
      `<(\\w+)[^>]*data-testid="${testid}"[^>]*>([\\s\\S]*?)</\\1>`, "g");
    const out = [];
    let m;
    while ((m = re.exec(html)) !== null && out.length < cap) {
      const t = strip(m[2]);
      if (t && !out.includes(t)) out.push(t);
    }
    return out;
  };
  const first = (testid) => blocks(testid, 1)[0] || null;

  const latlng = /data-atlas-latlng="([^"]+)"/.exec(html);
  // Each <li> inside the facilities wrappers, which is what querySelectorAll
  // returns in the browser.
  const items = [];
  for (const wrapper of blocks("property-most-popular-facilities-wrapper", 4)
    .concat(blocks("facility-group-container", 8))) {
    for (const line of wrapper.split("\n")) {
      if (line && line.length < 120 && !items.includes(line)) items.push(line);
    }
  }

  // The description block, by whichever hook this saved page happens to use.
  // Same non-greedy caveat as `blocks`, and it bites harder here: a write-up
  // wrapped in nested <div>s stops at the first close tag, so the text this
  // harness reports can be shorter than what a browser would send.
  const byAttr = (attr, value) => {
    const m = new RegExp(
      `<(\\w+)[^>]*${attr}="[^"]*${value}[^"]*"[^>]*>([\\s\\S]*?)</\\1>`, "i")
      .exec(html);
    return m ? strip(m[2]) : null;
  };
  const meta = /<meta[^>]+name="description"[^>]+content="([^"]*)"/i.exec(html);

  return {
    latlng: latlng ? latlng[1] : null,
    description: first("property-description") ||
                 byAttr("id", "property_description_content") ||
                 byAttr("class", "hp_desc_main_content"),
    meta: meta ? meta[1] : null,
    address: first("address"),
    score: first("review-score-component") || first("review-score-right-component"),
    subscores: blocks("review-subscore", 12),
    facilities: items.slice(0, 40),
    beds: blocks("bed-type-name", 12).concat(blocks("bed-type-configuration", 12))
  };
}

const [, , path, url] = process.argv;
if (!path || !url) {
  console.error("usage: node test_bookmarklet.js <saved.html> <original-url>");
  process.exit(2);
}
const rec = extract(contextFromFile(path, url));
const filled = Object.entries(rec).filter(([, v]) =>
  v !== null && !(Array.isArray(v) && v.length === 0));
console.log(JSON.stringify(Object.fromEntries(filled), null, 2));
