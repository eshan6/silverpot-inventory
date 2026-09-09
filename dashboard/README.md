# Silverpot marketplace dashboard

Sales, Ads and Spend for Silverpot Tea, behind a login, with a Super Admin /
Admin / Viewer hierarchy and per-user saved views.

Amazon and Walmart sales are both live. Walmart advertising is not: Walmart
Connect is a separate approval, and is postponed rather than promised.

## Status

| Piece | State |
|---|---|
| Schema, roles, RLS, audit log | **built and tested** (against real Postgres) |
| Ad programs (SP + SB + SD) and the Spend view | **built** |
| Sales: fetch, Eastern-day bucketing, aggregation | **built and tested** |
| Sales: writing into Supabase + scheduled workflow | **built** |
| Invites + first super admin, without a server | **built and tested** |
| Ads ingestion (Advertising API, all three programs) | **built**, waiting on the API approval |
| Frontend: Sales, Ads, Spend, saved views | **built** |
| Admin UI (people, invites, audit log) | **built** |
| One-paste Supabase setup | **built and tested** |
| Walmart sales ingestion | **built**, and backfilled to the first order |
| Walmart advertising | postponed on purpose (see the end) |

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

## Setting it up

Five steps, none of them repeated. Everything after this runs on a schedule.

**1. Create a Supabase project.** Free tier. Note the project URL, the `anon`
key and the `service_role` key from Settings → API. The anon key is meant to be
public; the service key is not and must never leave GitHub Actions secrets.

**2. Paste the schema.** Open `dashboard/supabase/setup.sql`, put your own
email on the one marked line near the end, and run the whole file in the
Supabase SQL editor. That is every migration in order plus the bootstrap
address, and it refuses to run while the example address is still in it.

**3. Sign up.** Deploy the app (step 4) or run it locally, click *Create your
account*, and use the address from step 2. You are provisioned as super admin
automatically — there is no follow-up SQL. That claim works only once: after an
active super admin exists the bootstrap does nothing.

If Supabase's email confirmation is on (Authentication → Providers → Email,
"Confirm email"), you get a confirmation link first. Either setting works; the
profile is created at signup either way.

**4. Deploy the frontend.** Vercel: import the repository, set the root
directory to `dashboard/web`, and add `VITE_SUPABASE_URL` and
`VITE_SUPABASE_ANON_KEY`. `vercel.json` already carries the build command, the
SPA rewrite and the security headers. Netlify and Cloudflare Pages read
`public/_redirects` and `public/_headers` and need the same two variables — the
app has no server of its own, so moving hosts is a ten-minute decision, not an
architectural one.

**5. Add the GitHub Actions secrets** so ingestion can write:

| Secret | Used by |
|---|---|
| `DASHBOARD_SUPABASE_URL` | both sync workflows |
| `DASHBOARD_SUPABASE_SERVICE_KEY` | both sync workflows |
| `ADS_CLIENT_ID` `ADS_CLIENT_SECRET` `ADS_REFRESH_TOKEN` `ADS_PROFILE_ID` | Ads sync |

Amazon's SP-API credentials are already secrets in this repository and are
reused as they are.

The four `ADS_*` values come from `python -m collector.ads_auth`, once the
Advertising API application is approved:

```bash
python -m collector.ads_auth url                 # open it, approve, copy the code
python -m collector.ads_auth token --code THE_CODE
python -m collector.ads_auth profiles            # the profile id for the US seller account
```

That is the one command in this repository that prints a secret on purpose. It
says so when it runs. Paste the refresh token into GitHub Secrets and nowhere
else.

**Then check it works** without waiting for a schedule: run the *Dashboard
sync* and *Ads sync* workflows by hand from the Actions tab. Ads accepts a
`probe` tick that prints Amazon's raw rows and writes nothing, which is the
right first run — it confirms the column names against reality rather than
assuming them.

## How far back the numbers go

Both sync jobs do two things on every run. They re-read a trailing window,
because orders cancel and advertising attribution keeps restating recent days.
Then they reach one chunk further into the past, so the archive grows on its
own until it holds everything Amazon still has.

That is a walk rather than one big fetch because `getOrders` allows a burst of
about twenty calls and then roughly one a minute, and this pipeline spends one
call per day of history. "Fetch two years every morning" is a twelve-hour run
that fails; it is also pointless, since a settled day never changes. History
gets collected once and only the recent days get re-read.

Sales walk back 15 days per run, twice a day, to a ceiling of two years.
Advertising walks 30 days per run to a much shorter ceiling, because Amazon
retains far less advertising history than order history. Both stop early — and
permanently — after about three months of consecutive days with nothing in
them, which means the walk has gone back past the first sale.

Three properties worth knowing, because each is a way this could have gone
quietly wrong:

- **Progress is a cursor over days attempted, not a query for the oldest row.**
  Keyed off the oldest row present, one month with no sales would write no
  rows, leave the oldest row where it was, and refetch that same month every
  twelve hours forever, with no error and no growth.
