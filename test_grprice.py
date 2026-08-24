#!/usr/bin/env python3
"""
Tests for grprice's handling of Cloudflare bot challenges.

Skroutz sits behind Cloudflare Bot Management. It answers the *first* request
with 403 + `cf-mitigated: challenge` and a "Just a moment..." interstitial.
That is not a rate limit, so retrying and waiting can never clear it. These
tests pin the distinction, because getting it wrong costs ~24s per call and
sends the user off waiting for a block that will never lift.
"""

import email.message
import io
import json
import os
import re
import time
import unittest
import sys
import unittest.mock
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parent / "scripts"))

import grprice


def _headers(pairs: dict) -> email.message.Message:
    m = email.message.Message()
    for k, v in pairs.items():
        m[k] = v
    return m


CHALLENGE_BODY = (
    b'<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>'
    b'</head><body><div class="main-wrapper"></div></body></html>'
)


def _cf_challenge(url):
    return urllib.error.HTTPError(
        url, 403,
        "Forbidden",
        _headers({"server": "cloudflare", "cf-mitigated": "challenge"}),
        io.BytesIO(CHALLENGE_BODY),
    )


class CloudflareChallengeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._orig_cache = grprice.CACHE_DIR
        self._orig_browser = grprice.USE_BROWSER
        grprice.CACHE_DIR = Path(self._tmp.name)
        grprice.USE_BROWSER = False        # these test the plain-HTTP transport
        grprice._last_request.clear()
        grprice._blocked_hosts.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        grprice.CACHE_DIR = self._orig_cache
        grprice.USE_BROWSER = self._orig_browser
        self._tmp.cleanup()

    def test_challenge_raises_blocked_not_generic_error(self):
        """A CF challenge must be its own exception type, not a bare RuntimeError."""
        url = "https://www.skroutz.gr/search?keyphrase=x"
        with unittest.mock.patch("urllib.request.urlopen",
                                 side_effect=lambda *a, **k: (_ for _ in ()).throw(_cf_challenge(url))):
            with self.assertRaises(grprice.BlockedError):
                grprice.fetch(url, ttl=0)

    def test_challenge_does_not_retry(self):
        """Retrying an unwinnable challenge burns ~24s per call. It must fail fast."""
        url = "https://www.skroutz.gr/search?keyphrase=y"
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise _cf_challenge(url)

        started = time.time()
        with unittest.mock.patch("urllib.request.urlopen", side_effect=boom):
            with self.assertRaises(grprice.BlockedError):
                grprice.fetch(url, ttl=0)
        self.assertEqual(len(calls), 1, "challenge must not be retried")
        self.assertLess(time.time() - started, 2.0, "challenge must not sleep/backoff")

    def test_message_names_the_real_cause(self):
        """The old text said 'IP looks rate-limited' - wrong, and it misleads the user."""
        url = "https://www.skroutz.gr/search?keyphrase=z"
        with unittest.mock.patch("urllib.request.urlopen",
                                 side_effect=lambda *a, **k: (_ for _ in ()).throw(_cf_challenge(url))):
            with self.assertRaises(grprice.BlockedError) as ctx:
                grprice.fetch(url, ttl=0)
        msg = str(ctx.exception).lower()
        self.assertIn("cloudflare", msg)
        self.assertNotIn("rate-limited", msg)
        self.assertNotIn("wait a few minutes", msg)

    def test_second_request_to_blocked_host_short_circuits(self):
        """search() relaxes the query and retries; each retry must not re-hit a blocked host."""
        calls = []

        def boom(req, *a, **k):
            calls.append(getattr(req, "full_url", req))
            raise _cf_challenge("https://www.skroutz.gr/")

        with unittest.mock.patch("urllib.request.urlopen", side_effect=boom):
            with self.assertRaises(grprice.BlockedError):
                grprice.fetch("https://www.skroutz.gr/search?keyphrase=a", ttl=0)
            with self.assertRaises(grprice.BlockedError):
                grprice.fetch("https://www.skroutz.gr/search?keyphrase=b", ttl=0)
        self.assertEqual(len(calls), 1, "host known-blocked; must not hit the network again")


