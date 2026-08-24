---
name: greek-price-check
description: Compares prices for a product across Skroutz.gr and BestPrice.gr, the two Greek price-comparison marketplaces. Use when the user is shopping in Greece and wants to know what something costs, which shop is cheapest, whether a price is good, or which model to buy - e.g. "what does a wifi camera cost", "find me a good 1TB NVMe", "is this a good price", "which site is cheaper for X", "τιμη για ...", "ψαχνω καμερα". Also use for cross-site price gaps, price-drop tracking on a watch list, and price history. Combines live Greek pricing with web research on specs and reviews to recommend a specific model.
---

# Greek price comparison (Skroutz + BestPrice)

Wraps `scripts/grprice.py`, which reads the schema.org JSON-LD both sites publish.
Stdlib only except the optional browser transport. The script gathers raw data;
you do the comparing.

## How this works

The script **gathers and decides nothing**. It searches both sites, pulls full
detail on every hit, and writes a run folder with one `REPORT.txt`. You do all
the comparing: which products are equivalent, which is better value, what a
price gap means. There is no scoring, matching, dedupe or top-N in the code —
deliberately, because those are your judgements to make and explain.

## The main flow

```bash
python3 scripts/grprice.py gather "wifi camera εξωτερικου χωρου" --browser --plus
```

It prints the report path. **Read that file**, then recommend.

```
~/.cache/grprice/runs/2026-08-24-wifi-camera-.../
  REPORT.txt        <- read this
  manifest.json     what ran, timings, per-fetch status
  raw/              one JSON per fetch, for tracing a claim back to the source
```

- `--browser` is **required for Skroutz** — it is behind Cloudflare and refuses
  plain HTTP. Without it you get BestPrice only. If `GRPRICE_BROWSER_EXE` is
  set, the tool starts that browser off-screen and closes it when done;
  otherwise it falls back to a Playwright-launched one, which Skroutz's bot
  protection tends to challenge in a loop. Headless never works.
- `--plus` — this user has Skroutz Plus. Always pass it (see Shipping below).
- `--limit N` (default 16) is hits per source, and the only cost dial: **every**
  hit gets a full detail fetch. Measured: the default yields 32 candidates in
  ~2 minutes. That breadth is what a confident recommendation needs — don't
  lower it just to be quick. BestPrice search pages top out at 16, so raising it past the default
  only adds Skroutz results at ~20s each. Use `--limit 4` only when the user
  explicitly wants a fast answer or names one specific product.
- `--max-price N` skips detail above a price. The report says how many it hid.
- `--source skroutz|bestprice|both` — use `bestprice` alone for a fast
  price-only answer with no browser.

Other commands: `offers <url>` for one product, `history <term>` for accumulated
price history, `track add|check|list` for the watch list, `login` if you ever
want a signed-in browser session (you don't need one — search is public).

## Reading REPORT.txt

The header states provenance. **Check it before you say anything comparative:**

```
gathered 2026-08-24 19:32  |  bestprice: ok  |  skroutz: ok
16/source requested → 32 hits → 32 detailed, 0 failed
Skroutz Plus: on  |  skroutz via browser
```

- A source marked `blocked` or `error`, or an `INCOMPLETE` flag, means the rows
  are **one site's view of the market, not the market**. Never say "the
  cheapest" or "cheaper on X" off a partial run — say which site is missing.
- A candidate marked `DETAIL FAILED` kept only its search-row fields. Don't read
  absent specs as "this model lacks that feature".

What each source gives you — they are complementary, so use both:

| | Skroutz | BestPrice |
|---|---|---|
| specs | ~25-30 fields | ~10 fields |
| reviews | rating **+ real review text** | rating often absent |
| description | omitted (SEO boilerplate) | real editorial copy |
| identity | `model`, `mpn` | `gtin` |
| shops | some anonymised | all named |

**Skroutz is usually the only source of qualitative signal.** Its review text
and rating counts are frequently the deciding evidence when two models are close
on price and specs. BestPrice often has `rating: null`.

### Matching products across the sites — your job now

Nothing pairs them for you. Use, in descending order of reliability:

1. `gtin` on both sides — same barcode, same product. (BestPrice only publishes
   it, so this works only when comparing two BestPrice rows.)
2. Skroutz `mpn` / `model` against the BestPrice `name` — `mpn: "Tapo C520WS"`
   appearing in a BestPrice title is strong.
3. Spec agreement — resolution, lens size, IP rating, capacity.
4. Names alone, last resort. Watch for `v1`/`v2`, Pro/Ultra/Kit/Edition, and
   capacity differences: those are *different products*, not variants.

Say how confident you are and why. If you cannot pair them, say so rather than
comparing prices across two different products.

### Judging prices

- **Quote delivered totals, not list prices.** Each candidate has a `delivered`
  line; when `cheapest-listing-is-cheapest-delivered: False`, the headline price
  belongs to a different shop than the actual best deal. Say so.
- A gap of a euro or two is noise. Don't present it as a finding.
- Many shops (40+) means a mainstream product with real competition; 2-3 means a
  grey import or near-EOL listing where a cheap price may be stale.


## Shipping and Skroutz Plus

**This user has Skroutz Plus and no BestPrice subscription.** That asymmetry
matters: a raw price comparison flatters BestPrice, because its rows carry a
courier fee the user will actually pay while most qualifying Skroutz orders ship
free. Always pass `--plus` when touching Skroutz, or rely on
`GRPRICE_SKROUTZ_PLUS=1` if they've exported it.

- With `--plus`, Skroutz rows at or above the free-shipping floor (€25 to an
  address, €15 to a Skroutz Point; `--delivery point` switches) get `shipping: 0`
  and a `shipping_note` saying the subscription is why. Below the floor, the
  script falls back to whatever fee the listing shows.
- BestPrice shipping is per shop, read from the listing rows, and is real money
  for this user. `cod_fee` is the extra charge for cash on delivery — mention it
  only if they bring up payment method.
- `shipping: null` means the site didn't state a fee (Skroutz varies it by
  address). Treat the total as unknown rather than assuming free, and say so.
