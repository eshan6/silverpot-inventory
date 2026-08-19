# Silverpot Unified Inventory

Pulls FBA and WFS stock daily, stores an append-only history in Google Sheets, and
publishes one JSON feed that both the ops dashboard and silverpottea.com read from.

```
Amazon SP-API ─┐
               ├─→ GitHub Actions (daily) ─→ Google Sheets (history)
Walmart API ───┘                          └─→ public/inventory.json ─→ dashboard + Lovable
```

## The availability contract

```
published_available = FBA.fulfillableQuantity + WFS.availableToSell − safety_buffer
```

Those are the only two pools that multi-channel fulfillment can ship to a
silverpottea.com customer. Everything else Amazon reports is stored in the
snapshot history for forecasting and **never** published:

| Excluded | Why |
|---|---|
| `inboundWorking / Shipped / Receiving` | In transit or being received. Not sellable today. |
| `reserved.pendingCustomerOrder` | Already sold, awaiting pick. |
| `reserved.pendingTransshipment` | Moving between FCs. |
| `reserved.fcProcessing` | Amazon has it sidelined. |
| `unfulfillable` | Damaged or otherwise dead. |
| `researching` | Amazon has lost it and is looking. |

### Why the safety buffer exists

The feed is up to 24 hours stale, and both pools drain from marketplace orders
you don't control in the interim. Without a buffer the site can sell the last
unit that Amazon already sold this morning. Default is `max(2, 5% of raw)`.
Override per SKU in the `safety_buffer` column once you can see real daily
depletion rates in the `snapshots` tab. Fast movers like Original Darjeeling
warrant more; a SKU turning two units a week warrants the floor of 2.

---

## Setup

### Step 1 — SKU map

Amazon and Walmart use the same SKU strings, so `sku` serves both. Two columns
need your attention before the first run:

**Nothing needs editing.** Readable names plus real SKU codes, as decided.
`internal_code` (`DARJ_ORIG`) is the internal join key; `sku` is the real
marketplace code, the same string on both Amazon and Walmart.

Two columns get filled in later:

- **`website_product_id`** — leave blank for now, filled at the Lovable step.
- **`walmart_sku_override`** — leave blank. Fill it only when the
  FNSKU-to-manufacturer-barcode conversion issues a new Amazon SKU for a product
  whose Walmart listing keeps the old one. That is the one foreseeable event
  that breaks the same-SKU assumption, and it breaks it on the Amazon side only.



### Step 2 — Amazon SP-API credentials

There is no AWS account, no IAM user, and no request signing. Amazon dropped
SigV4 in October 2023; it is now plain OAuth.

1. Seller Central → **Settings → User Permissions → Third-party developer and apps**.
2. Under *Developer Central*, click **Register as a developer**. Choose **Private
   seller** (an app only your own account uses). Approval is usually same-day to
   a few days.
3. Once approved: **Add new app client**. Name it `silverpot-inventory`. Under
   roles, tick **Inventory and Order Tracking** — that is what the FBA Inventory
   API needs. Do not request roles you don't need; extra roles slow approval.
4. Amazon shows you an **LWA client ID** (`amzn1.application-oa2-client...`) and
   an **LWA client secret**. Copy both.
5. On the app row, open the dropdown → **Authorize**. This is self-authorization,
   since it's your own seller account. Amazon returns a **refresh token**
   (`Atzr|...`). Copy it. It does not expire unless you revoke it.

You now have `LWA_CLIENT_ID`, `LWA_CLIENT_SECRET`, `LWA_REFRESH_TOKEN`.

### Step 3 — Walmart API credentials

1. Go to `developer.walmart.com`, sign in with your Seller Center account.
2. **My Account → API Key Management → Add New Key**. Pick **Production**.
3. Choose delegated access for **Inventory** (and **Fulfillment** if you plan to
   push MCS orders through the API later).
4. Copy the **Client ID** and **Client Secret**. The secret is shown once.

You now have `WALMART_CLIENT_ID`, `WALMART_CLIENT_SECRET`.

### Step 4 — Google Sheets service account

1. `console.cloud.google.com` → new project `silverpot-inventory`.
2. **APIs & Services → Library** → enable **Google Sheets API**.
3. **Credentials → Create credentials → Service account**. No roles needed.
4. Open the service account → **Keys → Add key → JSON**. Downloads a key file.
5. Create a Google Sheet named `Silverpot Inventory`. **Share it with the
   service account's email** (`...@....iam.gserviceaccount.com`) as **Editor**.
   This is the step people miss — without it every write returns 403.
