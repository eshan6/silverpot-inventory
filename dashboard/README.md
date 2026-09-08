# Silverpot marketplace dashboard

Sales, Ads and Spend for Silverpot Tea, behind a login, with a Super Admin /
Admin / Viewer hierarchy and per-user saved views.

Amazon only for now. Walmart is plumbed for in the schema but not ingested.

## Status

| Piece | State |
|---|---|
| Schema, roles, RLS, audit log | **built and tested** (34 checks against real Postgres) |
| Ad programs (SP + SB + SD) and the Spend view | **built** |
| Sales: fetch, Eastern-day bucketing, aggregation | **built and tested** (26 checks) |
| Sales: writing into Supabase + scheduled workflow | not started |
| Ads ingestion (Advertising API) | blocked on Amazon approval |
| Frontend (auth, three sections, saved views) | not started |
| Admin UI (users, settings) | not started |

## The shape of it

```
Amazon SP-API Orders ─┐
                      ├─→ GitHub Actions (daily) ─→ Supabase Postgres ─→ static SPA
Google Sheet (ads)   ─┘         service role          RLS enforced         any host
```

Four decisions worth knowing about, because they are why this is free and why
the host barely matters.

**The frontend is a static SPA with no server of its own.** Auth, data and
authorisation all come from Supabase; ingestion runs in GitHub Actions. That
means no serverless functions on the hosting side, so the same build deploys
unchanged to Cloudflare Pages, Netlify or Vercel. Hosting becomes a ten-minute
decision you can reverse, not an architectural commitment.

**Ingestion reuses the inventory collector's credentials.** `LWA_CLIENT_ID`,
`LWA_CLIENT_SECRET` and `LWA_REFRESH_TOKEN` are already GitHub Actions secrets
in this repository, and `collector/amazon.py` already handles LWA token
exchange, retries and IPv4 forcing. Putting the dashboard here rather than in a
new repository avoids copying marketplace credentials to a second place, which
is a security win as well as less work.

**No credential ever reaches the browser.** The client holds a Supabase anon
key and a user session, nothing else. Marketplace tokens live in Actions
secrets and are read only by the ingestion job.

**Authorisation lives in Postgres, not React.** See below.

## Roles

| | Viewer | Admin | Super Admin |
|---|---|---|---|
| Read Sales / Ads / Spend | yes | yes | yes |
| Own saved views | yes | yes | yes |
| See the user roster | no | yes | yes |
| Read the audit log | no | yes | yes |
| Invite viewers | no | yes | yes |
| Invite admins or super admins | no | **no** | yes |
| Change anyone's role | no | **no** | yes |
| Modify an admin or super admin | no | **no** | yes |
| Read or change app settings | no | **no** | yes |

Three rules that hold for everyone, super admins included:

- **Nobody changes their own role.** Your level is always someone else's
  decision.
- **The last active super admin cannot be removed.** Keep a second one as
  break-glass; right now that is a bus factor of one.
- **Users are deactivated, never deleted**, so the audit trail keeps pointing
  at a real person.

## Why the rules live in the database

A viewer who opens devtools has a valid session token and can call the Supabase
REST API directly. Hiding a button in React does nothing about that. So every
rule above is a row-level security policy or a trigger, and the UI is only a
convenience on top.

Two rules need triggers rather than policies: an `UPDATE` policy's `WITH CHECK`
sees only the resulting row, so it cannot tell "an admin edited a viewer's
name" from "an admin promoted themselves". Comparing `OLD` with `NEW` needs a
trigger.

The audit log is written by triggers too, so a change made through any route
gets recorded, not just changes the app remembered to log.

## Running the tests

Needs a Postgres 16 and nothing else. No Supabase account, no network, no
credentials.

```bash
dashboard/supabase/tests/run.sh
```

It creates a throwaway database, stands up a local stand-in for Supabase's
`auth` schema and roles, applies the migration, and then tries every escalation
a real session could attempt: a viewer promoting themselves, an admin minting
an admin, an admin deactivating a super admin, a deactivated user reading
facts, a stranger's token reading facts.

The suite is mutation-tested. Deleting the "admins may only invite viewers"
guard or the last-super-admin guard each make it fail.

That mutation testing already earned its keep: the "admin cannot invite an
admin" check originally used a random UUID, so the insert died on the foreign
key to `auth.users` before the role guard was consulted. It passed for the
wrong reason, and kept passing with the guard deleted. It now uses a real auth
user.

## Freshness

`ingest_runs` records every ingestion attempt with what date range it covered
and whether it succeeded. Every page stamps "as of" from it, and a range with
no successful run must say *no data* rather than render zeros.

This is not hypothetical caution. The inventory pipeline in this same
repository published a stale storefront for 31 hours on 2026-09-03 after a
Google Sheets outage, and nothing on the page said so.

## Not built yet, and deliberately

**Walmart.** Every fact table carries a `marketplace` column and the UI will
have a switcher, but only Amazon is ingested. Walmart orders are reachable with
credentials this repository already holds. Walmart Connect, their advertising
API, is a separate and harder approval, so it is postponed rather than
promised. Combined Amazon+Walmart views are meant to be impossible, and keeping
`marketplace` on every row and every query is how that stays true.

**Amazon Advertising API.** Impressions, clicks, CPC, spend and search terms
come from the Advertising API, which is *not* SP-API: separate registration,
separate approval, separate OAuth client, and async report polling. v1 reads a
Google Sheet you export into instead, which needs no approval. Swapping the
source later touches the ingestion job only, because `ads_daily` and
`ads_search_terms` are shaped around the data rather than around where it came
from.