class GenuineRateLimitTests(unittest.TestCase):
    """429/503 ARE transient. Those must still retry - don't over-correct."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._orig_cache = grprice.CACHE_DIR
        grprice.CACHE_DIR = Path(self._tmp.name)
        grprice._last_request.clear()
        grprice._blocked_hosts.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        grprice.CACHE_DIR = self._orig_cache
        self._tmp.cleanup()

    def test_429_still_retries_then_succeeds(self):
        url = "https://www.bestprice.gr/search?q=x"
        state = {"n": 0}

        class OK:
            def read(self_inner):
                return b"<html>ok</html>"
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *a):
                return False

        def flaky(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                raise urllib.error.HTTPError(url, 429, "Too Many", _headers({}), io.BytesIO(b""))
            return OK()

        with unittest.mock.patch("urllib.request.urlopen", side_effect=flaky):
            with unittest.mock.patch("time.sleep"):
                body = grprice.fetch(url, ttl=0)
        self.assertEqual(body, "<html>ok</html>")
        self.assertEqual(state["n"], 2)


class BrowserTransportTests(unittest.TestCase):
    """Cloudflare denies headless Chromium outright and lets headed through.

    Measured against the live site from a datacenter IP:
        urllib, any headers -> 403 "Just a moment..."   (challenge)
        Playwright headless -> 403 "Attention Required" (hard block)
        Playwright headed   -> 200, JSON-LD intact
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._orig_cache = grprice.CACHE_DIR
        self._orig_browser = grprice.USE_BROWSER
        grprice.CACHE_DIR = Path(self._tmp.name)
        grprice._last_request.clear()
        grprice._blocked_hosts.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        grprice.CACHE_DIR = self._orig_cache
        grprice.USE_BROWSER = self._orig_browser
        self._tmp.cleanup()

    def test_browser_hosts_route_to_browser_not_urllib(self):
        grprice.USE_BROWSER = True
        with unittest.mock.patch.object(grprice, "_browser_get",
                                        return_value="<html>via browser</html>") as bg:
            with unittest.mock.patch("urllib.request.urlopen") as uo:
                body = grprice.fetch("https://www.skroutz.gr/search?keyphrase=x", ttl=0)
        self.assertEqual(body, "<html>via browser</html>")
        bg.assert_called_once()
        uo.assert_not_called()

    def test_bestprice_stays_on_plain_http_even_with_browser_on(self):
        """BestPrice works fine over HTTP and is far faster. Don't route it."""
        grprice.USE_BROWSER = True

        class OK:
            def read(self_i): return b"<html>via http</html>"
            def __enter__(self_i): return self_i
            def __exit__(self_i, *a): return False

        with unittest.mock.patch.object(grprice, "_browser_get") as bg:
            with unittest.mock.patch("urllib.request.urlopen", return_value=OK()):
                body = grprice.fetch("https://www.bestprice.gr/search?q=x", ttl=0)
        self.assertEqual(body, "<html>via http</html>")
        bg.assert_not_called()

    def test_hard_block_page_is_recognised_as_a_challenge(self):
        """The headless denial page says 'Attention Required', not 'Just a moment'."""
        page = "<html><title>Attention Required! | Cloudflare</title></html>"
        self.assertEqual(grprice._challenge_reason(None, page), "cloudflare")

    def test_browser_get_raises_blocked_when_page_is_a_block_page(self):
        grprice.USE_BROWSER = True

        class FakePage:
            def goto(self_i, *a, **k): return None
            def title(self_i): return "Attention Required! | Cloudflare"
            def content(self_i):
                return "<html><title>Attention Required! | Cloudflare</title></html>"
            def wait_for_timeout(self_i, ms): pass
            def close(self_i): pass

        class FakeCtx:
            def new_page(self_i): return FakePage()

        with unittest.mock.patch.object(grprice, "_browser_context",
                                        return_value=FakeCtx()), \
             unittest.mock.patch.object(grprice, "CHALLENGE_WAIT", 0.01), \
             unittest.mock.patch.object(grprice, "INTERACTIVE_WAIT", 0.01):
            with self.assertRaises(grprice.BlockedError):
                grprice._browser_get("https://www.skroutz.gr/search?keyphrase=x")

    def test_headless_advice_names_the_actual_fix(self):
        grprice.USE_BROWSER = True
        with unittest.mock.patch.object(grprice, "BROWSER_HEADLESS", True):
            msg = grprice._blocked_message("www.skroutz.gr")
        self.assertIn("headless", msg.lower())
        self.assertIn("headed", msg.lower())

def _hit(source, i, price):
    return {"source": source, "name": f"{source} product {i}", "price_from": price,
            "url": f"https://www.{source}.gr/item/{i}", "shops": 3,
            "rating": 4.5, "reviews": 10}


def _detail(hit, offers=3, specs=5, reviews=2):
    d = dict(hit)
    d["offers"] = [{"shop": f"shop{n}", "price": hit["price_from"] + n,
                    "shipping": 0.0, "total": hit["price_from"] + n,
                    "delivery_eta": "1-3"} for n in range(offers)]
    d["specs"] = {f"spec{n}": f"value{n}" for n in range(specs)}
    d["review_sample"] = [{"rating": 5, "text": f"review {n}", "date": None}
                          for n in range(reviews)]
    d["delivered"] = {"delivered_from": hit["price_from"], "delivered_shop": "shop0",
                      "shipping_known_for": offers,
                      "cheapest_listing_is_cheapest_delivered": True}
    return d


