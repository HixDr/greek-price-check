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
        grprice.CACHE_DIR = Path(self._tmp.name)
        grprice._last_request.clear()
        grprice._blocked_hosts.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        grprice.CACHE_DIR = self._orig_cache
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
                                        return_value=FakeCtx()):
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

if __name__ == "__main__":
    unittest.main(verbosity=2)
