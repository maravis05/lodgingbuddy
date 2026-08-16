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
const { extract, parseRooms, parseCharges } = require("./bookmarklet.js");

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

// ── unit checks ────────────────────────────────────────────────────────────
//
// Run with no arguments. These need no saved page, so they cost nothing and
// run anywhere — which matters for parseRooms, whose whole job is reading a
// text shape that differs between a browser and this harness.

const CASES = [
  {
    name: "one bedroom, sofa bed in the living room",
    text: "One-Bedroom Apartment\nNumber of guests: \nMax. people: 3\n" +
          "Bedroom 1: 1 full bed \nLiving room: 1 sofa bed \n",
    want: { bedrooms: 1, bathrooms: null, sleeps: 3,
            beds: [["Bedroom 1", 1, "full", true],
                   ["Living room", 1, "sofa", false]] }
  },
  {
    name: "two bedrooms, everyone behind a door",
    text: "Two-Bedroom Apartment\nNumber of guests: \nMax. people: 3\n" +
          "Bedroom 1: 1 queen bed \nBedroom 2: 1 bunk bed \nBathrooms:2 \n",
    want: { bedrooms: 2, bathrooms: 2, sleeps: 3,
            beds: [["Bedroom 1", 1, "queen", true],
                   ["Bedroom 2", 1, "bunk", true]] }
  },
  {
    // What this harness produces from saved HTML, and a shape some layouts
    // hand the browser too. Room labels have to be found by position, not by
    // reading to the end of a line that isn't there.
    name: "same block with the whitespace collapsed",
    text: "Two-Bedroom Apartment Number of guests: Max. people: 3 " +
          "Bedroom 1: 1 queen bed Bedroom 2: 1 bunk bed Bathrooms:2",
    want: { bedrooms: 2, bathrooms: 2, sleeps: 3,
            beds: [["Bedroom 1", 1, "queen", true],
                   ["Bedroom 2", 1, "bunk", true]] }
  },
  {
    name: "several beds in one room",
    text: "Bedroom 1: 1 double bed, 2 single beds\nMax. people: 4\n",
    want: { bedrooms: 1, bathrooms: null, sleeps: 4,
            beds: [["Bedroom 1", 1, "double", true],
                   ["Bedroom 1", 2, "single", true]] }
  },
  {
    name: "studio has no bedroom",
    text: "Studio Apartment\nMax. people: 2\nStudio: 1 sofa bed\n",
    want: { bedrooms: 0, bathrooms: null, sleeps: 2,
            beds: [["Studio", 1, "sofa", false]] }
  },
  {
    name: "heading count with no room breakdown",
    text: "Three-Bedroom Cottage\nMax. people: 6\n",
    want: { bedrooms: 3, bathrooms: null, sleeps: 6, beds: [] }
  },
  {
    // "Private bathroom" is a facility, not a count, and the write-up saying
    // "two bathrooms" is prose. Only the colon-and-digit form is a number.
    name: "facilities list is not a bathroom count",
    text: "Free Wifi\nPrivate bathroom\nKitchen\nThe apartment has two bathrooms.\n",
    want: { bedrooms: null, bathrooms: null, sleeps: null, beds: [] }
  },
];

// What the quoted price leaves out. The rates matter to the penny — these are
// what let the total be worked out without clicking through to book.
const CHARGE_CASES = [
  {
    name: "taxes only, no fee",
    text: "3 nights\nExcluded: 20 % VAT, 5 % City tax\nFree cancellation before September 9, 2026",
    want: { taxes: [["VAT", 0.20], ["City tax", 0.05]], fees: null }
  },
  {
    name: "included cleaning fee in property currency",
    text: "3 nights\nIncluded: £78 Cleaning fee per stay\n" +
          "Excluded: 20 % VAT, 5 % City tax\nFree cancellation",
    want: { taxes: [["VAT", 0.20], ["City tax", 0.05]],
            fees: [["Cleaning fee", 78, "GBP"]] }
  },
  {
    // No line breaks to stop a label, so it has to stop itself.
    name: "same block collapsed onto one line",
    text: "3 nights Included: £78 Cleaning fee per stay " +
          "Excluded: 20 % VAT, 5 % City tax Free cancellation before September",
    want: { taxes: [["VAT", 0.20], ["City tax", 0.05]],
            fees: [["Cleaning fee", 78, "GBP"]] }
  },
  {
    name: "page states neither",
    text: "3 nights\nFree cancellation before September 9, 2026\nPay nothing until",
    want: { taxes: null, fees: null }
  },
];

function checkCharges() {
  let failed = 0;
  for (const c of CHARGE_CASES) {
    const got = parseCharges(c.text);
    const taxes = got.taxes && got.taxes.map(t => [t.label, t.rate]);
    const fees = got.fees_included &&
      got.fees_included.map(f => [f.label, f.amount, f.currency]);
    const problems = [];
    if (JSON.stringify(taxes) !== JSON.stringify(c.want.taxes)) {
      problems.push(`taxes: got ${JSON.stringify(taxes)}, want ${JSON.stringify(c.want.taxes)}`);
    }
    if (JSON.stringify(fees) !== JSON.stringify(c.want.fees)) {
      problems.push(`fees:  got ${JSON.stringify(fees)}, want ${JSON.stringify(c.want.fees)}`);
    }
    if (problems.length) {
      failed++;
      console.log(`FAIL  ${c.name}`);
      for (const p of problems) console.log(`      ${p}`);
    } else {
      console.log(`ok    ${c.name}`);
    }
  }
  return failed;
}

function checkRooms() {
  let failed = 0;
  for (const c of CASES) {
    const got = parseRooms(c.text);
    const flat = got.beds.map(b => [b.room, b.count, b.type, b.private]);
    const problems = [];
    for (const k of ["bedrooms", "bathrooms", "sleeps"]) {
      if (got[k] !== c.want[k]) problems.push(`${k}: got ${got[k]}, want ${c.want[k]}`);
    }
    if (JSON.stringify(flat) !== JSON.stringify(c.want.beds)) {
      problems.push(`beds:\n      got  ${JSON.stringify(flat)}\n      want ${JSON.stringify(c.want.beds)}`);
    }
    if (problems.length) {
      failed++;
      console.log(`FAIL  ${c.name}`);
      for (const p of problems) console.log(`      ${p}`);
    } else {
      console.log(`ok    ${c.name}`);
    }
  }
  return failed;
}

const [, , path, url] = process.argv;
if (!path && !url) {
  const failed = checkRooms() + checkCharges();
  const total = CASES.length + CHARGE_CASES.length;
  console.log(failed ? `\n${failed} failed` : `\n${total} passed`);
  process.exit(failed ? 1 : 0);
}
if (!path || !url) {
  console.error("usage: node test_bookmarklet.js [<saved.html> <original-url>]");
  console.error("       with no arguments, runs the unit checks");
  process.exit(2);
}
const rec = extract(contextFromFile(path, url));
const filled = Object.entries(rec).filter(([, v]) =>
  v !== null && !(Array.isArray(v) && v.length === 0));
console.log(JSON.stringify(Object.fromEntries(filled), null, 2));