class GatherTests(unittest.TestCase):
    """gather fetches everything and decides nothing."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._orig_cache = grprice.CACHE_DIR
        grprice.CACHE_DIR = self.root / "cache"
        grprice._last_request.clear()
        grprice._blocked_hosts.clear()
        grprice._source_status.clear()
        grprice._warned.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        grprice.CACHE_DIR = self._orig_cache
        self._tmp.cleanup()

    def _run(self, sk_rows, bp_rows, detail=None, source="both", limit=8):
        detail = detail or (lambda url: _detail(
            next(h for h in sk_rows + bp_rows if h["url"] == url)))
        with unittest.mock.patch.object(grprice, "skroutz_search",
                                        return_value=list(sk_rows)), \
             unittest.mock.patch.object(grprice, "bestprice_search",
                                        return_value=list(bp_rows)), \
             unittest.mock.patch.object(grprice, "offers", side_effect=detail):
            return grprice.gather("wifi camera", source=source, limit=limit,
                                  runs_dir=self.root / "runs")

    def test_every_hit_from_both_sources_reaches_the_report(self):
        """The crowding-out regression: cheap BestPrice rows must not evict Skroutz.

        The old compare() sorted pooled rows by price and took the top N, which
        on a real run returned 4 BestPrice candidates and 0 Skroutz.
        """
        sk = [_hit("skroutz", i, 90 + i) for i in range(3)]     # all pricier
        bp = [_hit("bestprice", i, 10 + i) for i in range(5)]   # all cheaper
        res = self._run(sk, bp)
        report = (Path(res["run_dir"]) / "REPORT.txt").read_text()
        self.assertEqual(res["detailed"], 8)
        for h in sk + bp:
            self.assertIn(h["name"], report, f"{h['name']} missing from report")
        self.assertEqual(report.count("--- CANDIDATE"), 8)

    def test_failed_detail_still_gets_a_candidate_block(self):
        sk = [_hit("skroutz", 0, 50)]
        bp = [_hit("bestprice", 0, 40)]

        def flaky(url):
            if "skroutz" in url:
                raise grprice.BlockedError("www.skroutz.gr", "challenge served")
            return _detail(bp[0])

        res = self._run(sk, bp, detail=flaky)
        report = (Path(res["run_dir"]) / "REPORT.txt").read_text()
        self.assertIn("skroutz product 0", report)
        self.assertIn("DETAIL FAILED", report)
        self.assertIn("challenge served", report)
        self.assertEqual(res["failed"], 1)

    def test_offers_table_is_not_truncated(self):
        bp = [_hit("bestprice", 0, 40)]
        res = self._run([], bp, detail=lambda u: _detail(bp[0], offers=70),
                        source="bestprice")
        report = (Path(res["run_dir"]) / "REPORT.txt").read_text()
        self.assertIn("OFFERS (70)", report)
        for n in (0, 35, 69):
            self.assertIn(f"shop{n}", report)

    def test_manifest_records_every_fetch_and_its_status(self):
        sk = [_hit("skroutz", 0, 50)]
        bp = [_hit("bestprice", 0, 40)]

        def flaky(url):
            if "skroutz" in url:
                raise RuntimeError("boom")
            return _detail(bp[0])

        res = self._run(sk, bp, detail=flaky)
        man = json.loads((Path(res["run_dir"]) / "manifest.json").read_text())
        states = {f["target"]: f["status"] for f in man["fetches"]}
        self.assertIn("ok", states.values())
        self.assertIn("failed", states.values())
        self.assertTrue(any("boom" in (f.get("error") or "")
                            for f in man["fetches"]))

    def test_blocked_source_still_yields_a_report_marked_incomplete(self):
        bp = [_hit("bestprice", i, 10 + i) for i in range(2)]

        def blocked_sk(*a, **k):
            raise grprice.BlockedError("www.skroutz.gr", "cloudflare challenge")

        with unittest.mock.patch.object(grprice, "skroutz_search",
                                        side_effect=blocked_sk), \
             unittest.mock.patch.object(grprice, "bestprice_search",
                                        return_value=list(bp)), \
             unittest.mock.patch.object(grprice, "offers",
                                        side_effect=lambda u: _detail(bp[0])):
            res = grprice.gather("wifi camera", runs_dir=self.root / "runs")
        report = (Path(res["run_dir"]) / "REPORT.txt").read_text()
        self.assertFalse(res["complete"])
        self.assertIn("blocked", report.lower())
        self.assertEqual(res["detailed"], 2)

    def test_raw_payload_saved_per_fetch(self):
        sk = [_hit("skroutz", 0, 50)]
        bp = [_hit("bestprice", 0, 40)]
        res = self._run(sk, bp)
        raw = sorted((Path(res["run_dir"]) / "raw").glob("*.json"))
        # 2 searches + 2 details
        self.assertEqual(len(raw), 4)
        self.assertTrue(any("search-skroutz" in f.name for f in raw))
        self.assertTrue(any("detail" in f.name for f in raw))

    def test_no_scoring_helpers_survive(self):
        """The judgement subsystem is gone, not merely unused."""
        for gone in ("match_score", "cross_match", "dedupe", "_pick_candidates",
                     "_tokens", "COLOR_WORDS", "NOISE_WORDS"):
            self.assertFalse(hasattr(grprice, gone),
                             f"{gone} should have been deleted")

class CacheControlTests(unittest.TestCase):
    """--no-cache must actually bypass the cache.

    `fetch(url, ttl=CACHE_TTL)` bound its default at import time, so setting
    the module global later (which is exactly what --no-cache does) left every
    caller on the 900s default. Prices went stale precisely when someone asked
    for fresh ones.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._orig_cache = grprice.CACHE_DIR
        self._orig_ttl = grprice.CACHE_TTL
        grprice.CACHE_DIR = Path(self._tmp.name)
        grprice._last_request.clear()
        grprice._blocked_hosts.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        grprice.CACHE_DIR = self._orig_cache
        grprice.CACHE_TTL = self._orig_ttl
        self._tmp.cleanup()

    @staticmethod
    def _resp(body=b"<html>x</html>"):
        class OK:
            def read(self_i): return body
            def __enter__(self_i): return self_i
            def __exit__(self_i, *a): return False
        return OK

    def test_setting_cache_ttl_to_zero_bypasses_the_cache(self):
        grprice.CACHE_TTL = 0
        calls = []

        def once(*a, **k):
            calls.append(1)
            return self._resp()()

        url = "https://www.bestprice.gr/search?q=x"
        with unittest.mock.patch("urllib.request.urlopen", side_effect=once):
            with unittest.mock.patch("time.sleep"):
                grprice.fetch(url)
                grprice.fetch(url)
        self.assertEqual(len(calls), 2,
                         "second fetch served from cache despite CACHE_TTL = 0")

    def test_default_ttl_still_caches(self):
        grprice.CACHE_TTL = 900
        calls = []

        def once(*a, **k):
            calls.append(1)
            return self._resp()()

        url = "https://www.bestprice.gr/search?q=y"
        with unittest.mock.patch("urllib.request.urlopen", side_effect=once):
            with unittest.mock.patch("time.sleep"):
                grprice.fetch(url)
                grprice.fetch(url)
        self.assertEqual(len(calls), 1, "cache should still work by default")

