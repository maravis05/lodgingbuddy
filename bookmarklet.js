/*
 * lodgingbuddy bookmarklet — run this on a listing page you're looking at.
 *
 * Why this exists: Claude Code runs on a headless box over SSH, while Chrome
 * runs on Windows. The Chrome extension can't bridge that (it uses Native
 * Messaging, which is same-machine only). But the browser has already rendered
 * the page and already passed any bot wall — so the data is sitting right
 * there. This pulls the fields out and copies a ready-to-run command.
 *
 * Build with:  python3 build_bookmarklet.py
 * Then paste the contents of bookmarklet.txt into a new bookmark's URL field.
 *
 * The extractor is split from the browser plumbing so it can be tested against
 * saved HTML under node — see test_bookmarklet.js.
 */

(function () {
  "use strict";

  // ── page accessors (the only part that needs a real browser) ──────────────
  function browserContext() {
    var lds = [];
    var nodes = document.querySelectorAll('script[type="application/ld+json"]');
    for (var i = 0; i < nodes.length; i++) {
      try { lds.push(JSON.parse(nodes[i].textContent)); } catch (e) { /* skip */ }
    }
    var nd = null;
    var el = document.getElementById("__NEXT_DATA__");
    if (el) { try { nd = JSON.parse(el.textContent); } catch (e) { /* skip */ } }
    return {
      url: location.href,
      host: location.hostname,
      jsonld: lds,
      next: nd,
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

  function extract(ctx) {
    var rec = {
      source: null, url: (ctx.url || "").split("#")[0], code: null, name: null,
      location: null, region: null, nights: null, adults: null, rooms: null,
      sleeps: null, bedrooms: null, bathrooms: null, score: null, reviews: null,
      checkin: null, checkout: null,
      price: null, currency: "GBP", price_basis: null, price_candidates: []
    };
    var host = ctx.host || "";

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
        if (o.aggregateRating) {
          rec.score = o.aggregateRating.ratingValue;
          rec.reviews = o.aggregateRating.reviewCount || o.aggregateRating.ratingCount;
        }
        var p = o.containsPlace;
        if (p) {
          if (p.occupancy && p.occupancy.value) rec.sleeps = p.occupancy.value;
          rec.bedrooms = p.numberOfBedrooms != null ? p.numberOfBedrooms : rec.bedrooms;
          rec.bathrooms = p.numberOfBathroomsTotal != null ? p.numberOfBathroomsTotal : rec.bathrooms;
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
    module.exports = { extract: extract, poundAmounts: poundAmounts };
    return;
  }

  // ── browser plumbing ─────────────────────────────────────────────────────
  var rec = extract(browserContext());
  var cmd = "./lodgingbuddy.py paste '" +
    JSON.stringify(rec).replace(/'/g, "'\\''") + "'";

  function done(copied) {
    var n = rec.price_candidates.length;
    var hint = rec.price != null
      ? "price " + rec.price
      : (n ? "pick a price from " + n + " candidates" : "no price found");
    alert(
      (copied ? "Copied to clipboard.\n\n" : "Copy this:\n\n") +
      (rec.name || "(unnamed)") + " — " + hint +
      (copied ? "\n\nPaste it into your Claude Code terminal." : "\n\n" + cmd)
    );
  }

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(cmd).then(function () { done(true); },
                                            function () { done(false); });
  } else {
    done(false);
  }
})();
