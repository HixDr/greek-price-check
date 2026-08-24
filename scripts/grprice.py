#!/usr/bin/env python3
"""
grprice — personal price gatherer for Skroutz.gr and BestPrice.gr.

This tool gathers and decides nothing. It searches both sites, pulls full detail
on every hit, and writes a run folder with a REPORT.txt. It deliberately does no
ranking, matching, dedupe or top-N selection: those are judgements about which
products a person should compare, and they belong to whoever reads the report.

  grprice.py gather "wifi camera" --browser --plus --limit 3
  grprice.py offers https://www.bestprice.gr/item/2163777203/...html
  grprice.py track add "nothing phone 4a pro" --target 550
  grprice.py track check
  grprice.py login                # optional; search does not need an account

Both sites publish schema.org JSON-LD, which is what the parsers read. Skroutz
additionally needs --browser: it sits behind Cloudflare Bot Management and
refuses plain HTTP clients (see README). Skroutz specs come from the rendered
DOM, the one place this tool reads presentation markup.

Stdlib only, except the optional Playwright browser transport. Responses cache
to ~/.cache/grprice for 15 min; be polite, this is for personal use.
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CACHE_DIR = Path(os.environ.get("GRPRICE_CACHE", Path.home() / ".cache" / "grprice"))
STATE_FILE = CACHE_DIR / "tracked.json"
CACHE_TTL = int(os.environ.get("GRPRICE_TTL", "900"))
# Skroutz throttles hard (whole-IP 403 after a handful of quick hits from
# datacenter ranges); BestPrice is far more tolerant. Pace per host.
# Shipping. Skroutz Plus makes most Skroutz orders free-delivery, while every
# BestPrice row carries its own courier fee -- so comparing list prices alone
# systematically flatters BestPrice. Set GRPRICE_SKROUTZ_PLUS=1 (or pass --plus)
# to have Skroutz totals reflect the subscription.
SKROUTZ_PLUS = os.environ.get("GRPRICE_SKROUTZ_PLUS", "").lower() in ("1", "true", "yes")
PLUS_FREE_OVER = {"address": float(os.environ.get("GRPRICE_PLUS_ADDRESS_MIN", "25")),
                  "point": float(os.environ.get("GRPRICE_PLUS_POINT_MIN", "15"))}
DELIVERY_MODE = os.environ.get("GRPRICE_DELIVERY", "address")

MIN_INTERVAL = {"www.skroutz.gr": 4.0, "www.bestprice.gr": 1.0}
DEFAULT_INTERVAL = 2.0

_last_request: dict[str, float] = {}
# Hosts that answered with a bot-management challenge during this run. A
# challenge is not a rate limit -- it does not lift because we waited -- so once
# a host lands here every further request short-circuits instead of burning the
# retry budget. search() relaxes the query and retries on empty results, which
# previously re-hit a dead host several times over at ~24s a go.
_blocked_hosts: dict[str, str] = {}

# Browser transport. Skroutz sits behind Cloudflare Bot Management and refuses
# every plain HTTP client, so the only way to read its HTML is to be an actual
# browser. Measured, from a datacenter IP:
#     urllib / curl, any headers  -> 403 "Just a moment..."   (challenge)
#     Playwright, headless        -> 403 "Attention Required" (hard block)
#     Playwright, headed          -> 200, page renders, JSON-LD intact
# Headless is not merely challenged, it is denied outright -- so this path is
# headed by default and warns if you force it off. Playwright is an optional
# dependency, imported only when the browser transport is actually used; the
# BestPrice path stays stdlib-only and much faster over plain HTTP.
USE_BROWSER = os.environ.get("GRPRICE_BROWSER", "").lower() in ("1", "true", "yes")
BROWSER_HEADLESS = os.environ.get("GRPRICE_BROWSER_HEADLESS", "").lower() in ("1", "true", "yes")
BROWSER_HOSTS = {"www.skroutz.gr"}
BROWSER_PROFILE = CACHE_DIR / "browser-profile"
CHALLENGE_WAIT = float(os.environ.get("GRPRICE_CHALLENGE_WAIT", "25"))
_browser = None


class BlockedError(RuntimeError):
    """A site refused us at the bot-management layer. Retrying will not help."""

    def __init__(self, host: str, message: str):
        super().__init__(message)
        self.host = host


def _challenge_reason(headers, body: str) -> str | None:
    """Detect a Cloudflare bot-management challenge.

    Skroutz answers the *first* request from a non-browser client with 403,
    `cf-mitigated: challenge`, and the "Just a moment..." interstitial. Clearing
    it requires executing the challenge JavaScript and presenting a browser TLS
    fingerprint, so no User-Agent, header set, or delay gets a plain HTTP client
    through. Verified: a full Chrome-equivalent header set still returns 403.
    """
    get = headers.get if headers is not None else (lambda *_a, **_k: None)
    low = body.lower()
    if (get("cf-mitigated") or "").lower() == "challenge":
        return "cloudflare"
    if "just a moment" in low or "challenges.cloudflare.com" in body:
        return "cloudflare"
    if "attention required" in low and "cloudflare" in low:
        return "cloudflare"
    if "cloudflare" in (get("server") or "").lower() and "cf-chl" in low:
        return "cloudflare"
    return None


def _blocked_message(host: str) -> str:
    if host in BROWSER_HOSTS and not USE_BROWSER:
        return (f"{host} is behind Cloudflare bot management and served a "
                f"challenge instead of the listing. No header or User-Agent gets "
                f"a plain HTTP client past it. Re-run with --browser (a real "
                f"browser does get through), or use --source bestprice.")
    if host in BROWSER_HOSTS and BROWSER_HEADLESS:
        return (f"{host} refused the headless browser. Cloudflare denies headless "
                f"Chromium outright rather than challenging it. Drop "
                f"GRPRICE_BROWSER_HEADLESS -- headed works.")
    return (f"{host} refused this client at the bot-management layer. Waiting "
            f"does not clear it; use --source bestprice for usable numbers.")


# ------------------------------------------------------- browser transport

def _browser_context():
    """Lazily start one headed browser and reuse it for the whole run."""
    global _browser
    if _browser is not None:
        return _browser[1]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "--browser needs Playwright, which is an optional dependency:\n"
            "    pip install playwright && playwright install chromium\n"
            "Everything else in this tool remains stdlib-only.") from e

    if BROWSER_HEADLESS:
        print("warning: headless Chromium is hard-blocked by Cloudflare; "
              "headed is the mode that works.", file=sys.stderr)
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    # A persistent profile keeps cookies between runs, so the challenge is
    # cleared once rather than on every invocation. Greek locale/timezone
    # because that is the market being queried.
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(BROWSER_PROFILE),
        headless=BROWSER_HEADLESS,
        locale="el-GR",
        timezone_id="Europe/Athens",
        viewport={"width": 1366, "height": 900},
    )
    _browser = (pw, ctx)
    atexit.register(_close_browser)
    return ctx


def _close_browser() -> None:
    global _browser
    if _browser is None:
        return
    pw, ctx = _browser
    _browser = None
    try:
        ctx.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass


def _browser_get(url: str) -> str:
    """Render a page in a real browser and hand back its HTML."""
    host = urllib.parse.urlparse(url).netloc
    page = _browser_context().new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # A managed challenge clears itself in a real browser after a few
        # seconds. Poll the title rather than sleeping a flat interval.
        deadline = time.time() + CHALLENGE_WAIT
        while time.time() < deadline:
            if "just a moment" not in (page.title() or "").lower():
                break
            page.wait_for_timeout(500)
        html = page.content()
        if _challenge_reason(None, html):
            msg = _blocked_message(host)
            _blocked_hosts[host] = msg
            raise BlockedError(host, msg)
        return html
    finally:
        page.close()


def browser_login(url: str = "https://www.skroutz.gr/") -> None:
    """Open the browser so a session can be established by hand.

    Optional. Search results do not need an account -- this exists only if you
    want the logged-in view. See the README on why logged-out is the default.
    """
    ctx = _browser_context()
    page = ctx.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    print("Browser open. Log in, then press Enter here to save the session.",
          file=sys.stderr)
    try:
        input()
    except EOFError:
        pass
    print(f"Session stored in {BROWSER_PROFILE}", file=sys.stderr)
    page.close()


# ------------------------------------------------------------ http transport

def _http_get(url: str, host: str, xhr: bool) -> str:
    headers = {
        "User-Agent": UA,
        "Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    if xhr:
        headers["X-Requested-With"] = "XMLHttpRequest"

    req = urllib.request.Request(url, headers=headers)
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = resp.read().decode("utf-8", "replace")
            _last_request[host] = time.time()
            return body
        except urllib.error.HTTPError as e:
            _last_request[host] = time.time()
            try:
                err_body = e.read().decode("utf-8", "replace")
            except Exception:
                err_body = ""
            if _challenge_reason(getattr(e, "headers", None), err_body):
                msg = _blocked_message(host)
                _blocked_hosts[host] = msg
                raise BlockedError(host, msg) from e
            if e.code in (403, 429, 503) and attempt < 2:
                time.sleep(8 * (attempt + 1))
                last_err = e
                continue
            if e.code == 403:
                msg = (f"{host} returned 403 with no challenge marker. The IP may "
                       f"genuinely be rate-limited; slow down or switch source.")
                _blocked_hosts[host] = msg
                raise BlockedError(host, msg) from e
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last_err}")


def fetch(url: str, ttl: int = CACHE_TTL, xhr: bool = False) -> str:
    """GET a URL with on-disk caching, rate limiting, and transport dispatch."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()[:20]
    path = CACHE_DIR / f"{key}.html"

    if ttl > 0 and path.exists() and time.time() - path.stat().st_mtime < ttl:
        return path.read_text(encoding="utf-8")

    host = urllib.parse.urlparse(url).netloc
    if host in _blocked_hosts:
        # Already refused this run; going back changes nothing but the clock.
        raise BlockedError(host, _blocked_hosts[host])

    gap = MIN_INTERVAL.get(host, DEFAULT_INTERVAL)
    wait = gap - (time.time() - _last_request.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)

    if USE_BROWSER and host in BROWSER_HOSTS:
        body = _browser_get(url)
        _last_request[host] = time.time()
    else:
        body = _http_get(url, host, xhr)

    path.write_text(body, encoding="utf-8")
    return body