class PacingTests(unittest.TestCase):
    """Concurrency must hide latency without raising the request RATE.

    The pause between requests is the politeness budget. Overlapping slow
    requests is fine -- issuing them faster is not. These pin that distinction,
    because "make it parallel" is exactly how a rate limit gets removed by
    accident.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._orig_cache = grprice.CACHE_DIR
        grprice.CACHE_DIR = Path(self._tmp.name)
        grprice._last_request.clear()
        grprice._blocked_hosts.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        grprice.CACHE_DIR = self._orig_cache
        self._tmp.cleanup()

    def test_skroutz_pause_is_one_second(self):
        self.assertEqual(grprice.MIN_INTERVAL["www.skroutz.gr"], 1.0)

    def test_pace_serialises_across_threads(self):
        """Ten threads hitting one host must still issue at ~1/sec, not all at once."""
        import threading
        grprice.MIN_INTERVAL["test.example"] = 0.05
        stamps = []
        lock = threading.Lock()

        def worker():
            grprice._pace("test.example")
            with lock:
                stamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stamps.sort()
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertEqual(len(stamps), 10)
        too_fast = [g for g in gaps if g < 0.04]
        self.assertFalse(too_fast,
                         f"{len(too_fast)} requests issued faster than the pace: {gaps}")


class ConcurrentGatherTests(unittest.TestCase):
    """Running the two sites at once must not change what comes back."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._orig_cache = grprice.CACHE_DIR
        grprice.CACHE_DIR = self.root / "cache"
        grprice._last_request.clear()
        grprice._blocked_hosts.clear()
        grprice._source_status.clear()
        grprice._warned.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        grprice.CACHE_DIR = self._orig_cache
        self._tmp.cleanup()

    def test_concurrent_run_returns_every_candidate(self):
        sk = [_hit("skroutz", i, 90 + i) for i in range(4)]
        bp = [_hit("bestprice", i, 10 + i) for i in range(4)]

        def slow_detail(url):
            time.sleep(0.15)          # latency, the thing concurrency should hide
            return _detail(next(h for h in sk + bp if h["url"] == url))

        with unittest.mock.patch.object(grprice, "skroutz_search", return_value=list(sk)), \
             unittest.mock.patch.object(grprice, "bestprice_search", return_value=list(bp)), \
             unittest.mock.patch.object(grprice, "offers", side_effect=slow_detail):
            res = grprice.gather("x", runs_dir=self.root / "runs")

        report = (Path(res["run_dir"]) / "REPORT.txt").read_text()
        self.assertEqual(res["detailed"], 8)
        for h in sk + bp:
            self.assertIn(h["name"], report)
        # ordering must stay deterministic despite threads
        order = [l.split()[-1] for l in report.splitlines()
                 if l.startswith("source      ")]
        self.assertEqual(order, sorted(order), "candidate order must stay stable")

    def test_the_two_sites_actually_overlap(self):
        """Guards against the 'parallel' code quietly running serially.

        4 hits per source at 0.15s each is 1.2s if everything is sequential.
        BestPrice pools its details and runs alongside Skroutz, so it should
        land near the slowest single leg instead of the sum.
        """
        sk = [_hit("skroutz", i, 90 + i) for i in range(4)]
        bp = [_hit("bestprice", i, 10 + i) for i in range(4)]

        def slow(url):
            time.sleep(0.15)
            return _detail(next(h for h in sk + bp if h["url"] == url))

        with unittest.mock.patch.object(grprice, "skroutz_search", return_value=list(sk)), \
             unittest.mock.patch.object(grprice, "bestprice_search", return_value=list(bp)), \
             unittest.mock.patch.object(grprice, "offers", side_effect=slow):
            started = time.monotonic()
            res = grprice.gather("x", runs_dir=self.root / "runs")
            elapsed = time.monotonic() - started

        self.assertEqual(res["detailed"], 8)
        self.assertLess(elapsed, 0.95,
                        f"ran in {elapsed:.2f}s; 8 x 0.15s serial would be ~1.2s "
                        "- the legs are not overlapping")

    def test_browser_is_only_ever_touched_from_the_main_thread(self):
        """Playwright's sync API must be used from the thread that created it.

        BestPrice details run in a worker, but its scroll-pagination needs the
        same browser Skroutz drives on the main thread. Searching before the
        threads start is what keeps that safe.
        """
        import threading
        main = threading.get_ident()
        seen = []

        def scroll(query, limit):
            seen.append(threading.get_ident())
            return [str(8000 + i) for i in range(30)]

        sk = [_hit("skroutz", i, 50 + i) for i in range(2)]

        def bp_search(q, limit):
            grprice._bestprice_scroll_ids(q, limit)
            return [_hit("bestprice", i, 40 + i) for i in range(2)]

        with unittest.mock.patch.object(grprice, "_bestprice_scroll_ids", side_effect=scroll), \
             unittest.mock.patch.object(grprice, "skroutz_search", return_value=sk), \
             unittest.mock.patch.object(grprice, "bestprice_search", side_effect=bp_search), \
             unittest.mock.patch.object(grprice, "offers",
                                        side_effect=lambda u: _detail(
                                            {"source": "x", "name": "n", "url": u,
                                             "price_from": 1.0})):
            grprice.gather("x", runs_dir=self.root / "runs")

        self.assertTrue(seen, "scroll pagination never ran")
        self.assertEqual(seen[0], main,
                         "browser scroll ran off the main thread - Playwright will break")

    def test_failures_in_one_source_do_not_lose_the_other(self):
        sk = [_hit("skroutz", i, 50 + i) for i in range(2)]
        bp = [_hit("bestprice", i, 40 + i) for i in range(2)]

        def flaky(url):
            if "skroutz" in url:
                raise RuntimeError("render died")
            return _detail(next(h for h in bp if h["url"] == url))

        with unittest.mock.patch.object(grprice, "skroutz_search", return_value=list(sk)), \
             unittest.mock.patch.object(grprice, "bestprice_search", return_value=list(bp)), \
             unittest.mock.patch.object(grprice, "offers", side_effect=flaky):
            res = grprice.gather("x", runs_dir=self.root / "runs")
        report = (Path(res["run_dir"]) / "REPORT.txt").read_text()
        self.assertEqual(res["detailed"], 4)
        self.assertEqual(res["failed"], 2)
        self.assertIn("render died", report)
        for h in bp:
            self.assertIn(h["name"], report)

