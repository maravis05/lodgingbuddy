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

  return { url, host: new URL(url).hostname, jsonld, next, text };
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
