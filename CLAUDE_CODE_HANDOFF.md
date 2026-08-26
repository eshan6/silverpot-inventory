# Moving to Claude Code

## Before you start

**Keep the repo private.** Claude Code works on the local clone on your machine.
It does not need GitHub access to read or edit files, so there is no reason to
expose your SKU map, stock levels, or inventory feed publicly.

**Amazon's 403 will not be fixed here.** It is a permission grant on Amazon's
servers. Open the support case in parallel; that is the only path.

## Setup, once

```bash
# 1. Clone the repo locally
git clone https://github.com/eshan6/silverpot-inventory.git
cd silverpot-inventory

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create .env with your credentials (already gitignored)
```

`.env` contents — same values as your GitHub secrets:

```
LWA_CLIENT_ID=amzn1.application-oa2-client...
LWA_CLIENT_SECRET=amzn1.oa2-cs.v1...
LWA_REFRESH_TOKEN=Atzr|...
WALMART_CLIENT_ID=9ceec6de-...
WALMART_CLIENT_SECRET=...
GOOGLE_SHEET_ID=1uS5iHJrPVjZyzHPVgWwHuEZBz7zC8oSiRuHjeRH-hKY
FORCE_IPV4=true
```

For the Google service account, keep the JSON file outside the repo and point at
it, or paste the whole thing as one line:

```
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account","project_id":"..."}
```

Then:

```bash
set -a && source .env && set +a
python -m collector.main --dry-run
```

If that prints Walmart numbers, you are ready.

**Optional but useful:** install the GitHub CLI (`gh`) and run `gh auth login`.
That lets Claude Code read your Actions logs and commit directly, on a private
repo, authenticated as you.

## The kickoff prompt

Paste this as your first message to Claude Code.

---

I'm handing you an existing project. Read `CLAUDE.md` in the repo root first —
it has the full architecture, the current status, and the rules for changes.
Then read `collector/main.py` to see how the pieces fit together.

Short version: this is a daily inventory pipeline that pulls stock from Amazon
FBA and Walmart WFS, stores an append-only history in Google Sheets, and
publishes a JSON feed that will eventually drive stock levels on
silverpottea.com. Walmart works. Amazon returns 403 on every inventory call
despite a valid token and an approved developer profile.

I have credentials in `.env`. Load them with
`set -a && source .env && set +a` before running anything.

Four things I want, in this order:

**1. Run the Amazon diagnostic and tell me what it means.**

```bash
python -m collector.main --diagnose
```

It calls a role-free SP-API endpoint alongside the inventory endpoint. If the
role-free one succeeds and inventory 403s, the app is live but the role is not
attached to the token. If both 403, the app is not authorised for API calls at
all. Tell me which, and draft the support case text for Amazon based on the
result. Do not try to fix this in code — it is an Amazon-side grant.

**2. Clean up the snapshots tab.**

The Google Sheet has orphan rows from early broken runs, using old-style
internal codes (`ASSAM_ORIG`, `DARJ_ORIG` etc.) that no longer exist in
`sku_map.csv`. They are all dated 2026-08-25 and all zero. Write a small
one-off script that deletes any snapshot row whose `internal_code` is not in the
current SKU map. Show me what it will delete before it deletes anything.

**3. Add a local test suite.**

Two bugs got to production that tests would have caught: a stale dict key
(`amazon_seller_sku`) that crashed the sheet write, and nine wrong guesses at
Walmart's available-to-sell field name. Add pytest tests that exercise
`parse_summary`, `parse_record`, `build`, `snapshot_rows` and `depletion`
against realistic fixture payloads, including the real Walmart response shape
documented in `collector/walmart.py`. Assert the availability contract and the
degraded-run guard specifically. Then wire the tests into the GitHub Actions
workflow so they run before the sync step.

**4. Tell me what else you would fix.**

Read the code with fresh eyes and give me a short prioritised list of real
problems, ordered by what would actually cost me money or time. Skip style
opinions. I care about correctness of the published number above everything —
wrong stock on the storefront means overselling or suppressed sales.

Rules: never commit `.env` or any credential, never print a secret value, and
never weaken the guard that blocks the website push on a degraded run.

---

## After that

Once Amazon is unblocked and a week of numbers has been verified by hand against
Seller Central and Seller Center, the remaining work is:

- Deploy `public/` to Cloudflare Pages for the dashboard
- Get the Supabase table and column names from Lovable (see `LOVABLE_PROMPT.md`)
- Configure the website push and watch the first few runs closely