def _sk_page(n, count=48):
    """A Skroutz search page's JSON-LD, with distinct urls per page."""
    items = [{"item": {"name": f"sk p{n} item {i}",
                       "url": f"https://www.skroutz.gr/s/{n}{i:03d}/x.html",
                       "offers": {"price": 10 + i, "offerCount": 3},
                       "aggregateRating": {"ratingValue": 4.5, "reviewCount": 9}}}
             for i in range(count)]
    return ('<script type="application/ld+json">'
            + json.dumps({"@type": "ItemList", "itemListElement": items})
            + "</script>")


def _bp_page(count=16):
    items = [{"item": {"@type": "Product", "name": f"bp item {i}",
                       "url": f"https://www.bestprice.gr/item/{9000+i}/x.html",
                       "offers": {"lowPrice": 20 + i, "offerCount": 5}}}
             for i in range(count)]
    return ('<script type="application/ld+json">'
            + json.dumps({"@type": "ItemList", "itemListElement": items})
            + "</script>")


class SearchPaginationTests(unittest.TestCase):
    """--limit above one search page must actually fetch more, not silently cap."""

    def setUp(self):
        self._orig_browser = grprice.USE_BROWSER
        self.addCleanup(lambda: setattr(grprice, "USE_BROWSER", self._orig_browser))

    def test_skroutz_paginates_to_reach_the_limit(self):
        pages = []

        def fake_fetch(url, ttl=None, xhr=False):
            m = re.search(r"[?&]page=(\d+)", url)
            n = int(m.group(1)) if m else 1
            pages.append(n)
            return _sk_page(n)

        with unittest.mock.patch.object(grprice, "fetch", side_effect=fake_fetch):
            rows = grprice.skroutz_search("wifi camera", limit=100)
        self.assertEqual(len(rows), 100)
        self.assertGreaterEqual(len(pages), 3, f"only fetched pages {pages}")
        self.assertEqual(len(rows), len({r["url"] for r in rows}), "duplicate urls")

    def test_skroutz_single_page_when_limit_fits(self):
        pages = []

        def fake_fetch(url, ttl=None, xhr=False):
            pages.append(url)
            return _sk_page(1)

        with unittest.mock.patch.object(grprice, "fetch", side_effect=fake_fetch):
            rows = grprice.skroutz_search("x", limit=10)
        self.assertEqual(len(rows), 10)
        self.assertEqual(len(pages), 1, "should not fetch page 2 when page 1 suffices")

    def test_skroutz_stops_when_a_page_adds_nothing_new(self):
        """A site that keeps serving page 1 must not loop forever."""
        calls = []

        def fake_fetch(url, ttl=None, xhr=False):
            calls.append(url)
            return _sk_page(1)          # same items every time

        with unittest.mock.patch.object(grprice, "fetch", side_effect=fake_fetch):
            rows = grprice.skroutz_search("x", limit=500)
        self.assertEqual(len(rows), 48)
        self.assertLessEqual(len(calls), grprice.MAX_SEARCH_PAGES + 1)

    def test_bestprice_stays_on_http_within_the_jsonld_cap(self):
        grprice.USE_BROWSER = True
        with unittest.mock.patch.object(grprice, "fetch", return_value=_bp_page()), \
             unittest.mock.patch.object(grprice, "_bestprice_scroll_ids") as scroll:
            rows = grprice.bestprice_search("x", limit=16)
        self.assertEqual(len(rows), 16)
        scroll.assert_not_called()

    def test_bestprice_harvests_ids_beyond_the_cap(self):
        grprice.USE_BROWSER = True
        extra = [str(7000 + i) for i in range(40)]
        with unittest.mock.patch.object(grprice, "fetch", return_value=_bp_page()), \
             unittest.mock.patch.object(grprice, "_bestprice_scroll_ids",
                                        return_value=extra) as scroll:
            rows = grprice.bestprice_search("x", limit=50)
        scroll.assert_called_once()
        self.assertEqual(len(rows), 50)
        self.assertEqual(len(rows), len({r["url"] for r in rows}))
        harvested = [r for r in rows if r.get("from_pagination")]
        self.assertTrue(harvested)
        self.assertTrue(all(r["url"].startswith("https://www.bestprice.gr/item/")
                            for r in harvested))
        self.assertIsNone(harvested[0]["price_from"],
                          "harvested rows carry no price until detailed")

    def test_bestprice_does_not_scroll_without_a_browser(self):
        grprice.USE_BROWSER = False
        with unittest.mock.patch.object(grprice, "fetch", return_value=_bp_page()), \
             unittest.mock.patch.object(grprice, "_bestprice_scroll_ids") as scroll:
            rows = grprice.bestprice_search("x", limit=50)
        self.assertEqual(len(rows), 16)
        scroll.assert_not_called()

