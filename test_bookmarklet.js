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
const { extract, parseRooms, parseCharges, pricedUnit } = require("./bookmarklet.js");

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
  {
    // Booking.com's wording, which "Max people" doesn't reach.
    name: "sleeps stated with a colon",
    text: "One-Bedroom Apartment\nRecommended for 3 adults\nSleeps: 3 adults\n" +
          "Bedroom 1: 1 full bed\nLiving room: 1 sofa bed\n",
    want: { bedrooms: 1, bathrooms: null, sleeps: 3,
            beds: [["Bedroom 1", 1, "full", true],
                   ["Living room", 1, "sofa", false]] }
  },
  {
    // Same rule as the bathroom count: no colon, no number.
    name: "sleeps in prose is not a capacity",
    text: "A bright apartment that sleeps 4 in comfort.\nFree Wifi\n",
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

// Which text the charges get read *from*, which is the half the cases above
// can't reach: they hand parseCharges the right string themselves. So all four
// passed while a real capture of the listing they were written from came back
// with no taxes at all — the room selectors had landed on the short nodes
// nested inside the block ("Private kitchen"), and a fragment of the block
// reads exactly like a block that stated nothing.
const SOURCE_CASES = [
  {
    name: "charges read off the room block",
    rooms: "Entire apartment\nIncluded: £78 Cleaning fee per stay\n" +
           "Excluded: 20 % VAT, 5 % City tax",
    text: "Royal Mile Apartment\nIncluded: £78 Cleaning fee per stay\n" +
          "Excluded: 20 % VAT, 5 % City tax",
    want: { taxes: 2, fee: 78 }
  },
  {
    name: "room block is a fragment, so the page text answers",
    rooms: "Private kitchen\nPrivate bathroom",
    text: "Royal Mile Apartment\nIncluded: £78 Cleaning fee per stay\n" +
          "Excluded: 20 % VAT, 5 % City tax",
    want: { taxes: 2, fee: 78 }
  },
  {
    name: "neither states them, so nothing is invented",
    rooms: "Private kitchen\nPrivate bathroom",
    text: "Royal Mile Apartment\nFree cancellation before September 9, 2026",
    want: { taxes: 0, fee: null }
  },
];

// Dragon Suites at Rutland Square, trimmed but in the page's own order and
// wording: four apartment types in one table, two rate plans each, and the
// two-bedroom one listed last. Read whole it describes no apartment that
// exists — Rowand's bedroom and living room, Elliot's second bedroom and
// bathroom count — and the capture claimed two bedrooms and two bathrooms at
// the price of the one-bedroom suite.
const MULTI_UNIT_TABLE = [
  "Book this apartment",
  "Apartment Type    Today's Price    Your choices    Select an Apartment",
  "Rowand Suite",
  "Recommended for 3 adults",
  "Sleeps: 3 adults",
  "Bedroom 1: 1 queen bed ",
  "Living room: 1 sofa bed ",
  "Entire apartment702 feet²Private kitchenAttached bathroomPatioFree Wifi",
  "$295 per night",
  "$1,123 $1,013 Original price $1,123 Current price $1,013",
  "3 nights",
  "Included: £95 Cleaning fee per stay",
  "Excluded: 20 % VAT, 5 % City tax",
  "Non-refundable",
  "$344 per night",
  "$1,288 $1,161 Original price $1,288 Current price $1,161",
  "Included: £95 Cleaning fee per stay",
  "Excluded: 20 % VAT, 5 % City tax",
  "Free cancellation before October 4, 2026",
  "Kerr Suite",
  "Sleeps: 3 adults",
  "Bedroom 1: 1 queen bed ",
  "Living room: 1 sofa bed ",
  "Entire apartment626 feet²Private kitchenAttached bathroomFree Wifi",
  "$326 per night",
  "$1,228 $1,107 Original price $1,228 Current price $1,107",
  "Included: £95 Cleaning fee per stay",
  "Excluded: 20 % VAT, 5 % City tax",
  "$381 per night",
  "$1,411 $1,270 Original price $1,411 Current price $1,270",
  "Elliot Suite",
  "Sleeps: 3 adults",
  "Bedroom 1: 1 queen bed ",
  "Bedroom 2: 1 queen bed ",
  "Bathrooms:2 ",
  "Entire apartment924 feet²Private kitchenAttached bathroomGarden view",
  "Biggest Apartment Available",
  "$382 per night",
  "$1,416 $1,275 Original price $1,416 Current price $1,275",
  "Included: £95 Cleaning fee per stay",
  "Excluded: 20 % VAT, 5 % City tax",
  "$446 per night",
  "$1,631 $1,466 Original price $1,631 Current price $1,466",
].join("\n");

// The same table with the cheap suite listed last, so a pass that picks the
// first unit rather than the priced one can't hold. Elliot's block moves to
// the top; the answer must not move with it.
const MULTI_UNIT_REORDERED = (() => {
  const at = MULTI_UNIT_TABLE.indexOf("Elliot Suite");
  return MULTI_UNIT_TABLE.slice(at) + "\n" + MULTI_UNIT_TABLE.slice(0, at);
})();

const UNIT_CASES = [
  {
    name: "four apartments in one table, priced one read alone",
    rooms: MULTI_UNIT_TABLE,
    want: { bedrooms: 1, bathrooms: null, sleeps: 3, rooms: 1,
            beds: [["Bedroom 1", 1, "queen", true],
                   ["Living room", 1, "sofa", false]],
            cheapest: 1013 }
  },
  {
    name: "cheapest apartment listed last, still the one read",
    rooms: MULTI_UNIT_REORDERED,
    want: { bedrooms: 1, bathrooms: null, sleeps: 3, rooms: 1,
            beds: [["Bedroom 1", 1, "queen", true],
                   ["Living room", 1, "sofa", false]],
            cheapest: 1013 }
  },
  {
    // The shape every earlier capture had, and the one that must not change:
    // one apartment, one capacity line, nothing to choose between.
    name: "single apartment is left whole",
    rooms: "One-Bedroom Apartment\nSleeps: 3 adults\n" +
           "Bedroom 1: 1 queen bed \nLiving room: 1 sofa bed \nBathrooms:1 \n" +
           "$295 per night\n$1,123 $1,013 Original price $1,123 Current price $1,013\n" +
           "Included: £95 Cleaning fee per stay\nExcluded: 20 % VAT, 5 % City tax",
    want: { bedrooms: 1, bathrooms: 1, sleeps: 3, rooms: 1,
            beds: [["Bedroom 1", 1, "queen", true],
                   ["Living room", 1, "sofa", false]],
            cheapest: 1013 }
  },
  {
    // Two rooms asked for, two rooms needed: the units sleep two and the party
    // is three, so nothing here says the search's answer was wrong.
    name: "party doesn't fit one unit, so the room count stands",
    rooms: "Double Room\nSleeps: 2 adults\nBedroom 1: 1 queen bed \n" +
           "$150 per night\nCurrent price $450\n" +
           "Twin Room\nSleeps: 2 adults\nBedroom 1: 2 single beds \n" +
           "$160 per night\nCurrent price $480",
    want: { bedrooms: 1, bathrooms: null, sleeps: 2, rooms: 2,
            beds: [["Bedroom 1", 1, "queen", true]],
            cheapest: 450 }
  },
];

// The listing the arithmetic in b5298a2 was checked against: 673.24 pre-tax,
// £828.00 at checkout once the stated rates and the untaxed fee are applied.
const BOOKING_URL =
  "https://www.booking.com/hotel/gb/royal-mile-apartment-edinburgh-edinburgh.html" +
  "?checkin=2026-10-09&checkout=2026-10-12&group_adults=3&no_rooms=2" +
  "&sr_pri_blocks=1440389401_441029911_3_0_0__67324";

// What sr_pri_blocks buys, and how many rooms that is. Hotels are the reason
// this table exists: a whole-property rental is one block and its own capacity,
// and every one of these cases is a way that stops being true once the search
// has to ask for two rooms to fit the party.
const BASE = "https://www.booking.com/hotel/gb/dalmahoy.html" +
             "?checkin=2026-10-09&checkout=2026-10-12&group_adults=3";

// Two room types, priced one apiece, so `pricedUnit` has a choice to get right
// before the arithmetic below has anything to work on.
const HOTEL_ROOMS =
  "Classic Double Room\n1 queen bed\nMax. people: 2\n$134 per night\n" +
  "$485 $402 Original price $485 Current price $402\n" +
  "Excluded: 20 % VAT, 5 % City tax\n" +
  "Superior Double Room with 2 Double Beds\n2 full beds\nMax. people: 4\n" +
  "$197 per night\n$714 $591 Original price $714 Current price $591\n" +
  "Excluded: 20 % VAT, 5 % City tax";

const BLOCK_CASES = [
  {
    // The capture that started this: the same Classic Double twice, 296.66
    // each, which is the $402 block the priced-unit read lands on. Taking the
    // first alone halved the bill and left "2 rooms booked" over a one-room
    // price. Capacity doubles with it — two rooms holding two hold four, and
    // at two the party of three reads as not fitting.
    name: "same block twice is two rooms, and two rooms' price",
    url: BASE + "&no_rooms=2&sr_pri_blocks=" +
         "3686523_95159595_2_2_0__29666%2C3686523_95159595_2_2_0__29666",
    rooms: HOTEL_ROOMS,
    want: { native_price: 593.32, rooms: 2, sleeps: 4 }
  },
  {
    // Two different rooms still add up — the sum is the sum whatever is in it.
    // Capacity doesn't: the page was narrowed to the cheaper unit long before
    // this, so doubling its "Max. people" would be describing a room nobody
    // read. It stays the one figure we actually have.
    name: "two different blocks add up, but their capacity isn't guessed",
    url: BASE + "&no_rooms=2&sr_pri_blocks=" +
         "3686523_95159595_2_2_0__29666%2C3686523_95159599_4_2_0__43600",
    rooms: HOTEL_ROOMS,
    want: { native_price: 732.66, rooms: 2, sleeps: 2 }
  },
  {
    // One block, and a unit that holds all three: the search asked for two
    // rooms and was answered with one apartment, so the room count follows the
    // price rather than the question.
    name: "one block for a party that fits is one room",
    url: BASE + "&no_rooms=2&sr_pri_blocks=1440389401_441029911_3_0_0__67324",
    rooms: "Two-Bedroom Apartment\nMax. people: 3\nBedroom 1: 1 queen bed\n" +
           "$295 per night\nCurrent price $1,013",
    want: { native_price: 673.24, rooms: 1, sleeps: 3 }
  },
  {
    // A four-figure minor-unit tail that works out at £4 is a supplement, not
    // a stay. Checked per block, so one bad entry sinks the sum rather than
    // being quietly averaged into it.
    name: "a block too small to be a stay is not a price",
    url: BASE + "&no_rooms=2&sr_pri_blocks=" +
         "3686523_95159595_2_2_0__29666%2C3686523_95159595_2_2_0__400",
    rooms: HOTEL_ROOMS,
    want: { native_price: null, rooms: 2, sleeps: 2 }
  },
];

function checkBlocks() {
  let failed = 0;
  for (const c of BLOCK_CASES) {
    const rec = extract({
      url: c.url, host: "www.booking.com", jsonld: [], next: null,
      text: c.rooms, dom: { rooms: c.rooms }
    });
    const problems = [];
    // `?? null` because a page with no usable block never sets native_price at
    // all, and "the key isn't there" is the same answer as "no price" to
    // everything downstream — the Python side only copies fields it is given.
    for (const k of ["native_price", "rooms", "sleeps"]) {
      const got = rec[k] ?? null;
      if (got !== c.want[k]) problems.push(`${k}: got ${got}, want ${c.want[k]}`);
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

// Through `extract` rather than against `pricedUnit` directly, because the
// half that went wrong is the wiring: parseRooms was right about the text it
// was handed and the text it was handed was four apartments at once.
function checkUnits() {
  let failed = 0;
  for (const c of UNIT_CASES) {
    const rec = extract({
      url: BOOKING_URL, host: "www.booking.com", jsonld: [], next: null,
      text: c.rooms, dom: { rooms: c.rooms }
    });
    const problems = [];
    for (const k of ["bedrooms", "bathrooms", "sleeps", "rooms"]) {
      if (rec[k] !== c.want[k]) problems.push(`${k}: got ${rec[k]}, want ${c.want[k]}`);
    }
    const flat = (rec.beds || []).map(b => [b.room, b.count, b.type, b.private]);
    if (JSON.stringify(flat) !== JSON.stringify(c.want.beds)) {
      problems.push(`beds:\n      got  ${JSON.stringify(flat)}\n      want ${JSON.stringify(c.want.beds)}`);
    }
    // The price this capture actually took is the cheapest on the page, so the
    // candidate list has to reach it — the failure was a list of eight that
    // stopped short of the only figure the record went on to use.
    const cheapest = Math.min(...rec.price_candidates);
    if (cheapest !== c.want.cheapest) {
      problems.push(`cheapest candidate: got ${cheapest}, want ${c.want.cheapest}`);
    }
    // Struck-through originals are not prices anybody is charged.
    const originals = rec.price_candidates.filter(v => [1123, 1288, 1228, 1411, 1416, 1631].includes(v));
    if (originals.length) {
      problems.push(`pre-discount amounts offered as prices: ${originals.join(", ")}`);
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

function checkSources() {
  let failed = 0;
  for (const c of SOURCE_CASES) {
    const rec = extract({
      url: BOOKING_URL, host: "www.booking.com", jsonld: [], next: null,
      text: c.text, dom: { rooms: c.rooms }
    });
    const problems = [];
    const taxes = (rec.taxes || []).length;
    const fee = rec.fees_included ? rec.fees_included[0].amount : null;
    if (taxes !== c.want.taxes) {
      problems.push(`taxes: got ${taxes}, want ${c.want.taxes}`);
    }
    if (fee !== c.want.fee) {
      problems.push(`fee: got ${fee}, want ${c.want.fee}`);
    }
    // Reading them is only worth anything against a price, so check it came
    // through the same capture rather than testing the rates in the abstract.
    if (rec.native_price !== 673.24) {
      problems.push(`native_price: got ${rec.native_price}, want 673.24`);
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
  const failed = checkRooms() + checkCharges() + checkSources() + checkUnits() +
                 checkBlocks();
  const total = CASES.length + CHARGE_CASES.length + SOURCE_CASES.length +
                UNIT_CASES.length + BLOCK_CASES.length;
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