- **A throttled chunk still counts.** Days are taken newest-first within a
  chunk, so a chunk cut short by rate limiting is contiguous with the history
  above it and the cursor moves to exactly what was collected. The days it
  missed are the next ones fetched.
- **The backfill can never break the daily numbers.** It runs after the day's
  figures are written and the run is recorded, and every failure in it is
  caught and reported rather than failing the job.

`--no-backfill` on either command re-reads the trailing window only.

## Adding people

An admin records an invite (Admin → Invite someone); the invited person signs
up with that address and is provisioned with the role recorded for them. Send
them the link yourself — recording an invite does not send an email.

There is no server in this design and the service key must never reach a
browser, so an admin cannot mint an account directly. This is the way round
that, and it keeps invite-only true: someone who signs up without an invite
gets an account with no profile, which every policy treats as no access.

## Running the tests

Needs a Postgres 16 and nothing else. No Supabase account, no network, no
credentials.

```bash
dashboard/supabase/tests/run.sh
```

Each test file gets its own throwaway database — they used to share one, which
made them order-dependent and made the "no super admin exists yet" bootstrap
check meaningless once another file had created one. The harness stands up a
local stand-in for Supabase's `auth` schema and roles, applies the migrations,
and then tries every escalation a real session could attempt: a viewer
promoting themselves, an admin minting an admin, an admin inviting an admin, an
admin deactivating a super admin, a deactivated user reading facts, a
stranger's token reading facts.

It finishes by running `setup.sql` itself, twice: once unedited, to prove it
refuses while the example address is still in it, and once with an address
filled in, to prove that signing up as that address really does produce a super
admin. A generated file nobody executes before the one time it matters is a
file nobody has tested.

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

**Walmart advertising.** Walmart *sales* are live: orders come from the
Marketplace API on the credentials the WFS inventory pull already uses, so
they needed no new approval and no new secret. Walmart Connect, their
advertising API, is a separate and harder approval, so it is postponed rather
than promised.

Harder in a specific way, and it is worth being precise about it because it is
not the Amazon situation. Amazon's Advertising API has a self-serve
application: you fill in a form and wait. Walmart Connect's Ads APIs are
documented as available to *Walmart Connect Partner Network* partners -
agencies and tech platforms - and an advertiser's own route is to authorise
one of those partners from the Ad Center admin page. There is no button that
mints advertising credentials for a seller.

Since this repository has been wrong from Walmart's documentation twice
already, that is checked rather than assumed. Run the *Ads sync* workflow with
`walmart_diagnose` ticked: `collector/walmart_ads.py` asks each candidate
Walmart Connect endpoint with the Marketplace token we already hold and prints
the status codes. It reads only - it cannot create a campaign or spend a
dollar - and it judges a known-good endpoint first, so a broken token reports
as *inconclusive* rather than as a denial.

Four outcomes and what each means:

| What it prints | What it means |
|---|---|
| Any advertising endpoint answers 200 | There is a route. Probe it for its response shape before any figure is written. |
| 403 *missing required security headers* | Rejected at the gateway, before access was ever considered. The wrong kind of credential, not a denial. |
| A plain 401 / 403, control at 200 | A real refusal: this needs an approval or a partner authorisation. |
| Everything 404, control at 200 | Wrong paths, not denied access. Says nothing either way. |

**What it actually said, run on 2026-09-09.** The control answered 200 and
every Sponsored Search endpoint answered `403 Request is missing required
security headers` — the second row, not the third. Walmart Connect does not
accept the Marketplace OAuth token at all: it wants the older signed scheme, a
consumer id and an RSA-SHA256 signature, and those are issued to a partner
rather than generated in Seller Center. The two `marketplace.walmartapis.com`
candidates returned 404, so advertising is not hiding on the Marketplace host
either.

That is consistent with the partner-network reading and is not proof of it.
What is proved is narrower and still decisive: there is no route to Walmart
advertising from anything this repository already holds, so nothing can be
built here until a credential of a different kind exists.

Whichever way it lands, the database is already shaped for the answer:
`ads_daily` and `ads_search_terms` both carry `marketplace` and `ad_program`,
and `spend_daily` joins per marketplace, so Walmart rows need an ingestion
module and no migration.

Combined Amazon+Walmart views are meant to be impossible, and keeping
`marketplace` on every row and every query is how that stays true.

One thing about Walmart orders is worth knowing before touching that code.
`/v3/orders` takes a `shipNodeType`, and its **default view does not include
WFS-fulfilled orders** - which is all of Silverpot's. Confirmed on 2026-09-09
over a three-month window: the default view returned 0 orders and
`WFSFulfilled` returned 105. Two probes before that had come back empty and
looked exactly like a channel with no sales. A run therefore asks for every
explicit view and merges them; they are disjoint, since an order is fulfilled
one way.

**Nothing else.** Advertising is now read straight from the Amazon Ads API
rather than from an exported Google Sheet, which was the earlier plan and the
one thing in this design that would have meant uploading a file every week.
`collector/ads.py` covers Sponsored Products, Brands and Display; only
Sponsored Products runs today, and the other two cost nothing to carry.