class LocalisedChallengeTests(unittest.TestCase):
    """Cloudflare localises its challenge, and we ask for el-GR.

    The wait loop matched only the English "Just a moment...", so a Greek
    challenge page ("Περιμένετε...") looked like a *cleared* page: the loop
    exited instantly and the run was declared blocked without ever waiting.
    """

    def test_greek_interstitial_is_recognised(self):
        html = "<html><head><title>Περιμένετε...</title></head><body></body></html>"
        self.assertEqual(grprice._challenge_reason(None, html), "cloudflare")

    def test_greek_turnstile_prompt_is_recognised(self):
        html = "<html><body>Επαληθεύστε ότι είστε άνθρωπος</body></html>"
        self.assertEqual(grprice._challenge_reason(None, html), "cloudflare")

    def test_english_forms_still_recognised(self):
        for html in ("<title>Just a moment...</title>",
                     "<title>Attention Required! | Cloudflare</title>"):
            self.assertEqual(grprice._challenge_reason(None, html), "cloudflare")

    def test_a_real_page_is_not_mistaken_for_a_challenge(self):
        html = ("<html><head><title>wifi camera | Skroutz.gr</title></head>"
                "<body><script type=\"application/ld+json\">{}</script></body></html>")
        self.assertIsNone(grprice._challenge_reason(None, html))

    def test_challenge_titles_cover_both_languages(self):
        self.assertTrue(grprice._is_challenge_title("Περιμένετε..."))
        self.assertTrue(grprice._is_challenge_title("Just a moment..."))
        self.assertFalse(grprice._is_challenge_title("wifi camera | Skroutz.gr"))

class PageReadRaceTests(unittest.TestCase):
    """Answering a challenge navigates the page mid-read. That is success."""

    def test_retries_while_navigating_then_returns_content(self):
        calls = []

        class P:
            def content(self_i):
                calls.append(1)
                if len(calls) < 3:
                    raise Exception("Page.content: Unable to retrieve content "
                                    "because the page is navigating and changing "
                                    "the content.")
                return "<html>real page</html>"
            def wait_for_load_state(self_i, *a, **k): pass
            def wait_for_timeout(self_i, ms): pass

        self.assertEqual(grprice._page_html(P()), "<html>real page</html>")
        self.assertEqual(len(calls), 3)

    def test_unrelated_errors_are_not_swallowed(self):
        class P:
            def content(self_i):
                raise Exception("target crashed")
            def wait_for_load_state(self_i, *a, **k): pass
            def wait_for_timeout(self_i, ms): pass

        with self.assertRaises(Exception) as ctx:
            grprice._page_html(P())
        self.assertIn("target crashed", str(ctx.exception))

