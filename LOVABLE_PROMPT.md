# Connecting the feed to silverpottea.com

You have an admin panel at `silverpottea.com/admin` where you type the stock
number in each week. That means there is already a database behind your site,
and the admin panel is just a form that writes to it.

So we do **not** change your website's code at all. The daily job writes into the
same database your admin panel writes to. The storefront keeps reading the same
number it always has, and your in-stock, out-of-stock and order-limit rules keep
working exactly as they do now. The only difference is that the number arrives
by itself each morning instead of you typing it.

Your admin panel keeps working too, as a manual override for when you need to
pin a SKU by hand.

---

## Message 1 — send this to Lovable first

Copy and paste this exactly:

> I have an admin page where I manually enter a stock quantity for each product
> each week. I want to know exactly where that number is stored so I can update
> it automatically from an outside system.
>
> Please tell me, in plain terms:
>
> 1. Is the stock quantity stored in a Supabase table? If yes, what is the exact
>    table name and the exact column name that holds the quantity?
> 2. What column identifies each product in that table, and what does it
>    contain — a product name, an internal id, or a SKU code?
> 3. Does that table already have a column holding a SKU code? If so, what is it
>    called?
> 4. Which parts of the storefront read that quantity to decide in-stock,
>    out-of-stock, and maximum order quantity?
>
> Do not change any code yet. Just tell me the names.

Send me the answer and I will finish the configuration.

---

## Message 2 — send this once Lovable has answered

This adds three small columns. It does not change any existing behaviour.

> Please make these changes to the products table, without altering any existing
> in-stock, out-of-stock, or order-limit logic:
>
> 1. Add a text column `external_sku`, nullable, with a unique index on it where
>    it is not null. This will hold the marketplace SKU code for each product.
> 2. Add a timestamptz column `inventory_synced_at`, nullable.
> 3. Add a text column `inventory_source`, default `'manual'`.
> 4. In my admin page, add an editable "External SKU" field for each product so I
>    can paste the SKU codes in, and show `inventory_synced_at` as read-only text
>    next to the stock quantity so I can see when it last updated.
> 5. In my admin page, whenever I manually edit a stock quantity, set that
>    product's `inventory_source` to `'manual'`. Add a small toggle labelled
>    "Auto-update stock" which, when switched on, sets `inventory_source` back to
>    `'auto'`.
> 6. Do not show `inventory_source` or `inventory_synced_at` anywhere on the
>    customer-facing storefront.

Then paste each tea's SKU code into the new External SKU field. They are in
`sku_map.csv`, in the `sku` column. For example, Original Darjeeling is
`R9-D7AT-S5WW`.

### Why the manual toggle matters

Without it, the daily job silently overwrites anything you set by hand. If you
ever hold stock back for a retail order, or pin a SKU during the barcode
changeover, you want that to survive the next morning's sync. With the toggle,
a product set to `manual` is skipped entirely until you switch it back to auto.

---

## Message 3 — the Supabase keys

Send this to Lovable, or find it yourself in your Supabase dashboard under
Project Settings → API:

> Please tell me my Supabase project URL and my service role key.

The service role key is powerful — it bypasses all row-level security. It goes
straight into GitHub Secrets and nowhere else. Never paste it into the website
code or anywhere public.

---

## What I do with your answers

You send me four things:

1. The table name
2. The stock quantity column name
3. Your Supabase project URL
4. Confirmation that the service role key is saved in GitHub Secrets

I set five values in your GitHub repository and the loop closes. Nothing else in
the code changes — `collector/website.py` is already written and waiting for
these names.

---

## One thing to change later

Your maximum-order-quantity rule currently works off a single warehouse number.
It will now work off a pool split between Amazon's and Walmart's warehouses. A
customer ordering 40 tins against a shown 45 might be drawing 30 from Amazon and
15 from Walmart, which means two separate parcels and two fulfilment fees.

If you cap order size, it is better to cap against the larger of the two rather
than the total, so one order ships from one place. Both numbers are in the feed
already. Not urgent, but worth doing before you push any big promotion.