6. The sheet ID is the long string in the URL between `/d/` and `/edit`.

You now have `GOOGLE_SERVICE_ACCOUNT_JSON` (the whole file contents, pasted) and
`GOOGLE_SHEET_ID`.

The script creates the `snapshots` and `current` tabs itself on first run.

### Step 5 — Repo and schedule

Push this folder to a **private** GitHub repo. Then
**Settings → Secrets and variables → Actions → New repository secret** for each:

```
LWA_CLIENT_ID
LWA_CLIENT_SECRET
LWA_REFRESH_TOKEN
WALMART_CLIENT_ID
WALMART_CLIENT_SECRET
GOOGLE_SERVICE_ACCOUNT_JSON
GOOGLE_SHEET_ID
```

Then, in this order:

```bash
# 1. Confirm the real API response shapes before trusting anything
python -m collector.main --probe

# 2. Compute without writing
python -m collector.main --dry-run

# 3. Real run
python -m collector.main
```

**Do not skip `--probe`.** Walmart's docs for the replacement WFS endpoint are
internally inconsistent — the page for the new `/v3/wfs/inventory` path still
shows the retired `/v3/fulfillment/inventory` URL in its own curl sample, and
doesn't pin down the available-to-sell field name. `collector/walmart.py`
therefore searches a list of candidate field names rather than hardcoding one.
`--probe` prints the raw payload so you can confirm which name your account
actually returns. If it isn't in `ATS_FIELDS`, add it at the top of the list.

Then verify against Seller Central and Seller Center by hand for about a week
before pointing the website at it.

The workflow runs at 05:30 UTC (01:30 ET). GitHub disables scheduled workflows
after 60 days of repo inactivity, and the daily commit of `inventory.json`
counts as activity, so this self-sustains as long as stock is moving.

### Step 6 — Dashboard hosting

`public/` is a pure static folder. No functions, no build step, no compute
meter, therefore no surprise bill.

**Cloudflare Pages** (recommended): Pages → Create → Connect to Git → pick the
repo → build command empty, output directory `public`. Free tier permits
commercial use and does not meter static asset bandwidth.

Vercel works identically (`vercel.json` is included), but its Hobby plan is not
licensed for commercial use — worth knowing before you put a company dashboard
on it.

Either way the same deployment serves both the dashboard at `/` and the feed at
`/inventory.json`, with CORS already open via `_headers` / `vercel.json`.

### Step 7 — Website

See `LOVABLE_PROMPT.md`. The daily job writes into the same database your admin
panel writes to, so no website code changes. Requires two more GitHub secrets
(`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`) and three repository variables
(`SUPABASE_TABLE`, `SUPABASE_SKU_COLUMN`, `SUPABASE_QTY_COLUMN`).

Until those are set the collector prints "Website push skipped" and does
everything else normally, so it is safe to run from day one.

---

## Data model

`snapshots` is append-only, long format, one row per SKU per node per state per day:

```
snapshot_date | internal_code | product_name | node | state | qty
2026-08-19    | DARJ_ORIG     | Original Darjeeling | FBA | fulfillable | 84
2026-08-19    | DARJ_ORIG     | Original Darjeeling | FBA | inbound     | 400
2026-08-19    | DARJ_ORIG     | Original Darjeeling | WFS | available_to_sell | 22
2026-08-19    | DARJ_ORIG     | Original Darjeeling | PUBLISHED | available | 100
```

Never overwrite it. The time series is the whole point — a single current-value
row tells you what you have, while the series tells you when you run out. Joined
against the sales tracker on `internal_code`, it gives days-of-cover per SKU,
which is the reorder trigger you need once FCL ocean puts 60–90 days between the
decision and the stock landing in Fords.

`current` is a derived one-row-per-SKU view, safe to overwrite, convenient for
eyeballing and for VLOOKUPs from other sheets.

## Warnings the collector emits

| Message | Meaning |
|---|---|
| `WARN no FBA match` | SKU in the map has no Amazon inventory record. Usually a listing that went inactive, or a SKU renamed by the barcode conversion. |
| `WARN no WFS match` | Same on the Walmart side. |
| `WARN present on one marketplace only` | The SKU resolved on one marketplace but not the other. Almost always a listing problem rather than an inventory problem — worth checking the same day. |
| `NOTE Walmart SKU overridden` | The same-SKU assumption has been deliberately broken for that row. |
