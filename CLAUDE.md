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
matches Eshan's existing Flask sales tracker. Amazon and Walmart currently use
identical SKU strings, so one `sku` column serves both;
`walmart_sku_override` exists for when the FNSKU-to-manufacturer-barcode
migration issues new Amazon SKUs while Walmart keeps the old ones.

## Current status

**Walmart: working.** 36 SKUs, ~323 publishable units. The correct field is
`payload.inventory[].inventoryData.availableUnits` (note `onhandUnits` has a
lowercase h). Walmart's docs do not name these fields; they were found by
dumping a live response with `--probe`.

**Amazon: 403 root-caused, and the app carries the wrong role.** Token
exchange succeeds; every call to `/fba/inventory/v1/summaries` returns
`{"code":"Unauthorized","message":"Access to requested resource is denied."}`.
App ID `amzn1.sp.solution.6e40c16d-09eb-4881-993e-2a7e6b319e38`, self-authorized
twice including a fresh refresh token, still 403 after 24 hours - so it was
never propagation delay.

The cause: **the FBA Inventory API is guarded by the "Amazon Fulfillment"
role.** The approval obtained on 2026-08-19 (case 21676039541) was for
*Inventory and Order Tracking*, which covers the Orders API and order tracking
reports, not FBA inventory. Approving the wrong role produces exactly this
signature - LWA fine, endpoint denied. Several sellers hitting the same 403
also needed *Product Listing* before it cleared.

The fix is not code, and it is not a checkbox on the app either. **The role
list on the App registration page only offers roles the developer profile was
approved for.** Silverpot's profile carries Inventory and Order Tracking alone,
so Amazon Fulfillment is not rendered as an option at all. Confirmed on
2026-08-26 against the live Develop Apps page.

So it goes profile first, app second:

1. Solution Provider Portal > Developer Central > **developer profile** > Edit.
   Add **Amazon Fulfillment** (and **Product Listing**), re-answer the use-case
   and data-security questions, resubmit for evaluation. Manual review, days.
2. Once approved, the checkbox appears on App registration. Tick it, save.
3. Re-authorize the app and mint a **fresh refresh token** - an existing token
   keeps the roles it was minted with, which is why re-authorizing twice
   without changing the roles changed nothing.

Two things in the code make that survivable:

- `--diagnose` probes one harmless endpoint per role and prints which roles the
  token actually carries, so the missing checkbox is named rather than guessed.
- `amazon.fetch_fba_inventory()` falls back to the Reports API
  (`GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA`, then `GET_AFN_INVENTORY_DATA`)
  when the direct endpoint 403s. It is slower and has no reserved breakdown.
  The route used is recorded in `public/inventory.json` as `fba_source`, so
  every published number is traceable to how it was obtained.

**Do not oversell the fallback.** It buys a different *role set*, not a way
around the profile: `GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA` wants Pricing +
Amazon Fulfillment, and `GET_AFN_INVENTORY_DATA` is an FBA report too. If the
profile carries Inventory and Order Tracking alone, expect both to 403 as well
- that role reaches orders and MFN quantities, not fulfillment-center stock.
There is no known route to FBA fulfillable quantity without Amazon Fulfillment.
`--diagnose` answers this definitively rather than by inference; run it before
telling anyone the sync is unblocked.

Once **Amazon Fulfillment** is granted, the direct API is used again
automatically and `fba_source` goes back to `fba-inventory-api`.

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