class AttachedBrowserTests(unittest.TestCase):
    """Attaching to a browser the user started, rather than launching one.

    Playwright-launched browsers -- bundled Chromium and real Chrome alike --
    hit an endless "verify you are human" loop from an IP whose ordinary
    browser loaded the same page unchallenged. Attaching clears it instantly.
    """

    def setUp(self):
        self._orig = (grprice._browser, grprice.CDP_URL)
        grprice._browser = None
        self.addCleanup(self._restore)

    def _restore(self):
        grprice._browser, grprice.CDP_URL = self._orig

    def _fake_pw(self, browser):
        pw = unittest.mock.MagicMock()
        pw.chromium.connect_over_cdp.return_value = browser
        return pw

    @staticmethod
    def _stub_playwright(pw):
        """Playwright is an optional dependency, so the suite must run without it."""
        import types
        pkg = types.ModuleType("playwright")
        mod = types.ModuleType("playwright.sync_api")
        mod.sync_playwright = lambda: unittest.mock.MagicMock(start=lambda: pw)
        pkg.sync_api = mod
        return unittest.mock.patch.dict(
            sys.modules, {"playwright": pkg, "playwright.sync_api": mod})

    def test_cdp_url_attaches_instead_of_launching(self):
        grprice.CDP_URL = "http://127.0.0.1:9222"
        ctx = unittest.mock.MagicMock()
        browser = unittest.mock.MagicMock(contexts=[ctx])
        pw = self._fake_pw(browser)
        with self._stub_playwright(pw):
            got = grprice._browser_context()
        self.assertIs(got, ctx)
        pw.chromium.connect_over_cdp.assert_called_once_with("http://127.0.0.1:9222")
        pw.chromium.launch_persistent_context.assert_not_called()

    def test_closing_never_kills_a_browser_we_did_not_start(self):
        """Their browser, their tabs. Detach, do not close."""
        grprice.CDP_URL = "http://127.0.0.1:9222"
        ctx = unittest.mock.MagicMock()
        browser = unittest.mock.MagicMock(contexts=[ctx])
        pw = self._fake_pw(browser)
        with self._stub_playwright(pw):
            grprice._browser_context()
        grprice._close_browser()
        ctx.close.assert_not_called()
        browser.close.assert_called_once()

    def test_launched_browser_is_closed_normally(self):
        """With no browser discoverable, Playwright launches its own."""
        grprice.CDP_URL = ""
        ctx = unittest.mock.MagicMock()
        pw = unittest.mock.MagicMock()
        pw.chromium.launch_persistent_context.return_value = ctx
        with self._stub_playwright(pw), \
             unittest.mock.patch.object(grprice, "find_browser", return_value=None):
            grprice._browser_context()
        grprice._close_browser()
        ctx.close.assert_called_once()

class LaunchedBrowserProfileTests(unittest.TestCase):
    """A Windows browser driven from WSL cannot open a Linux profile path.

    It does not complain -- it just never opens the debug port, which reads as
    "the browser is broken" rather than "you passed a path it cannot use".
    """

    def test_windows_exe_gets_a_windows_profile_path(self):
        got = grprice._cdp_profile_dir(
            "/mnt/c/Users/HixPC/AppData/Local/BraveSoftware/Brave-Browser/"
            "Application/brave.exe")
        self.assertTrue(got.startswith("C:\\Users\\HixPC"), got)
        self.assertNotIn("/", got)

    def test_linux_exe_keeps_a_linux_path(self):
        got = grprice._cdp_profile_dir("/usr/bin/google-chrome")
        self.assertTrue(got.startswith("/"), got)

    def test_explicit_override_wins(self):
        with unittest.mock.patch.dict(os.environ,
                                      {"GRPRICE_CDP_PROFILE": "D:\\somewhere"}):
            self.assertEqual(grprice._cdp_profile_dir("/mnt/c/Users/x/brave.exe"),
                             "D:\\somewhere")

class BrowserDiscoveryTests(unittest.TestCase):
    """Using your own browser is the default, so it has to be found for you."""

    def test_env_override_wins_over_discovery(self):
        with unittest.mock.patch.dict(os.environ,
                                      {"GRPRICE_BROWSER_EXE": "/custom/brave"}):
            self.assertEqual(grprice.find_browser(), "/custom/brave")

    def test_prefers_brave_then_chrome_then_edge(self):
        present = {"/mnt/c/Users/x/AppData/Local/Microsoft/Edge/Application/msedge.exe",
                   "/mnt/c/Users/x/AppData/Local/Google/Chrome/Application/chrome.exe",
                   "/mnt/c/Users/x/AppData/Local/BraveSoftware/Brave-Browser/"
                   "Application/brave.exe"}
        with unittest.mock.patch.dict(os.environ, {}, clear=False), \
             unittest.mock.patch.object(grprice, "_candidate_browsers",
                                        return_value=sorted(present, key=lambda c: (
                                            "brave" not in c, "chrome" not in c))):
            got = grprice.find_browser()
        self.assertIn("brave", got.lower())

    def test_returns_none_when_nothing_is_installed(self):
        grprice._browser_exe_cache.clear()
        self.addCleanup(grprice._browser_exe_cache.clear)
        with unittest.mock.patch.object(grprice, "BROWSER_EXE", ""), \
             unittest.mock.patch.object(grprice, "_candidate_browsers", return_value=[]):
            env = {k: v for k, v in os.environ.items() if k != "GRPRICE_BROWSER_EXE"}
            with unittest.mock.patch.dict(os.environ, env, clear=True):
                self.assertIsNone(grprice.find_browser())

    def test_discovery_only_returns_paths_that_exist(self):
        found = grprice._candidate_browsers()
        for c in found:
            self.assertTrue(os.path.exists(c), f"{c} does not exist")