def ld_blocks(html: str) -> list:
    """Yield every parseable application/ld+json payload in a page."""
    out = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.S | re.I,
    ):
        try:
            out.append(json.loads(m.group(1).strip()))
        except json.JSONDecodeError:
            continue
    return out


def _delivered_key(offer: dict):
    """Rank by delivered cost, falling back to list price when shipping is unknown."""
    v = offer.get("total")
    if v is None:
        v = offer.get("price")
    return v if v is not None else 1e9


def _best_delivered(offers: list[dict]) -> dict:
    """Cheapest row once shipping is counted, and how it differs from list price."""
    known = [o for o in offers if o.get("total") is not None]
    if not known:
        return {"delivered_from": None,
                "shipping_known_for": 0,
                "note": "no shipping data on this page; prices are list only"}
    best = min(known, key=lambda o: o["total"])
    by_list = min(offers, key=lambda o: o["price"] if o.get("price") is not None else 1e9)
    return {
        "delivered_from": best["total"],
        "delivered_shop": best.get("shop"),
        "shipping_known_for": len(known),
        "cheapest_listing_is_cheapest_delivered":
            best.get("shop") == by_list.get("shop"),
    }


def _price(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return round(float(str(v).replace(",", ".")), 2)
    except ValueError:
        return None


# ---------------------------------------------------------------- skroutz

SKROUTZ_SEARCH = "https://www.skroutz.gr/search?keyphrase={}"


def skroutz_search(query: str, limit: int = 8) -> list[dict]:
    html = fetch(SKROUTZ_SEARCH.format(urllib.parse.quote_plus(query)))
    results = []
    for block in ld_blocks(html):
        if not isinstance(block, dict) or block.get("@type") != "ItemList":
            continue
        for entry in block.get("itemListElement", []):
            item = entry.get("item", {})
            offers = item.get("offers") or {}
            rating = item.get("aggregateRating") or {}
            results.append({
                "source": "skroutz",
                "name": item.get("name"),
                "url": item.get("url"),
                "price_from": _price(offers.get("price") or offers.get("lowPrice")),
                "shops": offers.get("offerCount"),
                "rating": rating.get("ratingValue"),
                "reviews": rating.get("reviewCount"),
            })
    # A single exact match redirects straight to the SKU page.
    if not results:
        one = skroutz_offers(_canonical(html) or SKROUTZ_SEARCH.format(
            urllib.parse.quote_plus(query)))
        if one.get("name"):
            results.append({
                "source": "skroutz", "name": one["name"], "url": one["url"],
                "price_from": one.get("price_from"),
                "shops": len(one.get("offers", [])),
                "rating": one.get("rating"), "reviews": one.get("reviews"),
            })
    return results[:limit]


def _canonical(html: str) -> str | None:
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', html)
    return m.group(1) if m else None


def _skroutz_shipping(card_html: str, price: float | None) -> tuple:
    """Shipping for one Skroutz shop card, adjusted for a Plus subscription."""
    if SKROUTZ_PLUS and price is not None:
        floor = PLUS_FREE_OVER.get(DELIVERY_MODE, 25.0)
        if price >= floor:
            return 0.0, f"free with Skroutz Plus (over {floor:.0f} EUR, {DELIVERY_MODE})"
    fee = re.search(r'product-card-fee-value">([^<]+)<', card_html)
    if not fee:
        return None, "not shown on the listing (Skroutz varies it by address)"
    txt = fee.group(1).strip()
    if txt.lower().startswith(("δωρε", "free")):
        return 0.0, "free shipping shown on the listing"
    amount = _price(re.sub(r"[^\d,.]", "", txt).replace(".", "").replace(",", "."))
    return amount, (f"listing shows {txt}" if amount is not None else
                    "could not parse the shipping label")


def _strip_tags(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t)).strip()


