/*
 * lodgingbuddy bookmarklet — run this on a listing page you're looking at.
 *
 * Why this exists: Claude Code runs on a headless box over SSH, while Chrome
 * runs on Windows. The Chrome extension can't bridge that (it uses Native
 * Messaging, which is same-machine only). But the browser has already rendered
 * the page and already passed any bot wall — so the data is sitting right
 * there. This pulls the fields out and copies them, as JSON, to the clipboard.
 * Paste that at the stays> prompt.
 *
 * Build with:  python3 build_bookmarklet.py   (`python` on Windows)
 * Then import bookmarklet.html, or drag its link onto the bookmarks bar.
 *
 * The extractor is split from the browser plumbing so it can be tested against
 * saved HTML under node — see test_bookmarklet.js.
 */

(function () {
  "use strict";

  // ── page accessors (the only part that needs a real browser) ──────────────

  // Booking.com renders nothing useful into JSON-LD and obfuscates its class
  // names, but a handful of data-testid hooks and the map's own latlng
  // attribute have stayed put for years. Everything the DOM knows is collected
  // here, as plain strings, so `extract` stays testable without a browser.
  function domFacts() {
    function attr(sel, name) {
      var el = document.querySelector(sel);
      return el ? el.getAttribute(name) : null;
    }
    function text(sel) {
      var el = document.querySelector(sel);
      return el ? (el.innerText || "").trim() : null;
    }
    function texts(sel, cap) {
      var out = [], nodes = document.querySelectorAll(sel);
      for (var i = 0; i < nodes.length && out.length < (cap || 60); i++) {
        var t = (nodes[i].innerText || "").trim();
        if (t && t.length < 120 && out.indexOf(t) === -1) out.push(t);
      }
      return out;
    }
    return {
      // The map pin, which is the property's actual position rather than the
      // town it files itself under.
      latlng: attr("[data-atlas-latlng]", "data-atlas-latlng"),
      // Booking.com's write-up, which lives nowhere but the rendered page.
      description: text('[data-testid="property-description"]') ||
                   text("#property_description_content") ||
                   text(".hp_desc_main_content"),
      // Last resort, and true of every site: the page's own meta description.
      // Only a couple of sentences, but they are about this property, which
      // beats the nothing we'd otherwise send.
      meta: attr('meta[name="description"]', "content"),
      address: text('[data-testid="address"]') || text(".hp_address_subtitle") ||
               text("#hotel_address"),
      score: text('[data-testid="review-score-component"]') ||
             text('[data-testid="review-score-right-component"]'),
      subscores: texts('[data-testid="review-subscore"]', 12),
      facilities: texts('[data-testid="property-most-popular-facilities-wrapper"] li,' +
                        '[data-testid="facility-group-container"] li,' +
                        '[data-testid="property-highlights"] li', 40),
      beds: texts('[data-testid="bed-type-name"],' +
                  '[data-testid="bed-type-configuration"] li', 12)
    };
  }

  function browserContext() {
    var lds = [];
    var nodes = document.querySelectorAll('script[type="application/ld+json"]');
    for (var i = 0; i < nodes.length; i++) {
      try { lds.push(JSON.parse(nodes[i].textContent)); } catch (e) { /* skip */ }
    }
    var nd = null;
    var el = document.getElementById("__NEXT_DATA__");
    if (el) { try { nd = JSON.parse(el.textContent); } catch (e) { /* skip */ } }
    var dom = {};
    try { dom = domFacts(); } catch (e) { /* a page we don't know: no loss */ }
    return {
      url: location.href,
      host: location.hostname,
      jsonld: lds,
      next: nd,
      dom: dom,
      text: document.body ? document.body.innerText : ""
    };
  }

  // ── shared extraction ────────────────────────────────────────────────────
  function flatten(lds) {
    var out = [];
    for (var i = 0; i < lds.length; i++) {
      var d = lds[i];
      var arr = Object.prototype.toString.call(d) === "[object Array]" ? d : [d];
      for (var j = 0; j < arr.length; j++) {
        if (arr[j] && typeof arr[j] === "object") out.push(arr[j]);
      }
    }
    return out;
  }

  // Every distinct money amount on the page, biggest first, plus whichever
  // currency symbol dominates. Not £-only: Booking.com renders in the viewer's
  // chosen currency, so a US account sees US$ and a pound-only match finds
  // nothing at all.
  var SYMBOLS = [
    ["US$", "USD"], ["C$", "CAD"], ["A$", "AUD"],
    ["£", "GBP"], ["€", "EUR"], ["$", "USD"]
  ];

  // Amounts sitting in marketing copy are not prices. cottages.com runs a
  // "chance to win £500" newsletter draw on every property page, which
  // otherwise reads as that cottage costing £500.
  var NOT_A_PRICE = /win|prize|voucher|draw|newsletter|sign\s?up|gift\s?card|competition|terms and conditions|save up to|discount code/i;

  function moneyAmounts(text) {
    var seen = {}, out = [], counts = {};
    // Thousands separators must be followed by exactly three digits, so a
    // European "€412,50" reads as 412.50 rather than forty-one thousand.
    var re = /(US\$|C\$|A\$|£|€|\$)\s?(\d{1,3}(?:,\d{3})*|\d+)(?:[.,](\d{2}))?/g, m;
    while ((m = re.exec(text)) !== null) {
      // Judge each amount by the words around it.
      var near = text.slice(Math.max(0, m.index - 90), m.index + 90);
      if (NOT_A_PRICE.test(near)) continue;
      var sym = m[1], cur = "GBP";
      for (var i = 0; i < SYMBOLS.length; i++) {
        if (SYMBOLS[i][0] === sym) { cur = SYMBOLS[i][1]; break; }
      }
      counts[cur] = (counts[cur] || 0) + 1;
      var v = parseFloat(m[2].replace(/,/g, "") + (m[3] ? "." + m[3] : ""));
      if (v >= 20 && v <= 100000 && !seen[v]) { seen[v] = 1; out.push(v); }
    }
    var best = null;
    for (var c in counts) if (!best || counts[c] > counts[best]) best = c;
    return {
      currency: best,
      values: out.sort(function (a, b) { return b - a; }).slice(0, 8)
    };
  }

  // Backwards-compatible alias used by the tests.
  function poundAmounts(text) { return moneyAmounts(text).values; }

  // ── the write-up ─────────────────────────────────────────────────────────

  // The listing's own prose is the part of a page no schema has a column for —
  // which bed is the sofa bed, how steep the track is, whether the sea view is
  // from the kitchen or the car park. It's sent verbatim; nothing here tries to
  // understand it.
  //
  // The cap is not about the terminal — measured over a pty, a 32KB pasted
  // line arrives whole, because readline reads in raw mode rather than through
  // the 4096-byte canonical line buffer. It's a guard against a selector that
  // stops matching a description and starts matching the page: real write-ups
  // run 1,500-6,000 characters, so anything past this is markup that moved,
  // not prose. What it drops is recoverable — prose pasted at the prompt
  // appends to what's already there.
  var SUMMARY_MAX = 12000;

  var ENTITIES = [
    [/&nbsp;/g, " "], [/&amp;/g, "&"], [/&lt;/g, "<"], [/&gt;/g, ">"],
    [/&quot;/g, '"'], [/&#0?39;|&apos;|&rsquo;|&#8217;/g, "'"],
    [/&ndash;|&#8211;/g, "–"], [/&mdash;|&#8212;/g, "—"], [/&hellip;/g, "…"]
  ];

  function stripTags(s) {
    var out = String(s)
      .replace(/<\s*br\s*\/?>/gi, "\n")
      .replace(/<\/\s*(p|div|li|h\d)\s*>/gi, "\n")
      .replace(/<[^>]+>/g, " ");
    for (var i = 0; i < ENTITIES.length; i++) {
      out = out.replace(ENTITIES[i][0], ENTITIES[i][1]);
    }
    return out;
  }

  // A description arrives as a string, as HTML, as a list of paragraphs, or as
  // titled sections — which one depends on the site and sometimes on which of
  // its templates rendered the page. All of them come out as plain paragraphs.
  var PROSE_KEYS = ["title", "heading", "name", "label",
                    "body", "text", "content", "description", "value"];

  function asProse(raw, depth) {
    depth = depth || 0;
    if (raw == null || depth > 3) return "";
    if (typeof raw === "string") return stripTags(raw);
    var parts = [];
    if (Object.prototype.toString.call(raw) === "[object Array]") {
      for (var i = 0; i < raw.length; i++) parts.push(asProse(raw[i], depth + 1));
    } else if (typeof raw === "object") {
      for (var k = 0; k < PROSE_KEYS.length; k++) {
        if (raw[PROSE_KEYS[k]] != null) {
          parts.push(asProse(raw[PROSE_KEYS[k]], depth + 1));
        }
      }
    }
    var out = [];
    for (var j = 0; j < parts.length; j++) {
      if (parts[j] && out.indexOf(parts[j]) === -1) out.push(parts[j]);
    }
    return out.join("\n");
  }

  // Chrome that sits inside the description block itself and says nothing about
  // the property — Booking.com puts its Genius banner in there. Matched at the
  // start of a line and dropped a line at a time, so a real sentence that
  // happens to mention one of these words survives.
  var NOT_PROSE = /^(you'?re eligible|genius|sign in|log in|show (more|less)|read more|translated automatically|book now|save \d+%)/i;

  function tidy(text) {
    var kept = String(text || "").replace(/\r/g, "").split("\n");
    for (var i = kept.length - 1; i >= 0; i--) {
      if (NOT_PROSE.test(kept[i].trim())) kept.splice(i, 1);
    }
    var s = kept.join("\n")
      .replace(/[ \t ]+/g, " ")
      .replace(/ ?\n ?/g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
    if (s.length <= SUMMARY_MAX) return s;
    // Cut at a sentence where there's one near the end, so the corpus doesn't
    // fill with half-words.
    var cut = s.slice(0, SUMMARY_MAX);
    var stop = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf("\n"));
    return (stop > SUMMARY_MAX * 0.6 ? cut.slice(0, stop + 1) : cut).trim() + " …";
  }

  function summarise(raw) { return tidy(asProse(raw)) || null; }

  // ── reading the things that aren't prices ────────────────────────────────

  // "1 double bed", "2 single beds", "Bunk bed" — the answer to how much space
  // there really is. Sleeps-4 counts a sofa bed the same as a double behind a
  // door, and only one of those settles who sleeps where.
  var BED_KINDS = "single|double|twin|queen|king|sofa|bunk|futon|full";

  function parseBeds(lines) {
    var out = [], seen = {};
    var re = new RegExp("(\\d+)?\\s*(" + BED_KINDS + ")[\\s-]*bed", "gi");
    for (var i = 0; i < lines.length; i++) {
      var m;
      re.lastIndex = 0;
      while ((m = re.exec(lines[i])) !== null) {
        var kind = m[2].toLowerCase();
        var count = m[1] ? parseInt(m[1], 10) : 1;
        // The same bed listed twice in two panels is one bed, so the larger
        // count wins rather than the two being added together.
        if (seen[kind]) { seen[kind].count = Math.max(seen[kind].count, count); }
        else { seen[kind] = { type: kind, count: count }; out.push(seen[kind]); }
      }
    }
    return out;
  }

  // Booking.com breaks its rating into categories — Cleanliness, Location,
  // Value for money — each rendered as a label and a number. Cleanliness is
  // the one worth having: it's the factor people notice and photos hide.
  function parseSubscores(lines) {
    var out = {}, any = false;
    for (var i = 0; i < lines.length; i++) {
      var m = /^([A-Za-z][A-Za-z \-/]{2,24}?)\s*[\n:]?\s*(\d{1,2}(?:[.,]\d)?)\s*$/
        .exec(lines[i].replace(/\s+/g, " ").trim());
      if (!m) continue;
      var key = m[1].toLowerCase().replace(/[^a-z]+/g, "_").replace(/^_|_$/g, "");
      var val = parseFloat(m[2].replace(",", "."));
      if (key && val >= 0 && val <= 10) { out[key] = val; any = true; }
    }
    return any ? out : null;
  }

  function parseScore(text) {
    if (!text) return null;
    // "Scored 8.6", "8.6 Excellent", or the bare number on its own line.
    var m = /(\d{1,2}(?:[.,]\d)?)\s*(?:\/\s*10)?/.exec(text.replace(/\s+/g, " "));
    if (!m) return null;
    var v = parseFloat(m[1].replace(",", "."));
    return v >= 0 && v <= 10 ? v : null;
  }

  function parseReviews(text) {
    var m = /([\d,]{1,9})\s*(?:genuine\s+)?reviews?/i.exec(text || "");
    return m ? parseInt(m[1].replace(/,/g, ""), 10) : null;
  }

  function parseLatLng(raw, rec) {
    if (!raw) return;
    var p = String(raw).split(",");
    var lat = parseFloat(p[0]), lon = parseFloat(p[1]);
    if (!isNaN(lat) && !isNaN(lon)) { rec.latitude = lat; rec.longitude = lon; }
  }

  function extract(ctx) {
    var rec = {
      source: null, url: (ctx.url || "").split("#")[0], code: null, name: null,
      location: null, region: null, nights: null, adults: null, rooms: null,
      sleeps: null, bedrooms: null, bathrooms: null, score: null, reviews: null,
      checkin: null, checkout: null,
      // Amenities go over as the site's own wording. Normalising them into
      // slugs is Python's job, so the alias table lives in exactly one place
      // rather than being kept in step across two languages.
      amenities: null, beds: null, rooms_total: null, subscores: null,
      address: null, latitude: null, longitude: null, summary: null,
      price: null, currency: "GBP", price_basis: null, price_candidates: []
    };
    var host = ctx.host || "";
    var dom = ctx.dom || {};

    // Sykes — schema.org VacationRental, the richest of the four.
    if (host.indexOf("sykescottages") !== -1) {
      rec.source = "sykes";
      var objs = flatten(ctx.jsonld);
      for (var i = 0; i < objs.length; i++) {
        var o = objs[i];
        if (["VacationRental", "LodgingBusiness", "Accommodation"].indexOf(o["@type"]) === -1) continue;
        rec.name = o.name || rec.name;
        rec.code = o.identifier ? String(o.identifier) : rec.code;
        if (o.address && o.address.streetAddress) rec.location = o.address.streetAddress;
        rec.summary = summarise(o.description) || rec.summary;
        if (o.aggregateRating) {
          rec.score = o.aggregateRating.ratingValue;
          rec.reviews = o.aggregateRating.reviewCount || o.aggregateRating.ratingCount;
        }
        if (o.latitude != null && o.longitude != null) {
          rec.latitude = o.latitude; rec.longitude = o.longitude;
        }
        var p = o.containsPlace;
        if (p) {
          rec.summary = rec.summary || summarise(p.description);
          if (p.occupancy && p.occupancy.value) rec.sleeps = p.occupancy.value;
          rec.bedrooms = p.numberOfBedrooms != null ? p.numberOfBedrooms : rec.bedrooms;
          rec.bathrooms = p.numberOfBathroomsTotal != null ? p.numberOfBathroomsTotal : rec.bathrooms;
          rec.rooms_total = p.numberOfRooms != null ? p.numberOfRooms : rec.rooms_total;
          if (p.amenityFeature && p.amenityFeature.length) {
            var feats = [];
            for (var k = 0; k < p.amenityFeature.length; k++) {
              var f = p.amenityFeature[k];
              if (f && f.value !== false && f.name) feats.push(String(f.name));
            }
            if (feats.length) rec.amenities = feats;
          }
          var bedList = p.bed;
          if (bedList) {
            if (Object.prototype.toString.call(bedList) !== "[object Array]") bedList = [bedList];
            var beds = [];
            for (var b = 0; b < bedList.length; b++) {
              var kind = bedList[b] && bedList[b].typeOfBed;
              if (kind && kind.name) kind = kind.name;
              if (!kind) continue;
              beds.push({
                type: String(kind).toLowerCase().replace(/\s*bed$/, "").trim(),
                count: parseInt(bedList[b].numberOfBeds, 10) || 1
              });
            }
            if (beds.length) rec.beds = beds;
          }
          var off = p.offers;
          if (Object.prototype.toString.call(off) === "[object Array]") off = off[0];
          if (off && off.price != null) {
            rec.price = parseFloat(off.price);
            rec.currency = off.priceCurrency || rec.currency;
            // Sykes tags its headline rate unitText:"from". That figure does
            // not track the dates on screen — "from £1090" can bill at £582 —
            // so pass the distinction along instead of implying a quote.
            var spec = off.priceSpecification || {};
            var unit = String(spec.unitText || "").toLowerCase();
            rec.price_basis = unit === "from" ? "indicative" : "quoted";
          }
        }
        break;
      }
      var dm = /[#&]duration=(\d+)/.exec(ctx.url || "");
      if (dm) rec.nights = parseInt(dm[1], 10);
    }

    // cottages.com and Hoseasons — same Awaze platform, same shape.
    else if (host.indexOf("cottages.com") !== -1 || host.indexOf("hoseasons") !== -1) {
      rec.source = host.indexOf("hoseasons") !== -1 ? "hoseasons" : "cottages.com";
      var pp = (ctx.next && ctx.next.props && ctx.next.props.pageProps) || {};
      var svc = pp.service || {};
      rec.name = svc.propertyName || rec.name;
      rec.code = svc.code || rec.code;
      rec.location = svc.location || rec.location;
      if (svc.grade && !isNaN(parseFloat(svc.grade))) rec.score = parseFloat(svc.grade);
      if (pp.lengthOfStay) rec.nights = parseInt(pp.lengthOfStay, 10);
      if (pp.guests && pp.guests.adults) rec.adults = parseInt(pp.guests.adults, 10);
      if (svc.bedrooms != null) rec.bedrooms = parseInt(svc.bedrooms, 10) || null;
      if (svc.bathrooms != null) rec.bathrooms = parseFloat(svc.bathrooms) || null;
      if (svc.guests != null) rec.sleeps = parseInt(svc.guests, 10) || null;
      if (svc.latitude != null && svc.longitude != null) {
        rec.latitude = parseFloat(svc.latitude);
        rec.longitude = parseFloat(svc.longitude);
      }
      // Same story as the amenities below: which key holds the write-up
      // depends on the brand and the template, so try the lot. Some of these
      // are a list of titled sections rather than a string — `asProse` flattens
      // whichever shape turns up.
      rec.summary = summarise(
        svc.description || svc.propertyDescription || svc.longDescription ||
        svc.overview || svc.summary || svc.sections ||
        pp.description || pp.propertyDescription);

      // Awaze files amenities under a few different names depending on which
      // of its brands rendered the page, so try each and take the first list.
      var attrs = svc.attributes || svc.amenities || svc.facilities || pp.attributes;
      if (attrs && attrs.length) {
        var names = [];
        for (var ai = 0; ai < attrs.length; ai++) {
          var a = attrs[ai];
          var nm = typeof a === "string" ? a : (a && (a.name || a.title || a.label));
          if (nm) names.push(String(nm));
        }
        if (names.length) rec.amenities = names;
      }
      if (svc.beds || svc.sleepingArrangements) {
        rec.beds = parseBeds([].concat(svc.beds || [], svc.sleepingArrangements || [])
          .map(function (b) { return typeof b === "string" ? b : JSON.stringify(b); }));
      }

      // The search query survives in the URL — start=09-10-2026 (day first),
      // nights, adult, regionName — and beats anything guessed from the page.
      var aq = {};
      ((ctx.url || "").split("?")[1] || "").split("&").forEach(function (kv) {
        var kp = kv.split("="); if (kp[0]) aq[kp[0]] = decodeURIComponent((kp[1] || "").replace(/\+/g, " "));
      });
      if (aq.adult) rec.adults = parseInt(aq.adult, 10);
      if (aq.nights) rec.nights = parseInt(aq.nights, 10);
      if (aq.regionName) rec.region = aq.regionName;
      if (aq.start && /^\d{2}-\d{2}-\d{4}$/.test(aq.start)) {
        var d = aq.start.split("-");
        var ci = new Date(Date.UTC(+d[2], +d[1] - 1, +d[0]));
        rec.checkin = ci.toISOString().slice(0, 10);
        if (rec.nights) {
          rec.checkout = new Date(ci.getTime() + rec.nights * 86400000)
            .toISOString().slice(0, 10);
        }
      }
      // Price is rendered client-side, so read it off the page.
      rec.price_candidates = poundAmounts(ctx.text || "");
    }

    // Booking.com — the URL carries dates and party size; the price is on screen.
    else if (host.indexOf("booking.com") !== -1) {
      rec.source = "booking.com";
      var m = /\/hotel\/([a-z]{2})\/([^/.]+)\./.exec(ctx.url || "");
      if (m) {
        rec.code = m[2];
        rec.name = m[2].replace(/-/g, " ").replace(/\b\w/g, function (c) { return c.toUpperCase(); });
      }
      var q = {};
      ((ctx.url || "").split("?")[1] || "").split("&").forEach(function (kv) {
        var p = kv.split("="); if (p[0]) q[p[0]] = decodeURIComponent(p[1] || "");
      });
      if (q.group_adults) rec.adults = parseInt(q.group_adults, 10);
      if (q.no_rooms) rec.rooms = parseInt(q.no_rooms, 10);
      if (q.checkin && q.checkout) {
        rec.checkin = q.checkin; rec.checkout = q.checkout;
        rec.nights = Math.round(
          (new Date(q.checkout) - new Date(q.checkin)) / 86400000
        );
      }
      // Everything below comes off the rendered page, because Booking.com puts
      // none of it in JSON-LD and serves a bot wall to anything but a browser.
      // This is the only route to the factors that aren't cost.
      parseLatLng(dom.latlng, rec);
      if (dom.address) rec.address = dom.address.replace(/\s+/g, " ").trim();
      if (dom.score) {
        rec.score = parseScore(dom.score);
        rec.reviews = parseReviews(dom.score) || parseReviews(ctx.text || "");
      }
      if (dom.subscores && dom.subscores.length) {
        rec.subscores = parseSubscores(dom.subscores);
      }
      if (dom.facilities && dom.facilities.length) rec.amenities = dom.facilities;
      if (dom.beds && dom.beds.length) rec.beds = parseBeds(dom.beds);

      var money = moneyAmounts(ctx.text || "");
      rec.price_candidates = money.values;
      if (money.currency) rec.currency = money.currency;

      // Booking.com encodes the selected block's price in sr_pri_blocks, as
      // minor units on the end: ..._5_0_0__29363 means 293.63. That beats
      // scraping the rendered page, which shows many competing figures.
      var pri = /sr_pri_blocks=[^&]*?__(\d+)/.exec(ctx.url || "");
      if (pri) {
        var amount = parseInt(pri[1], 10) / 100;
        if (amount >= 10 && amount <= 100000) {
          rec.price = amount;
          // Measured against two real checkouts this runs 19-23% under the
          // final total: it is roughly the pre-tax room rate, not the bill.
          rec.price_basis = "indicative";
          rec.tax_included = false;
        }
      }
    }

    // Whatever the site's own data didn't give up, the rendered page might.
    // Booking.com reaches this with the real write-up; a site whose structured
    // data has no description at all reaches it with the meta tag, which is two
    // sentences but is two sentences about this property.
    if (!rec.summary) rec.summary = summarise(dom.description || dom.meta);

    if (!rec.source) rec.source = host.replace(/^www\./, "");
    // Deliberately no "single candidate must be the total" shortcut. A page
    // showing one money figure is not thereby showing the price — a £40 pet
    // supplement scraped alone would sail through as a two-night stay. Text
    // amounts are always offered for confirmation; only structured data
    // (JSON-LD offers, __NEXT_DATA__, the URL's own price) sets a price.
    return rec;
  }

  // Exported for the node test harness; harmless in a browser.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      extract: extract, poundAmounts: poundAmounts,
      parseBeds: parseBeds, parseSubscores: parseSubscores,
      parseScore: parseScore, parseReviews: parseReviews,
      summarise: summarise, asProse: asProse, tidy: tidy
    };
    return;
  }

  // ── browser plumbing ─────────────────────────────────────────────────────
  var rec = extract(browserContext());

  // The record alone, with no command wrapped round it. It used to copy
  // `./lodgingbuddy.py paste '...'`, which reads as a Unix command line and is
  // one on no version of Windows: cmd.exe doesn't take single quotes, and the
  // `./` is wrong everywhere off Unix. The prompt reads raw JSON directly, so
  // the wrapper bought nothing and cost a platform. Shell-quoting goes with
  // it — JSON.stringify already escapes what needs escaping.
  var cmd = JSON.stringify(rec);

  // What came off the page, named. A site that quietly changes its markup
  // shows up here as a missing line, which is the only way you'd notice
  // before the stay is already in the table scoring zero for no reason.
  function found() {
    var bits = [];
    if (rec.score != null) {
      bits.push("score " + rec.score + (rec.reviews ? " (" + rec.reviews + " reviews)" : ""));
    }
    if (rec.subscores) {
      var keys = [];
      for (var k in rec.subscores) keys.push(k);
      bits.push(keys.length + " sub-scores");
    }
    if (rec.beds && rec.beds.length) {
      var beds = [];
      for (var i = 0; i < rec.beds.length; i++) {
        beds.push(rec.beds[i].count + " " + rec.beds[i].type);
      }
      bits.push("beds: " + beds.join(", "));
    }
    if (rec.amenities && rec.amenities.length) bits.push(rec.amenities.length + " amenities");
    if (rec.latitude != null) bits.push("located");
    if (rec.summary) {
      bits.push("summary: " + rec.summary.split(/\s+/).length + " words"
                + (/…$/.test(rec.summary) ? ", cut to fit" : ""));
    }
    return bits;
  }

  function done(copied) {
    var n = rec.price_candidates.length;
    var hint = rec.price != null
      ? "price " + rec.price
      : (n ? "pick a price from " + n + " candidates" : "no price found");
    var extras = found();
    alert(
      (copied ? "Copied to clipboard.\n\n" : "Copy this:\n\n") +
      (rec.name || "(unnamed)") + " — " + hint +
      (extras.length ? "\n\nAlso captured:\n  " + extras.join("\n  ")
                     : "\n\nNothing captured beyond the price.") +
      (copied ? "\n\nPaste it at the stays> prompt." : "\n\n" + cmd)
    );
  }

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(cmd).then(function () { done(true); },
                                            function () { done(false); });
  } else {
    done(false);
  }
})();
