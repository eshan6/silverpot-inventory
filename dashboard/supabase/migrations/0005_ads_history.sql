-- Change history, and advertising metrics per advertised product.
--
-- Two tables, and the reason there are two rather than one is the whole point
-- of this migration.
--
-- `ads_daily` holds one row per campaign per day. `ads_summary` and every
-- other reader sums it grouped only by (marketplace, ad_program) - it does not
-- filter by grain, because until now there was only one grain. Putting
-- advertised-product rows in there as well would count the same spend twice,
-- once under the campaign and once under the ASIN. Nothing would error: the
-- primary key carries `sku`, so the product rows would insert cleanly beside
-- the campaign rows and every figure the dashboard has ever shown would
-- quietly double. Wrong numbers that look right are this project's worst
-- failure mode, so the finer grain gets its own table.

-- ------------------------------------------------------- advertised product
--
-- Sponsored Products reported per advertised ASIN. Same window and same
-- attribution columns as ads_daily, so the two are comparable; a different
-- table, so they are never accidentally added together.

create table public.ads_advertised_product (
    marketplace      marketplace not null,
    ad_program       ad_program  not null default 'sponsored_products',
    ad_date          date        not null,
    campaign_id      text        not null,
    campaign_name    text,
    ad_group         text        not null default '',
    asin             text        not null default '',
    sku              text        not null default '',
    impressions      bigint      not null default 0,
    clicks           bigint      not null default 0,
    spend            numeric(12, 2) not null default 0,
    attributed_sales numeric(12, 2) not null default 0,
    attributed_units integer     not null default 0,
    extra            jsonb       not null default '{}'::jsonb,
    updated_at       timestamptz not null default now(),
    -- ASIN *and* SKU: one ASIN can be advertised under more than one seller
    -- SKU, which is already true here - the stickerless conversion issued a
    -- second SKU for the same product. Keying on ASIN alone would make those
    -- two rows collide and silently drop one pool's spend.
    primary key (marketplace, ad_program, ad_date, campaign_id, ad_group, asin, sku)
);

create index ads_advertised_product_date_idx
    on public.ads_advertised_product (marketplace, ad_date);

create index ads_advertised_product_asin_idx
    on public.ads_advertised_product (marketplace, asin, ad_date);

comment on table public.ads_advertised_product is
    'Sponsored Products metrics per advertised ASIN per day. Deliberately not '
    'in ads_daily: ads_summary sums that table without filtering by grain, so '
    'a second grain there would double every spend figure.';

-- ------------------------------------------------------------ change history
--
-- Who changed what, and when. Amazon keeps roughly ninety days and deletes
-- the rest, so unlike the reporting tables this one cannot be rebuilt from
-- the API after the fact. That shapes two decisions below.
--
-- First, `raw`. Every record is kept whole as jsonb alongside the columns we
-- read out of it. A field we did not think to model is not recoverable later
-- - the window will have closed - so nothing Amazon sends is discarded.
--
-- Second, `change_key`. The request is for no duplicates, and the backfill
-- and the daily pull overlap by design, so the same change arrives more than
-- once. The key is Amazon's own change id where the response carries one, and
-- otherwise a digest the collector computes from the identifying fields. It
-- is text rather than a generated column because which fields identify a
-- change is a fact about Amazon's payload, and this migration ships before
-- the probe that settles it; computing it in Python keeps that decision in
-- one place, next to the parsing.

create table public.ads_change_history (
    marketplace   marketplace not null default 'amazon',
    change_key    text        not null,
    changed_at    timestamptz not null,
    entity_type   text        not null,
    entity_id     text        not null default '',
    entity_name   text,
    campaign_id   text        not null default '',
    campaign_name text,
    change_type   text        not null default '',
    field_name    text        not null default '',
    old_value     text,
    new_value     text,
    changed_by    text,
    raw           jsonb       not null default '{}'::jsonb,
    ingested_at   timestamptz not null default now(),
    primary key (marketplace, change_key)
);

-- The three ways this gets read: a date range, one campaign over a range, and
-- one ASIN over a range. The ASIN case has no column of its own because an
-- ASIN appears inside a product-target or ad record rather than on every
-- change, so ad_change_history(asin=...) filters on entity_id and the raw
-- payload; that is why entity_id is indexed too.
create index ads_change_history_when_idx
    on public.ads_change_history (marketplace, changed_at desc);

create index ads_change_history_campaign_idx
    on public.ads_change_history (marketplace, campaign_id, changed_at desc);

create index ads_change_history_entity_idx
    on public.ads_change_history (marketplace, entity_id, changed_at desc);

comment on table public.ads_change_history is
    'Amazon Ads change history. Amazon keeps about ninety days and deletes '
    'the rest, so this table is the only copy once the window passes - which '
    'is why the whole record is kept in raw.';

-- ------------------------------------------------------------------- access
--
-- Same posture as every other table here: RLS on, select for an active user,
-- no insert/update/delete policy at all. Writes come from the collector,
-- which holds the service key and bypasses RLS. A viewer with devtools has a
-- real session token and can call PostgREST directly, so this is the control
-- and the UI is only a courtesy.

alter table public.ads_advertised_product enable row level security;
alter table public.ads_change_history     enable row level security;

create policy advertised_product_read on public.ads_advertised_product
    for select using (public.is_active_user());

create policy change_history_read on public.ads_change_history
    for select using (public.is_active_user());