def _brand_name(product: dict):
    b = product.get("brand") or product.get("manufacturer")
    if isinstance(b, dict):
        return b.get("name")
    return b


def _reviews(product: dict, limit: int = 5) -> list[dict]:
    """Actual review text, where the site publishes it in JSON-LD.

    Skroutz carries a handful of real user reviews per SKU; BestPrice publishes
    none. This is the only qualitative signal either site gives us, so it is
    worth more than another spec field when two candidates are close.
    """
    out = []
    for r in (product.get("review") or [])[:limit]:
        if not isinstance(r, dict):
            continue
        body = (r.get("reviewBody") or "").strip()
        if not body:
            continue
        out.append({
            "rating": (r.get("reviewRating") or {}).get("ratingValue"),
            "text": re.sub(r"\s+", " ", body),
            "date": r.get("datePublished"),
        })
    return out


def _skroutz_specs(html: str) -> dict:
    """Skroutz publishes no specs in JSON-LD, but the rendered page has them.

    Structure is a flat run of <dl><dt>label</dt><dd>value</dd></dl>. This is
    presentation markup rather than structured data, so unlike the rest of this
    tool it *will* break on a redesign -- hence it fails soft to {} rather than
    taking the run down with it. Only populated on the --browser path, because
    a plain HTTP fetch never gets the page at all.
    """
    specs = {}
    try:
        for k, v in re.findall(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", html, re.S):
            k, v = _strip_tags(k), _strip_tags(v)
            if k and v and len(k) < 60 and len(v) < 120:
                specs[k] = v
    except Exception:
        return {}
    return specs


def skroutz_offers(url: str) -> dict:
    """Per-shop offers for one Skroutz SKU page."""
    html = fetch(url)
    product = next((b for b in ld_blocks(html)
                    if isinstance(b, dict) and b.get("@type") == "Product"), {})
    rating = product.get("aggregateRating") or {}
    sku = product.get("sku") or (re.search(r"/s/(\d+)/", url) or [None, None])[1]

    offers = []
    if sku:
        try:
            shops = fetch(f"https://www.skroutz.gr/s/{sku}/shops_list", xhr=True)
        except Exception:
            shops = ""
        names = dict(re.findall(r'href="/shop/(\d+)/([^/#"?]+)', shops))
        for card in re.finditer(
            r'<li[^>]*data-shop-id="(\d+)"[^>]*data-raw-price="([\d.]+)"[^>]*>'
            r'(.*?)(?=<li[^>]*data-shop-id=|\Z)', shops, re.S,
        ):
            shop_id, raw, body = card.groups()
            title = re.search(r'class="product-name"[^>]*>([^<]+)<', body)
            eta = re.search(r'delivery-estimation-date">([^<]+)<', body)
            price = _price(raw)
            ship, note = _skroutz_shipping(body, price)
            offers.append({
                "shop": urllib.parse.unquote(names.get(shop_id, f"shop-{shop_id}")),
                "shop_id": int(shop_id),
                "price": price,
                "shipping": ship,
                "total": (round(price + ship, 2)
                          if price is not None and ship is not None else None),
                "shipping_note": note,
                "delivery_eta": eta.group(1).strip() if eta else None,
                "listing_title": (title.group(1).strip() if title else None),
            })
        offers.sort(key=_delivered_key)

    top = (product.get("offers") or {})
    return {
        "source": "skroutz",
        "name": product.get("name"),
        "url": product.get("url") or url,
        "sku": sku,
        "price_from": _price(top.get("price")) or (offers[0]["price"] if offers else None),
        "brand": _brand_name(product),
        "model": product.get("model"),
        "mpn": product.get("mpn"),
        "category": product.get("category"),
        "color": product.get("color"),
        "rating": rating.get("ratingValue"),
        "reviews": rating.get("reviewCount"),
        "review_sample": _reviews(product),
        "specs": _skroutz_specs(html),
        "delivered": _best_delivered(offers),
        "offers": offers,
        "note": ("Skroutz anonymises some marketplace listings; shop names come "
                 "from the reviews links and may be missing for a few rows. "
                 "Its `description` is SEO boilerplate and is deliberately not "
                 "returned; use BestPrice's for editorial copy."),
    }


# -------------------------------------------------------------- bestprice

BESTPRICE_SEARCH = "https://www.bestprice.gr/search?q={}"


def bestprice_search(query: str, limit: int = 8) -> list[dict]:
    html = fetch(BESTPRICE_SEARCH.format(urllib.parse.quote_plus(query)))
    results = []
    for block in ld_blocks(html):
        if not isinstance(block, dict):
            continue
        entities = []
        main = block.get("mainEntity")
        if isinstance(main, dict) and main.get("@type") == "ItemList":
            entities = main.get("itemListElement", [])
        elif block.get("@type") == "ItemList":
            entities = block.get("itemListElement", [])
        for entry in entities:
            item = entry.get("item", {})
            if item.get("@type") != "Product":
                continue
            offers = item.get("offers") or {}
            results.append({
                "source": "bestprice",
                "name": item.get("name"),
                "url": item.get("url"),
                "price_from": _price(offers.get("lowPrice") or offers.get("price")),
                "shops": offers.get("offerCount"),
                "rating": (item.get("aggregateRating") or {}).get("ratingValue"),
                "reviews": (item.get("aggregateRating") or {}).get("reviewCount"),
            })
    return results[:limit]


def bestprice_offers(url: str) -> dict:
    html = fetch(url)
    product = next((b for b in ld_blocks(html)
                    if isinstance(b, dict) and b.get("@type") == "Product"), {})
    agg = product.get("offers") or {}
    # Per-shop courier fees live in data- attributes on the price rows, keyed by
    # the same product id that appears in each JSON-LD offer URL.
    ship_by_id = {}
    for row in re.finditer(r'<[^>]*prices__product[^>]*>', html):
        tag = row.group(0)
        pid = re.search(r'data-product-id=.(\d+)', tag)
        if not pid:
            continue
        cost = re.search(r'data-shipping-cost=.(\d+)', tag)
        cod = re.search(r'&quot;ondelivery&quot;:(\d+)', tag)
        ship_by_id[pid.group(1)] = {
            "shipping": (round(int(cost.group(1)) / 100, 2) if cost else None),
            "free_shipping": "data-free-shipping" in tag,
            "cod_fee": (round(int(cod.group(1)) / 100, 2) if cod else None),
        }

    offers = []
    for o in agg.get("offers", []) or []:
        price = _price(o.get("price"))
        oid = re.search(r"/to/(\d+)/", o.get("url") or "")
        extra = ship_by_id.get(oid.group(1)) if oid else None
        ship = None
        if extra:
            ship = 0.0 if extra["free_shipping"] else extra["shipping"]
        offers.append({
            "shop": (o.get("seller") or {}).get("name"),
            "price": price,
            "shipping": ship,
            "total": (round(price + ship, 2)
                      if price is not None and ship is not None else None),
            "cod_fee": (extra or {}).get("cod_fee"),
            "url": o.get("url"),
            "in_stock": "InStock" in str(o.get("availability", "")),
        })
    offers.sort(key=_delivered_key)
    rating = product.get("aggregateRating") or {}
    specs = {}
    for prop in product.get("additionalProperty") or []:
        if prop.get("name"):
            specs[prop["name"]] = prop.get("value")
    return {
        "source": "bestprice",
        "name": product.get("name"),
        "url": product.get("url") or url,
        "sku": product.get("sku"),
        "price_from": _price(agg.get("lowPrice")),
        "price_to": _price(agg.get("highPrice")),
        "shop_count": agg.get("offerCount"),
        "rating": rating.get("ratingValue"),
        "reviews": rating.get("reviewCount"),
        "gtin": product.get("gtin13"),
        "brand": _brand_name(product),
        "category": product.get("category"),
        "released": product.get("releaseDate"),
        "delivered": _best_delivered(offers),
        "description": product.get("description"),
        "review_sample": _reviews(product),
        "specs": specs,
        "offers": offers,
    }


# ------------------------------------------------------------- dispatch

STOPWORDS = {"για", "με", "σε", "και", "the", "for", "with", "best", "καλυτερη",
             "καλύτερη", "φθηνη", "φθηνή", "χωρου", "χώρου", "εσωτερικου",
             "εσωτερικού", "καλο", "καλό"}


def _relax(query: str) -> list[str]:
    """Progressively shorter fallbacks: both sites do keyword, not NL, matching."""
    words = [w for w in query.split() if w]
    core = [w for w in words if w.lower() not in STOPWORDS]
    tries = []
    for cand in (" ".join(core), " ".join(core[:3]), " ".join(core[:2])):
        if cand and cand.lower() != query.lower() and cand not in tries:
            tries.append(cand)
    return tries


# Which sources actually answered. A price comparison that quietly drops a site
# is worse than one that fails outright: the caller goes on to present half the
# market as though it were all of it. Every report and payload carries this, so
# a blocked Skroutz can never be mistaken for "Skroutz had nothing cheaper".
_source_status: dict[str, dict] = {}
_warned: set = set()


def _note_source(name: str, state: str, detail: str | None = None) -> None:
    if _source_status.get(name, {}).get("state") == "ok":
        return  # a later relaxation round failing doesn't undo an earlier success
    _source_status[name] = {"source": name, "state": state, "detail": detail}


def source_report() -> list[dict]:
    """Per-source outcome for this run: ok | blocked | error."""
    return sorted(_source_status.values(), key=lambda d: d["source"])


def sources_complete() -> bool:
    return bool(_source_status) and all(
        d["state"] == "ok" for d in _source_status.values())


def with_sources(payload):
    """Attach source provenance to whatever a command is about to emit."""
    report = source_report()
    if isinstance(payload, dict):
        out = dict(payload)
        out["sources"] = report
        out["complete"] = sources_complete()
        return out
    return {"results": payload, "sources": report,
            "complete": sources_complete()}


def _warn(msg: str) -> None:
    """Print a warning once per run, however many times we hit it."""
    if msg not in _warned:
        _warned.add(msg)
        print("warning: " + msg, file=sys.stderr)


def _search_source(name: str, query: str, limit: int) -> list[dict]:
    """Search one site, falling back to shorter queries. Records source status.

    Both sites do keyword matching, not natural language, so a fluent phrase can
    return nothing where its bare keywords work. That fallback is mechanical, so
    it stays in code. Per source rather than pooled: a query that works on one
    site and not the other used to leave the second site unrelaxed.
    """
    fn = {"skroutz": skroutz_search, "bestprice": bestprice_search}[name]
    for i, q in enumerate([query] + _relax(query)):
        try:
            rows = fn(q, limit)
        except BlockedError as e:
            _note_source(name, "blocked", str(e)); _warn(f"{name}: {e}"); return []
        except Exception as e:
            _note_source(name, "error", str(e)); _warn(f"{name}: {e}"); return []
        if rows:
            _note_source(name, "ok")
            if i:
                for r in rows:
                    r["matched_query"] = q
            return rows
    _note_source(name, "ok")
    return []


def _search(query: str, source: str = "both", limit: int = 8) -> list[dict]:
    rows = []
    for name in ("skroutz", "bestprice"):
        if source in (name, "both"):
            rows += _search_source(name, query, limit)
    rows.sort(key=lambda r: r["price_from"] if r.get("price_from") is not None else 1e9)
    record_history(rows)
    return rows


# ----------------------------------------------------------------- gather

def _slug(query: str) -> str:
    s = re.sub(r"[^\wͰ-Ͽ]+", "-", (query or "").lower()).strip("-")
    return s[:40].rstrip("-") or "query"


def _write_raw(raw_dir, seq: int, label: str, payload) -> str:
    name = f"{seq:02d}-{label}.json"
    (raw_dir / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return name


def _detail_id(url: str) -> str:
    m = re.search(r"/(?:s|item|to)/(\d+)", url or "")
    if m:
        return m.group(1)
    return re.sub(r"\W+", "", (url or "x"))[-12:] or "x"


def gather(query: str, source: str = "both", limit: int = 16,
           max_price: float | None = None, runs_dir=None) -> dict:
    """Fetch everything, decide nothing.

    Searches each requested source, then pulls full detail for *every* hit --
    no ranking, no dedupe, no cross-matching, no top-N. Those are judgements
    about which products a person should compare, and they belong to the reader
    of the report, not to this function. `limit` bounds breadth and is the
    caller's dial, not a heuristic.

    Writes one raw JSON per fetch (so any line in the report can be traced back
    to what the site actually returned), a REPORT.txt for a reader to reason
    over, and a manifest recording what ran and what failed.
    """
    base = Path(runs_dir) if runs_dir else CACHE_DIR / "runs"
    stamp = time.strftime("%Y-%m-%d")
    run = base / f"{stamp}-{_slug(query)}"
    n = 2
    while run.exists():
        run = base / f"{stamp}-{_slug(query)}-{n}"
        n += 1
    raw = run / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    fetches, hits, seq = [], [], 0
    for name in ("skroutz", "bestprice"):
        if source not in (name, "both"):
            continue
        seq += 1
        t0 = time.time()
        rows = _search_source(name, query, limit)
        status = next((d for d in source_report() if d["source"] == name), {})
        fetches.append({
            "file": _write_raw(raw, seq, f"search-{name}",
                               {"query": query, "source": name, "rows": rows}),
            "kind": "search", "target": name,
            "status": "ok" if status.get("state") == "ok" else status.get("state"),
            "rows": len(rows), "seconds": round(time.time() - t0, 1),
            "error": status.get("detail"),
        })
        hits.extend(rows)
    record_history(hits)

    dropped = 0
    if max_price is not None:
        before = len(hits)
        hits = [h for h in hits
                if h.get("price_from") is None or h["price_from"] <= max_price]
        dropped = before - len(hits)

    candidates, failed = [], 0
    for h in hits:
        seq += 1
        t0 = time.time()
        err = None
        try:
            d = offers(h["url"])
            d.setdefault("name", h.get("name"))
        except Exception as e:
            err = str(e)
            failed += 1
            d = dict(h)
            d["detail_error"] = err
            d.setdefault("offers", [])
        fetches.append({
            "file": _write_raw(raw, seq,
                               f"detail-{h.get('source')}-{_detail_id(h.get('url'))}", d),
            "kind": "detail", "target": h.get("url"),
            "status": "failed" if err else "ok",
            "seconds": round(time.time() - t0, 1), "error": err,
        })
        candidates.append(d)

    # Presentation order only. Nothing is dropped and nothing is preferred.
    candidates.sort(key=lambda c: (
        c.get("source") or "",
        c["price_from"] if c.get("price_from") is not None else 1e9))

    meta = {
        "query": query,
        "gathered": time.strftime("%Y-%m-%d %H:%M"),
        "source": source, "limit": limit, "max_price": max_price,
        "price_filtered_out": dropped,
        "hits": len(hits), "detailed": len(candidates), "failed": failed,
        "sources": source_report(), "complete": sources_complete(),
        "skroutz_plus": SKROUTZ_PLUS, "browser": USE_BROWSER,
    }
    (run / "REPORT.txt").write_text(_render_report(meta, candidates), encoding="utf-8")
    (run / "manifest.json").write_text(
        json.dumps({**meta, "fetches": fetches}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return {**meta, "run_dir": str(run), "report": str(run / "REPORT.txt")}


def _render_report(meta: dict, candidates: list[dict]) -> str:
    W = 80
    out = ["=" * W, f'GREEK PRICE REPORT — "{meta["query"]}"']
    srcs = "  |  ".join(f"{d['source']}: {d['state']}" for d in meta["sources"])
    out.append(f"gathered {meta['gathered']}  |  {srcs or 'no sources'}")
    line = (f"{meta['limit']}/source requested → {meta['hits']} hits → "
            f"{meta['detailed']} detailed, {meta['failed']} failed")
    out.append(line)
    flags = []
    if meta.get("skroutz_plus"):
        flags.append("Skroutz Plus: on")
    if meta.get("browser"):
        flags.append("skroutz via browser")
    if meta.get("max_price") is not None:
        flags.append(f"--max-price {meta['max_price']} "
                     f"(hid {meta['price_filtered_out']} before detail)")
    if not meta.get("complete"):
        flags.append("INCOMPLETE — a source did not answer; see warnings above")
    if flags:
        out.append("  |  ".join(flags))
    out.append("=" * W)
    out.append("")
    out.append("Nothing here is ranked, matched, or filtered by the tool beyond "
               "the flags above.")
    out.append("Candidates are listed source-then-price purely for readability.")

    for d in meta["sources"]:
        if d["state"] != "ok":
            out += ["", f"!! {d['source']}: {d['state'].upper()}",
                    f"   {d.get('detail') or ''}"]

    total = len(candidates)
    for i, c in enumerate(candidates, 1):
        head = f"--- CANDIDATE {i} of {total} "
        out += ["", head + "-" * max(0, W - len(head))]
        out.append(f"source      {c.get('source')}")
        out.append(f"name        {c.get('name')}")
        out.append(f"url         {c.get('url')}")
        if c.get("detail_error"):
            out.append(f"DETAIL FAILED: {c['detail_error']}")
            out.append("            fields below are from the search row only")
        ident = [f"{k} {c.get(k)}" for k in ("brand", "model", "mpn", "gtin", "color")
                 if c.get(k)]
        if ident:
            out.append("            " + "    ".join(ident))
        if c.get("category"):
            out.append(f"category    {c['category']}")
        price = [f"price_from {c.get('price_from')}"]
        if c.get("price_to") is not None:
            price.append(f"price_to {c['price_to']}")
        if c.get("rating") is not None:
            price.append(f"rating {c['rating']} ({c.get('reviews')} reviews)")
        if c.get("shop_count") is not None:
            price.append(f"shops {c['shop_count']}")
        out.append("            " + "    ".join(price))
        dl = c.get("delivered") or {}
        if dl.get("delivered_from") is not None:
            out.append(f"delivered   {dl['delivered_from']} via "
                       f"{dl.get('delivered_shop')}   "
                       f"cheapest-listing-is-cheapest-delivered: "
                       f"{dl.get('cheapest_listing_is_cheapest_delivered')}")
        elif dl.get("note"):
            out.append(f"delivered   {dl['note']}")
        if c.get("matched_query"):
            out.append(f"note        literal query returned nothing; matched on "
                       f"\"{c['matched_query']}\"")
        if c.get("description"):
            out += ["", "  DESCRIPTION",
                    "    " + re.sub(r"\s+", " ", c["description"]).strip()]
        specs = c.get("specs") or {}
        if specs:
            out += ["", f"  SPECS ({len(specs)})"]
            width = min(34, max(len(k) for k in specs))
            for k, v in specs.items():
                out.append(f"    {k:<{width}}  {v}")
        revs = c.get("review_sample") or []
        if revs:
            total_r = c.get("reviews")
            out += ["", f"  REVIEWS ({len(revs)}"
                        + (f" of {total_r}" if total_r else "") + ")"]
            for r in revs:
                out.append(f"    [{r.get('rating')}] {r.get('text')}")
        offs = c.get("offers") or []
        if offs:
            out += ["", f"  OFFERS ({len(offs)})",
                    "     #  shop                      price    ship   total  eta"]
            for n, o in enumerate(offs, 1):
                def num(v):
                    return f"{v:>6.2f}" if isinstance(v, (int, float)) else "     -"
                out.append(f"    {n:>2}  {str(o.get('shop'))[:24]:<24} "
                           f"{num(o.get('price'))} {num(o.get('shipping'))} "
                           f"{num(o.get('total'))}  {o.get('delivery_eta') or ''}")
        if c.get("note"):
            out += ["", f"  NOTE  {c['note']}"]
    out.append("")
    return "\n".join(out)


def offers(url: str) -> dict:
    if "skroutz.gr" in url:
        return skroutz_offers(url)
    if "bestprice.gr" in url:
        return bestprice_offers(url)
    raise SystemExit("url must be a skroutz.gr or bestprice.gr product page")


# ---------------------------------------------------------------- output

def fmt_money(v) -> str:
    return f"{v:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".") + " €" \
        if isinstance(v, (int, float)) else "—"


def print_offers(d: dict) -> None:
    print(f"{d['name']}  [{d['source']}]")
    rng = fmt_money(d.get("price_from"))
    if d.get("price_to"):
        rng += f" – {fmt_money(d['price_to'])}"
    print(f"  {rng}   {d.get('shop_count') or len(d.get('offers', []))} listings")
    print(f"  {d['url']}\n")
    dv = d.get("delivered") or {}
    if dv.get("delivered_from") is not None:
        tag = "" if dv.get("cheapest_listing_is_cheapest_delivered") \
            else "   <- not the cheapest listing"
        print(f"  cheapest delivered: {fmt_money(dv['delivered_from'])} "
              f"from {dv.get('delivered_shop')}{tag}\n")
    for o in d.get("offers", []):
        stock = "" if o.get("in_stock", True) else "  (out of stock)"
        if o.get("shipping") is None:
            ship = "  + shipping ?"
        elif o["shipping"] == 0:
            ship = "  free delivery"
        else:
            ship = f"  + {fmt_money(o['shipping'])} ship"
        total = f"  = {fmt_money(o['total'])}" if o.get("total") is not None else ""
        print(f"{fmt_money(o['price']):>12}{ship:>16}{total:>14}  "
              f"{o.get('shop') or '?'}{stock}")


def print_source_notice() -> None:
    """Say out loud when a site is missing from the numbers above."""
    missing = [d for d in source_report() if d["state"] != "ok"]
    if not missing:
        return
    names = ", ".join(d["source"] for d in missing)
    print(f"\nincomplete: {names} did not answer, so these prices are not the "
          f"whole market. See the warning above.", file=sys.stderr)


def print_history(rows: list[dict]) -> None:
    if not rows:
        print("nothing recorded yet - run a search or compare first")
        return
    for e in rows:
        flag = "  ** at/near its low **" if e["at_low"] else \
               f"  (+{e['vs_low_pct']}% over low)"
        print(f"{fmt_money(e['last']):>12}  {e['name'][:60]}")
        print(f"{'':>14}low {fmt_money(e['min'])}  high {fmt_money(e['max'])}  "
              f"{e['points']} obs since {e['first_seen'][:10]}{flag}")


# ----------------------------------------------------------------- track

HISTORY_FILE = CACHE_DIR / "history.jsonl"


def record_history(rows: list[dict]) -> None:
    """Append every observed price, so 'is this cheap?' becomes answerable."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M")
        with HISTORY_FILE.open("a", encoding="utf-8") as fh:
            for r in rows:
                if r.get("price_from") is None:
                    continue
                fh.write(json.dumps({"t": stamp, "source": r["source"],
                                     "name": r.get("name"), "url": r.get("url"),
                                     "price": r["price_from"]},
                                    ensure_ascii=False) + "\n")
    except OSError:
        pass


def history(term: str) -> list[dict]:
    """Min / max / latest price per product, for anything seen before."""
    if not HISTORY_FILE.exists():
        return []
    seen: dict[str, dict] = {}
    term = term.lower()
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if term and term not in (rec.get("name") or "").lower():
            continue
        key = rec.get("url") or rec.get("name")
        e = seen.setdefault(key, {"name": rec.get("name"), "url": rec.get("url"),
                                  "source": rec.get("source"), "points": 0,
                                  "min": rec["price"], "max": rec["price"],
                                  "first_seen": rec["t"], "last": rec["price"],
                                  "last_seen": rec["t"]})
        e["points"] += 1
        e["min"] = min(e["min"], rec["price"])
        e["max"] = max(e["max"], rec["price"])
        e["last"] = rec["price"]
        e["last_seen"] = rec["t"]
    out = list(seen.values())
    for e in out:
        e["at_low"] = e["last"] <= e["min"] * 1.02
        e["vs_low_pct"] = (round((e["last"] - e["min"]) / e["min"] * 100, 1)
                           if e["min"] else None)
    out.sort(key=lambda e: e["last_seen"], reverse=True)
    return out


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"items": []}


def save_state(state: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def track_check(as_json: bool = False) -> list[dict]:
    state = load_state()
    report = []
    for item in state["items"]:
        rows = _search(item["query"], item.get("source", "both"), limit=3)
        best = next((r for r in rows if r["price_from"] is not None), None)
        if not best:
            continue
        prev = item.get("last_price")
        item["last_price"] = best["price_from"]
        item["last_checked"] = time.strftime("%Y-%m-%d %H:%M")
        report.append({
            "query": item["query"],
            "name": best["name"],
            "price": best["price_from"],
            "previous": prev,
            "delta": (round(best["price_from"] - prev, 2)
                      if isinstance(prev, (int, float)) else None),
            "target": item.get("target"),
            "hit_target": (item.get("target") is not None
                           and best["price_from"] <= item["target"]),
            "source": best["source"],
            "url": best["url"],
        })
    save_state(state)
    if not as_json:
        for r in report:
            flag = "  ** TARGET HIT **" if r["hit_target"] else ""
            delta = ""
            if r["delta"]:
                delta = f"  ({'+' if r['delta'] > 0 else ''}{r['delta']:.2f})"
            print(f"{fmt_money(r['price']):>12}{delta}  {r['name']}{flag}")
    return report


# ------------------------------------------------------------------ main

def main() -> None:
    p = argparse.ArgumentParser(prog="grprice", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    common.add_argument("--no-cache", action="store_true", default=argparse.SUPPRESS,
                        help="bypass the disk cache")
    common.add_argument("--plus", action="store_true", default=argparse.SUPPRESS,
                        help="you have Skroutz Plus: treat qualifying Skroutz "
                             "orders as free delivery")
    common.add_argument("--delivery", choices=["address", "point"],
                        default=argparse.SUPPRESS,
                        help="where Plus free-shipping thresholds apply")
    common.add_argument("--browser", action="store_true", default=argparse.SUPPRESS,
                        help="read Skroutz through a real (headed) browser; the "
                             "only transport Cloudflare lets through")
    for a in ("--json", "--no-cache", "--plus", "--browser"):
        p.add_argument(a, action="store_true", default=argparse.SUPPRESS,
                       help=argparse.SUPPRESS)
    p.add_argument("--delivery", choices=["address", "point"],
                   default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gather", parents=[common],
                       help="search both sites and pull full detail on every "
                            "hit into a run folder + REPORT.txt")
    g.add_argument("query", nargs="+")
    g.add_argument("--source", choices=["skroutz", "bestprice", "both"], default="both")
    g.add_argument("--limit", type=int, default=16,
                   help="hits per source to detail (default 16); the only cost "
                        "dial -- every hit gets a full detail fetch. BestPrice "
                        "search pages top out at 16, so raising this only adds "
                        "Skroutz results, at ~20s each")
    g.add_argument("--max-price", type=float, dest="max_price",
                   help="skip detail for hits above this price")

    h = sub.add_parser("history", parents=[common],
                       help="price history for anything seen before")
    h.add_argument("term", nargs="*", default=[])

    o = sub.add_parser("offers", parents=[common], help="per-shop prices for one product page")
    o.add_argument("url")

    lg = sub.add_parser("login", parents=[common],
                        help="open the browser to sign in to Skroutz (optional; "
                             "search does not need an account)")
    lg.add_argument("--url", default="https://www.skroutz.gr/")

    t = sub.add_parser("track", parents=[common], help="watch list stored in ~/.cache/grprice")
    tsub = t.add_subparsers(dest="tcmd", required=True)
    ta = tsub.add_parser("add", parents=[common])
    ta.add_argument("query", nargs="+")
    ta.add_argument("--target", type=float, help="alert at or below this price")
    ta.add_argument("--source", choices=["skroutz", "bestprice", "both"], default="both")
    tsub.add_parser("list", parents=[common])
    tr = tsub.add_parser("rm", parents=[common])
    tr.add_argument("query", nargs="+")
    tsub.add_parser("check", parents=[common])

    args = p.parse_args()
    as_json = getattr(args, "json", False)
    if getattr(args, "plus", False):
        global SKROUTZ_PLUS
        SKROUTZ_PLUS = True
    if getattr(args, "delivery", None):
        global DELIVERY_MODE
        DELIVERY_MODE = args.delivery
    if getattr(args, "no_cache", False):
        global CACHE_TTL
        CACHE_TTL = 0
    global USE_BROWSER
    if getattr(args, "browser", False) or args.cmd == "login":
        USE_BROWSER = True

    if args.cmd == "login":
        browser_login(args.url)
        return

    if args.cmd == "gather":
        res = gather(" ".join(args.query), args.source, args.limit,
                     args.max_price)
        if as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(res["report"])
            print(f"{res['detailed']} candidates from {res['hits']} hits"
                  f"  ({res['failed']} failed)"
                  f"  complete={res['complete']}")
        print_source_notice()

    elif args.cmd == "history":
        rows = history(" ".join(args.term))
        print(json.dumps(rows, ensure_ascii=False, indent=2)) if as_json \
            else print_history(rows)

    elif args.cmd == "offers":
        d = offers(args.url)
        print(json.dumps(d, ensure_ascii=False, indent=2)) if as_json \
            else print_offers(d)

    elif args.cmd == "track":
        if args.tcmd == "add":
            state = load_state()
            q = " ".join(args.query)
            state["items"] = [i for i in state["items"] if i["query"] != q]
            state["items"].append({"query": q, "target": args.target,
                                   "source": args.source})
            save_state(state)
            print(f"tracking: {q}" + (f" (target {args.target} €)"
                                      if args.target else ""))
        elif args.tcmd == "list":
            state = load_state()
            print(json.dumps(state, ensure_ascii=False, indent=2)) if as_json else \
                [print(f"{i['query']}  target={i.get('target') or '—'}  "
                       f"last={i.get('last_price') or '—'}") for i in state["items"]]
        elif args.tcmd == "rm":
            state = load_state()
            q = " ".join(args.query)
            state["items"] = [i for i in state["items"] if i["query"] != q]
            save_state(state)
            print(f"removed: {q}")
        elif args.tcmd == "check":
            rep = track_check(as_json=as_json)
            if as_json:
                print(json.dumps(rep, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
