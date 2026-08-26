# CLAUDE.md

Context for Claude Code working on this repository.

## What this is

A daily inventory pipeline for Silverpot Tea (operating entity: DCG North
America LLC). It reads stock levels from Amazon FBA and Walmart WFS, stores an
append-only history in Google Sheets, publishes a JSON feed, and will eventually
write availability into the silverpottea.com database.

Silverpot sells 36 SKUs in the US only: 31 tins (50 count, $13.99) and 5 pouches
(100 count, $21.99). Channels are Amazon, Walmart, Weee!, and the DTC site.

## The core rule: what is publishable

```
published_available = FBA.fulfillableQuantity + WFS.availableUnits - safety_buffer
```

Multi-channel fulfillment means both marketplace pools can ship DTC orders, so
summing them is legitimate. **Nothing else is sellable.** Inbound, reserved
(customer order / transshipment / FC processing), unfulfillable and researching
quantities are stored for forecasting and must never be published.

The safety buffer exists because the feed is up to 24 hours stale and both pools
drain from marketplace orders in the interim. Default `max(2, 5% of raw)`,
overridable per SKU in `sku_map.csv`.

**Do not remove or weaken these guards:**

- A degraded run (any marketplace failing) skips the website push entirely.
  Publishing FBA-only or WFS-only numbers would understate real stock.
- If both marketplaces fail, nothing is written at all and the previous data is
  left untouched.
- `snapshots` is append-only. Never overwrite it. The time series is what makes
  days-of-cover possible, and days-of-cover is the reorder trigger that matters
  once FCL ocean freight puts 60-90 days between decision and stock landing.

## Architecture

```
collector/
  config.py    SKU map loader, dataclasses, constants
  net.py       shared HTTP session: retries, IPv4 forcing, error descriptions
  amazon.py    SP-API FBA inventory (API + Reports fallback) + diagnose()
  walmart.py   Walmart WFS inventory
  sheets.py    Google Sheets sink (snapshots + current tabs)
  website.py   Supabase writer (not yet configured)
  main.py      orchestrator
sku_map.csv    the identity layer - 36 rows, hand-maintained
public/        static dashboard + inventory.json feed
tests/         payload-shape tests; no credentials, no network
.github/workflows/inventory.yml   daily cron 05:30 UTC
.github/workflows/tests.yml       unit tests on every push
```

`internal_code` (`2201US`, `2301US`) is the join key for the whole system and
matches Eshan's existing Flask sales tracker. The `sku` column is the primary
marketplace SKU and serves Walmart too; `walmart_sku_override` exists for when
that stops being true.

**One product can hold stock under several Amazon SKUs at once.** Converting a
listing to stickerless (commingled) inventory issues a *second* seller SKU -
Silverpot's carry a `-stickerless` suffix - and Amazon reports each pool
separately under its own SKU. Both ship the same tea, so
`amazon_sku_aliases` (`;` or `|` separated) lists the extra SKUs and
`main.build()` **sums** every matching pool into one `fba_fulfillable`.

Two things guard this, because the failure mode is silent - unmatched stock
just never appears, with no error anywhere:

- Loading the map fails hard if two rows claim the same Amazon SKU. One SKU
  landing on two products would attribute one tea's stock to another.
- A run warns when an Amazon SKU holds fulfillable stock and no row claims it
  (`WARN ... not in sku_map.csv, so it is NOT published`). That is the
  reverse of the long-standing "no FBA match" warning, and it is how a newly
  issued SKU gets noticed. Unmapped SKUs holding zero are counted, not listed.

Do not fold the suffix into the code as a rule. Amazon's naming is not a
contract, and the alias column keeps a guess out of the join.

## Current status

**Walmart: working.** 36 SKUs. The correct field is
`payload.inventory[].inventoryData.availableUnits` (note `onhandUnits` has a
lowercase h). Walmart's docs do not name these fields; they were found by
dumping a live response with `--probe`.

**Amazon: working since 2026-08-26.** 36 SKUs via the direct FBA Inventory
API (`fba_source: fba-inventory-api`). Combined with Walmart the feed now
publishes 1132 units across 36 SKUs, 9 at zero.

