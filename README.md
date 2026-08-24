# greek-price-check

Compares prices across **Skroutz.gr** and **BestPrice.gr**, the two Greek
price-comparison marketplaces.

It searches both sites, pulls full detail on every result — specs, per-shop
prices, delivery costs, ratings, review text — and writes the lot to one report.
Claude reads that report and recommends what to buy.

The script gathers. It doesn't rank, match, or filter. That's deliberate: those
are judgement calls, and Claude can weigh them and explain itself where a
scoring function can't.

## Install

Clone it as a Claude Code skill:

```bash
git clone https://github.com/HixDr/greek-price-check.git ~/.claude/skills/greek-price-check
```

Restart Claude Code, then just ask — *"what's a good wifi camera under €60?"*
For one project only, clone to `<project>/.claude/skills/greek-price-check`.

Reading Skroutz needs a real browser, because it sits behind Cloudflare and
refuses plain HTTP clients:

```bash
pip install playwright && playwright install chromium
```

That's the only dependency; everything else is Python 3.10+ stdlib. On WSL2 you
need WSLg or an X server — a browser window opens while it runs. No Skroutz
account is needed.

## Run

```bash
scripts/grprice.py gather "wifi camera" --browser --plus
```

It prints where the report landed:

```
~/.cache/grprice/runs/2026-08-24-wifi-camera/REPORT.txt
32 candidates from 32 hits  (0 failed)  complete=True
```

```
runs/2026-08-24-wifi-camera/
  REPORT.txt        every candidate in full — this is the thing to read
  manifest.json     what ran, timings, what failed
  raw/              one JSON per fetch, to trace any line back to its source
```

### Flags

| Flag | Meaning |
|---|---|
| `--browser` | **required for Skroutz.** Without it you get BestPrice only |
| `--plus` | you have Skroutz Plus — cost qualifying orders as free delivery |
| `--limit N` | hits per source (default 16). The only cost dial |
| `--max-price N` | skip detail on hits above this price |
| `--source` | `skroutz` \| `bestprice` \| `both` (default) |
| `--no-cache` | bypass the 15-minute cache |
| `--json` | machine-readable summary on stdout |

**`--limit` is what a run costs you.** Every hit gets a full detail fetch —
Skroutz ~20s each through the browser, BestPrice ~2s. The default of 16 is
chosen because BestPrice search pages top out at 16 results, so it captures that
site completely; a measured run gave **32 candidates in 1m55s** (~130KB of
report). Raising it adds Skroutz only (~48 available there). Drop to
`--limit 4` for a quick look.

The two sites are fetched concurrently, and BestPrice details are pooled to
overlap round-trips. Both sites are still rate-limited to one request per second
each — the pool hides latency, it does not issue requests faster.

### Other commands

```bash
scripts/grprice.py offers "<product url>"        # one product, per-shop prices
scripts/grprice.py history "tapo"                # price history, built up over time
scripts/grprice.py track add "nothing phone 4a pro" --target 520
scripts/grprice.py track check                   # watch list; cron-safe, no browser
scripts/grprice.py login                         # optional signed-in browser session
```

## Notes

- **Skroutz needs `--browser`, always.** It answers plain HTTP clients with a
  Cloudflare challenge on the very first request — not a rate limit, so waiting
  and retrying never helps. Headless is blocked outright; headed works. Details
  and measurements in `docs/`.
- **The two sites are complementary.** Skroutz carries more specs (~29 vs ~10),
  ratings, and real review text; BestPrice names every seller and writes proper
  editorial descriptions. Use both.
- **Be proportionate.** A browser render costs Skroutz real work. Keep runs
  interactive and personal-scale — don't cron `gather`, don't loop it over a
  catalogue. Scraping is against both sites' terms; the supported route is the
  official API — request access at api@skroutz.gr. Note that Skroutz removed the
  public v3 documentation from developer.skroutz.gr in 2022, so ask them for
  current docs along with credentials.

## Config

| Env var | Default | Meaning |
|---|---|---|
| `GRPRICE_CACHE` | `~/.cache/grprice` | cache, runs, and watch-list directory |
| `GRPRICE_TTL` | `900` | cache lifetime in seconds |
| `GRPRICE_BROWSER` | unset | `1` to always read Skroutz through a browser |
| `GRPRICE_SKROUTZ_PLUS` | unset | `1` if you have Skroutz Plus |
| `GRPRICE_DELIVERY` | `address` | `address` or `point`, for Plus thresholds |
| `GRPRICE_PLUS_ADDRESS_MIN` | `25` | Plus free-shipping floor to an address |
| `GRPRICE_PLUS_POINT_MIN` | `15` | Plus free-shipping floor to a Skroutz Point |
| `GRPRICE_CHALLENGE_WAIT` | `25` | seconds to let a challenge clear in the browser |