class BrowserDefaultTests(unittest.TestCase):
    """The browser path is on unless explicitly turned off."""

    def test_browser_is_on_by_default(self):
        self.assertTrue(grprice.USE_BROWSER,
                        "reading Skroutz needs a browser; it should not need a flag")

    def test_track_check_never_opens_a_browser(self):
        """The watch list is meant for cron. Cron must not launch a GUI."""
        seen = {}

        def fake_search(query, source="both", limit=8):
            seen["browser"] = grprice.USE_BROWSER
            return []

        with unittest.mock.patch.object(grprice, "_search", side_effect=fake_search), \
             unittest.mock.patch.object(grprice, "load_state",
                                        return_value={"items": [{"query": "x"}]}), \
             unittest.mock.patch.object(grprice, "save_state"):
            grprice.track_check()
        self.assertFalse(seen.get("browser", True),
                         "track check ran with the browser enabled")

class BrowserShutdownTests(unittest.TestCase):
    """Closing a browser we launched, without touching one we did not.

    On WSL a Windows browser is started through an interop shim, so terminating
    the process we spawned reaps the shim and leaves the real browser running.
    Windows showed nine brave.exe while WSL's ps showed three. The fix is to
    ask the browser to quit over its own debug session -- killing by image name
    is not an option, because the user's own windows share it.
    """

    def setUp(self):
        self._orig = (grprice._browser, grprice._launched_proc, grprice.CDP_URL)
        grprice._browser = None
        grprice._launched_proc = None
        self.addCleanup(self._restore)

    def _restore(self):
        grprice._browser, grprice._launched_proc, grprice.CDP_URL = self._orig

    def test_browser_we_launched_is_told_to_quit(self):
        attached = unittest.mock.MagicMock()
        session = attached.new_browser_cdp_session.return_value
        proc = unittest.mock.MagicMock()
        grprice._browser = (unittest.mock.MagicMock(), unittest.mock.MagicMock(), attached)
        grprice._launched_proc = proc

        grprice._close_browser()

        session.send.assert_called_once_with("Browser.close")
        proc.terminate.assert_called_once()

    def test_browser_we_attached_to_is_only_disconnected(self):
        attached = unittest.mock.MagicMock()
        grprice._browser = (unittest.mock.MagicMock(), unittest.mock.MagicMock(), attached)
        grprice._launched_proc = None            # not ours to close

        grprice._close_browser()

        attached.new_browser_cdp_session.assert_not_called()
        attached.close.assert_called_once()

class InterpreterTests(unittest.TestCase):
    """Playwright lives in a venv; the caller should not have to know which.

    Run with a system python that lacks it and Skroutz silently drops out of
    the results -- the run reports complete=False and half the market, and the
    reason is buried in a warning. The script re-launches itself into an
    interpreter that has it instead.
    """

    def test_finds_a_venv_that_has_playwright(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            good = root / "good"
            (good / "lib" / "python3.12" / "site-packages" / "playwright").mkdir(parents=True)
            (good / "bin").mkdir(parents=True)
            (good / "bin" / "python").touch(mode=0o755)
            bare = root / "bare"
            (bare / "bin").mkdir(parents=True)
            (bare / "bin" / "python").touch(mode=0o755)

            self.assertIsNone(grprice._python_with_playwright([bare]))
            self.assertEqual(grprice._python_with_playwright([bare, good]),
                             str(good / "bin" / "python"))

    def test_ignores_a_venv_without_the_package(self):
        with TemporaryDirectory() as d:
            bare = Path(d) / "v"
            (bare / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
            (bare / "bin").mkdir(parents=True)
            (bare / "bin" / "python").touch(mode=0o755)
            self.assertIsNone(grprice._python_with_playwright([bare]))

    def test_candidates_include_the_cache_venv(self):
        cands = [str(c) for c in grprice._venv_candidates()]
        self.assertTrue(any("grprice" in c and "venv" in c for c in cands),
                        f"cache venv not searched: {cands}")

    def test_reexec_does_not_loop(self):
        """The relaunch must mark itself, or it would re-exec forever."""
        calls = []
        with unittest.mock.patch.dict(os.environ, {grprice.REEXEC_FLAG: "1"}), \
             unittest.mock.patch.object(os, "execv",
                                        side_effect=lambda *a: calls.append(a)):
            grprice.ensure_playwright()
        self.assertEqual(calls, [], "re-exec ran again despite the guard")

if __name__ == "__main__":
    unittest.main(verbosity=2)
