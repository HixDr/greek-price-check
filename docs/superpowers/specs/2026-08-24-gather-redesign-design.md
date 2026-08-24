# gather redesign — move judgement out of the code

**Date:** 2026-08-24
**Status:** approved, implementing

## Problem

`grprice.py` makes semantic judgements in Python that an LLM makes better, and
it makes them *destructively* — data is discarded before Claude ever sees it.

The trigger: a real run of `compare "wifi camera" --candidates 4` across both
sites returned **four BestPrice candidates and zero Skroutz**, because
candidates were chosen by sorting the pooled rows on price and taking the top
N. The cheaper site crowded the other out completely, and with it went the only
source of review text and ratings either site publishes. That is not a tuning
bug. It is the predictable outcome of asking `sort()` to answer "which products
should a human compare?".

The same pattern repeats across ~183 lines:

| Code | Judgement it makes | Cost |
|---|---|---|
| `match_score` / `_tokens` | "are these the same product?" | fuzzy, opaque, name-only |
| `cross_match` (0.45 floor) | which pairs are comparable | silently drops non-matches |
| `dedupe` (0.8 threshold) | which listings are variants | deletes rows |
| `_pick_candidates` | which products deserve detail | deletes candidates |
| `COLOR_WORDS`/`NOISE_WORDS`/`_CAP` | which words identify a product | hand-maintained lists |

Claude has the product names, `mpn`, `model`, `gtin`, `brand`, and full spec
maps. It can decide all of the above, explain why, and be corrected. The Python
cannot.

## Approach

The program becomes a **gatherer and formatter**. It fetches, parses, and
writes. It never ranks, matches, scores, or discards.

One new command replaces three:

```
grprice.py gather "wifi camera εξωτερικου χωρου" --browser --plus
```

1. Create `~/.cache/grprice/runs/<date>-<slug>/`
2. Search each requested source; save each raw payload
3. Fetch full detail for **every** hit — no cap, no selection
4. Write `REPORT.txt` and `manifest.json`
5. Print the folder path and a one-line summary

`--limit` (default 16 per source) is the only cost dial, and it belongs to the
user. 16 was chosen after measuring the search pages: BestPrice yields at most
16 hits, Skroutz ~48 — so the default captures BestPrice completely and samples
Skroutz. Measured: 32 candidates in 4m19s.

### Why detail everything

Considered and rejected: a two-phase flow (search → Claude picks → detail), and
a one-shot with a coded per-source cap. Both were cheaper; both put the
selection decision back somewhere. Detailing every hit is the only option where
no code chooses. Accepted costs, explicitly:

- **Skroutz load roughly doubles** — every hit gets a browser render, including
  ones Claude would have dismissed on sight. Pacing (4s) and the 15-minute
  cache still apply. `--limit` is the mitigation.
- **~32k tokens per report** at the default (measured: 130KB). Deemed acceptable.

## Artifacts

```
~/.cache/grprice/runs/2026-08-24-wifi-camera/
  manifest.json                     what ran, timings, per-fetch status
  raw/
    01-search-skroutz.json
    02-search-bestprice.json
    03-sku-skroutz-44813654.json
    04-item-bestprice-2163320025.json
    ...
  REPORT.txt
```

Raw payloads are kept so a claim in the report can be traced to what the site
actually returned, and so one item can be re-read without re-scraping (a fresh
browser render, for Skroutz).

## REPORT.txt

Header carries provenance and completeness; one block per candidate; every
field the parser produced.

```
================================================================================
GREEK PRICE REPORT — "wifi camera εξωτερικου χωρου"
gathered 2026-08-24 18:42  |  skroutz: ok (browser)  |  bestprice: ok (http)
8/source requested → 14 hits → 14 detailed, 0 failed  |  Plus: on (free ≥€25)
================================================================================

--- CANDIDATE 1 of 14 ---------------------------------------------------------
source      skroutz
name        TP-LINK Tapo C520WS v1 IP Κάμερα ...
url         https://www.skroutz.gr/s/44813654/...
brand       TP-LINK    model Tapo C520WS    mpn Tapo C520WS    colour Λευκό
category    Κάμερες Παρακολούθησης
price_from  49.50      rating 4.8 (407 reviews)
delivered   49.50 via Public   cheapest-listing-is-cheapest-delivered: yes

  SPECS (29)
    Χρήση                     IP
    ...
  REVIEWS (5 of 407)
    [5] Γρήγορη παράδοση...
  OFFERS (70)
     #  shop            price   ship   total  eta
     1  Public          49.50   0.00   49.50  1-3 ημέρες
```

Rules:

- Candidates are ordered source-then-price. Ordering is presentation; nothing
  is dropped and no candidate is preferred.
- **A failed detail fetch still gets a candidate block**, marked
  `DETAIL FAILED: <reason>`, carrying whatever the search row held. Nothing
  vanishes silently — that is the whole point of the redesign.
- Skroutz's `description` is SEO boilerplate and is not emitted; BestPrice's is
  editorial and is.

## Deleted

`match_score`, `_tokens`, `cross_match`, `dedupe`, `_pick_candidates`,
`COLOR_WORDS`, `NOISE_WORDS`, `_CAP`, the `search` / `compare` / `cross`
subcommands, `print_search`, `print_cross`.

## Kept

The fetch/cache/pace layer; the browser transport and its challenge detection;
both JSON-LD parsers; `_skroutz_specs`; `_reviews`; `_best_delivered`
(arithmetic, not judgement); `_relax` and `STOPWORDS` (mechanical query
fallback); the `sources` / `complete` provenance tracking; `offers`, `history`,
`track`, `login`.

`track check` stays HTTP/BestPrice by default so a cron job never launches a
browser.

## Testing

1. Every search hit reaches REPORT.txt when both sources answer — the
   crowding-out regression, pinned.
2. A failed detail fetch still produces a candidate block with its reason.
3. Offers tables are complete — no truncation.
4. `manifest.json` records per-fetch status including failures.
5. A blocked source still yields a report from the other, marked incomplete.
6. Existing challenge/transport tests keep passing.

## SKILL.md consequences

The consumer contract changes: Claude runs `gather`, reads `REPORT.txt`, and
does the comparing. Guidance on `confidence` scores, the 0.45/0.8 thresholds,
`matched_query` and "a €3 gap is noise" describes deleted machinery and goes.
What stays: keep price-vs-web provenance separate, fill spec gaps with web
search, and recommend one product with a runner-up.
