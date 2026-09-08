-- Carry all three Amazon ad programs from the start.
--
-- Silverpot runs only Sponsored Products today. Sponsored Brands and Sponsored
-- Display are modelled anyway so that switching one on later is a settings
-- change and a new report request, never a migration and a rewrite of every
-- query. An empty program costs one enum value and nothing else.

create type ad_program as enum (
    'sponsored_products',
    'sponsored_brands',
    'sponsored_display'
);

-- Program-specific metrics live in `extra` rather than in columns, because the
-- three programs do not report the same things: Sponsored Display has viewable
-- impressions and Sponsored Brands has video quartiles, neither of which means
-- anything for Sponsored Products. Widening the table for every metric one
-- program has would leave most columns null most of the time, and would need a
-- migration the first time Amazon adds a metric. The five columns below are
-- the ones all three genuinely share, so they stay first-class and queryable.

alter table public.ads_daily
    add column ad_program ad_program not null default 'sponsored_products',
    add column extra jsonb not null default '{}'::jsonb;

alter table public.ads_search_terms
    add column ad_program ad_program not null default 'sponsored_products',
    add column extra jsonb not null default '{}'::jsonb;

-- The program is part of a row's identity: the same campaign id could appear
-- under two programs, and summing them without distinction would double-count
-- spend, which is the numerator of the one metric this dashboard exists for.
alter table public.ads_daily drop constraint ads_daily_pkey;
alter table public.ads_daily add primary key
    (marketplace, ad_program, ad_date, campaign_id, ad_group, sku);

alter table public.ads_search_terms drop constraint ads_search_terms_pkey;
alter table public.ads_search_terms add primary key
    (marketplace, ad_program, ad_date, campaign_id, search_term, match_type);

-- Sponsored Display targets audiences and products rather than search queries,
-- so it will not populate ads_search_terms in practice. That is left as a fact
-- about the data rather than a check constraint: Amazon has changed what each
-- program reports before, and a constraint encoding today's behaviour is a
-- migration waiting to happen.

create index ads_daily_program_idx
    on public.ads_daily (marketplace, ad_program, ad_date);

-- ---------------------------------------------------------------- spend
--
-- The number this dashboard exists to answer: what did we pay, in advertising,
-- for each unit we actually sold - not each unit advertising takes credit for.
--
-- Two things make that easy to get wrong, so the view does them once here
-- rather than in whichever page happens to ask.
--
-- Spend is summed across every ad program. If Sponsored Brands is running and
-- only Sponsored Products is counted, the numerator is short while the
-- denominator is complete, and cost per unit reads better than reality - the
-- exact direction that would let overspending look fine.
--
-- Units come from sales_daily, which is every unit sold from the Orders API,
-- not attributed_units. Ad-attributed units answer a different question.

create or replace view public.spend_daily as
select
    coalesce(s.marketplace, a.marketplace)   as marketplace,
    coalesce(s.sale_date, a.ad_date)         as day,
    coalesce(a.spend, 0)                     as ad_spend,
    coalesce(s.units, 0)                     as units_sold,
    coalesce(s.revenue, 0)                   as revenue,
    coalesce(a.attributed_sales, 0)          as attributed_sales,
    coalesce(a.attributed_units, 0)          as attributed_units,
    -- Null rather than zero on a day with no sales: dividing by nothing has no
    -- answer, and 0 would plot as a perfect day.
    case when coalesce(s.units, 0) > 0
         then round(coalesce(a.spend, 0) / s.units, 2) end as cost_per_unit,
    -- TACoS. $4 on a $13.99 tin is 29% of revenue; $4 on a $21.99 pouch is
    -- 18%. The ratio and the percentage say different things and the Spend
    -- page shows both.
    case when coalesce(s.revenue, 0) > 0
         then round(100 * coalesce(a.spend, 0) / s.revenue, 2) end as ad_pct_of_revenue
from (
    select marketplace, sale_date, sum(units) units, sum(revenue) revenue
    from public.sales_daily group by 1, 2
) s
full outer join (
    select marketplace, ad_date, sum(spend) spend,
           sum(attributed_sales) attributed_sales,
           sum(attributed_units) attributed_units
    from public.ads_daily group by 1, 2
) a on a.marketplace = s.marketplace and a.ad_date = s.sale_date;

comment on view public.spend_daily is
    'Ad spend over units actually sold, per marketplace per day. Spend covers '
    'every ad program; units come from Orders, not from attribution.';

-- A view runs as its caller, so the RLS on sales_daily and ads_daily still
-- applies through it: a deactivated user reads nothing here either.
grant select on public.spend_daily to authenticated;