- Reordering is the whole point. On a real run, a €699 listing beat a €698 one
  once delivery was counted. Never rank on list price when totals are available.

## Fill the gaps with web search

The script gives prices, sellers, and whatever specs the site happened to publish.
That is often not enough to actually recommend something. Spec coverage is uneven —
a phone may carry ~45 fields while a camera carries 7 — the values are Greek
marketing copy rather than measurements, and neither site says anything about
long-term reliability, firmware, or app quality.

So search the web whenever any of these is true:

- The SPECS block is thin (roughly under 10 fields) and the choice turns on specs.
- The user asks about something absent from `specs` — local SD recording, HomeKit
  or Home Assistant support, PoE, subscription requirements, codec, panel type.
- Two candidates are close on price and you need a tiebreaker.
- A product looks unfamiliar, or the listing title is ambiguous about which
  generation or regional variant it is.
- The price is a conspicuous outlier and you suspect an EOL model or grey import.

Search the manufacturer's spec page first, then independent reviews. For Greek
listings, search the model code rather than the Greek title — `TP-Link Tapo C520WS
specifications` works, the full Greek listing name does not.

Two rules when you do:

1. **Keep provenance separate.** Prices and seller names come from the script;
   specs and verdicts may come from the web. Say which is which. Never fold a
   web-sourced spec into the JSON as though the site reported it.
2. **The script wins on price, the web wins on quality.** If a review quotes a
   price, ignore it — it's the wrong country or out of date. Live Greek pricing is
   what the script is for.

Don't search when the answer is already in the output, or when the user just asked
what something costs.

## How to advise

The user wants a decision, not a table. So:

1. Name one recommendation and one runner-up, each with a price and a reason.
2. Justify with specs and review counts from the output, not just the lowest price;
   pull in web-sourced detail where the listing data is too thin to decide.
3. Flag the cheapest listing separately if it's from a shop with few reviews —
   cheapest and best-value are usually different rows.
4. Include the product URL so they can check shipping and stock themselves.
5. Never invent a spec, stock status, or shipping cost. If it isn't in the JSON,
   either look it up and attribute it, or say it's unknown.

## Troubleshooting

- **Skroutz returns a Cloudflare challenge (`state: "blocked"`)** — Skroutz is
  behind Cloudflare Bot Management and refuses plain HTTP clients. **The fix is
  `--browser`**, which renders the page in a real (headed) browser; that returns
  `200` with the JSON-LD intact. Do *not* tell the user to wait and retry — this
  is not a rate limit and waiting never clears it. Header/User-Agent tuning is
  also a dead end, and headless is hard-blocked; both were tested.
  Needs `pip install playwright && playwright install chromium` once, plus a
  display (WSLg or X on WSL2). No Skroutz account is needed — search results are
  public, so run it logged out.
- **When to spend the browser.** It renders a full page per query, so it is
  slower and costs Skroutz real work. Default to `--source bestprice` for "what
  does X cost". Reach for `--browser` when Skroutz is actually load-bearing:
  a cross-site comparison, review counts as a popularity signal, or Plus
  shipping economics. Never in a loop or a cron job.
- **Empty results for a fluent Greek phrase** — both sites do keyword matching,
  not natural language. The script strips filler words and retries automatically;
  if it still fails, search the bare brand plus model.
- **Stale prices** — responses are cached for 15 minutes in `~/.cache/grprice`.
  Pass `--no-cache` when the user is about to buy.

## Etiquette

Personal-use tool. Keep runs occasional, leave the pacing alone, and don't loop it
over large catalogues — that's what gets an IP banned and it's against both sites'
terms. If this ever needs to run continuously, ask Skroutz for API access
(api@skroutz.gr) instead of scaling the scraper up.