How the 403 was resolved, because the reasoning was wrong twice on the way:

- `/fba/inventory/v1/summaries` is guarded by the **Amazon Fulfillment** role,
  not *Inventory and Order Tracking*. The 2026-08-19 approval (case
  21676039541) was for the latter, which covers the Orders API and order
  tracking reports. LWA fine, endpoint denied - that is the signature.
- A refresh token **keeps the roles it was minted with**. Self-authorizing
  twice against an unchanged role set could never have helped. What finally
  fixed it was minting a fresh refresh token once the roles were right.

Confirmed by `--diagnose` on the live token:

```
[Selling Partner Insights   ] Sellers                HTTP 403
[Amazon Fulfillment         ] FBA Inventory          HTTP 200
[Amazon Fulfillment         ] Fulfillment Outbound   HTTP 200
[Inventory and Order Tracking] Orders                HTTP 200
[Product Listing            ] Catalog Items          HTTP 403
[Reports API                ] Reports                HTTP 200
```

Product Listing and Selling Partner Insights are **not** granted, and nothing
in this pipeline needs them. Do not chase those 403s.

Two things in the code exist because of this episode:

- `--diagnose` probes one endpoint per role and prints which roles the token
  actually carries. Read its verdict carefully: the target endpoint is judged
  first and on its own. An earlier version keyed the verdict off the Sellers
  canary, which it wrongly labelled role-free, and announced "the app is not
  live" while FBA Inventory was answering 200. **There is no genuinely
  role-free SP-API endpoint** - Sellers needs Selling Partner Insights.
- `amazon.fetch_fba_inventory()` falls back to the Reports API
  (`GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA`, then `GET_AFN_INVENTORY_DATA`)
  on a 403. It is slower and has no reserved breakdown, and both reports also
  sit under Amazon Fulfillment, so it is insurance against a role being
  revoked, not a way around one that was never granted. The route used is
  recorded in `public/inventory.json` as `fba_source`, so every published
  number is traceable to how it was obtained.

**Website push: not configured.** Waiting on Supabase table and column names
from the silverpottea.com Lovable project. See `LOVABLE_PROMPT.md`.

## Running it

```bash
pip install -r requirements.txt

python -m collector.main --diagnose   # name the missing Amazon role, write nothing
python -m collector.main --probe      # dump raw API responses, write nothing
python -m collector.main --dry-run    # compute and print, write nothing
python -m collector.main              # full run

python -m unittest discover -s tests  # no credentials or network needed
```

Credentials come from environment variables. Locally, put them in `.env`
(already gitignored) and source it. In CI they are GitHub Actions secrets.

```
LWA_CLIENT_ID  LWA_CLIENT_SECRET  LWA_REFRESH_TOKEN
WALMART_CLIENT_ID  WALMART_CLIENT_SECRET
GOOGLE_SERVICE_ACCOUNT_JSON  GOOGLE_SHEET_ID
FORCE_IPV4=true
```

Optional, for the website push once known: `SUPABASE_URL`,
`SUPABASE_SERVICE_KEY`, `SUPABASE_TABLE`, `SUPABASE_SKU_COLUMN`,
`SUPABASE_QTY_COLUMN`.

## Rules for changes

- **Never commit `.env`, service account JSON, or any credential.** If one is
  found in the repo or in git history, stop and say so immediately.
- **Never print a secret** in logs or diagnostic output. Print lengths and
  prefixes if a value needs checking.
- Test against realistic payloads before claiming something works. The bugs in
  this repo so far were a stale dict key that crashed the sheet write and nine
  wrong guesses at a Walmart field name. Both would have been caught by testing
  against a real response shape.
- Do not hardcode API field names discovered by guessing. Keep the
  candidate-list-plus-fallback pattern in `walmart.py` and lead with confirmed
  names.
- Prefer failing loudly and writing nothing over writing something plausible but
  wrong. Wrong stock numbers on a storefront cost real money.

## Communication style

Eshan wants the deliverable first, minimal preamble, plain language, and steps
in order. Concede errors immediately rather than defending them. He pushes back
precisely when analysis is wrong, and expects claims to be verified against data
rather than asserted.
