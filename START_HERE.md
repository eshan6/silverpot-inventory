# START HERE

Do these in order. Do not skip ahead — each step needs the one before it.

Where you are right now:

- Amazon developer registration: **submitted, waiting** (case 21676039541)
- Walmart API keys: **you have them**
- Everything else: not started

The system runs fine on Walmart alone. Amazon just starts filling in its column
when approval lands. So we build and test the whole thing now.

---

# STEP 1 — Download and unzip the files

Download `silverpot-inventory.zip` and unzip it. You get a folder called
`silverpot-inventory` containing:

```
silverpot-inventory/
├── README.md              detailed reference
├── START_HERE.md          this file
├── LOVABLE_PROMPT.md      used at the very end
├── sku_map.csv            your 31 teas (already filled in)
├── requirements.txt
├── collector/             the program (7 files, don't touch)
├── public/                the dashboard
└── .github/workflows/     the daily timer
```

You never need to edit anything inside `collector/`.

**Check the hidden folder came through.** The `.github` folder starts with a dot,
which means Windows and Mac hide it by default. If you can't see it, turn on
hidden files (Mac: Cmd+Shift+. in Finder — Windows: View → Hidden items). If it
didn't unzip, nothing will run automatically.

---

# STEP 2 — Create the GitHub repository

1. Go to `github.com/new`
2. Repository name: `silverpot-inventory`
3. Select **Private**. Not public — this repo will hold your Amazon and Walmart
   connection settings.
4. Do not tick "Add a README file". Leave the initialise options alone.
5. Click **Create repository**

You now have an empty repo. Keep the page open.

---

# STEP 3 — Upload the files

On your new empty repo page, click **"uploading an existing file"** (it's in the
line of text in the middle of the page).

Then drag the **contents** of the `silverpot-inventory` folder into the browser
window — not the folder itself. You want `README.md`, `collector`, `public`, and
the rest sitting at the top level of the repo, not nested inside another folder.

Click **Commit changes**.

**Then check `.github` uploaded.** Look at your repo's file list. If you don't
see a `.github` folder, the browser skipped it because it's hidden. Fix it like
this:

1. In your repo, click **Add file → Create new file**
2. In the filename box type exactly: `.github/workflows/inventory.yml`
   (typing the slashes creates the folders automatically)
3. Open `inventory.yml` from the unzipped folder in any text editor, copy
   everything, paste it in
4. Click **Commit changes**

---

# STEP 4 — Add your Walmart keys to GitHub

Now the repo exists, so the secrets have somewhere to go.

In your repo: **Settings** (top row) → in the left sidebar, **Secrets and
variables** → **Actions** → green **New repository secret** button.

Add two secrets, one at a time. The names must be typed exactly, capitals and
underscores included:

| Name | Value |
|---|---|
| `WALMART_CLIENT_ID` | your ClientId from the Walmart portal |
| `WALMART_CLIENT_SECRET` | your ClientSecret from the Walmart portal |

Once saved, GitHub will never show you these values again. That is intentional.
Keep your own copy somewhere safe, such as a password manager.

---

# STEP 5 — Set up Google Sheets

This is where the daily history gets stored. About fifteen minutes.

### 5a. Create the Google Cloud project

1. Go to `console.cloud.google.com`
2. Top of the page, click the project dropdown → **New Project**
3. Name it `silverpot-inventory` → **Create**
4. Wait for it to finish, then make sure it's the selected project

### 5b. Turn on the Sheets API

1. Left menu → **APIs & Services** → **Library**
2. Search for `Google Sheets API`
3. Click it → **Enable**

### 5c. Create the service account

A service account is a robot user with its own email address. The program signs
in as this robot instead of as you.

1. Left menu → **APIs & Services** → **Credentials**
2. **+ Create Credentials** → **Service account**
3. Name it `inventory-bot` → **Create and Continue**
4. Skip the optional role and user steps → **Done**

### 5d. Download its key

1. Click the service account you just created
2. **Keys** tab → **Add Key** → **Create new key** → **JSON** → **Create**
3. A `.json` file downloads. Open it in a text editor. You'll need the whole
   contents shortly.

### 5e. Create the sheet and share it

1. Go to `sheets.google.com`, create a blank spreadsheet
2. Name it `Silverpot Inventory`
3. Click **Share**
4. In the JSON file, find the line starting `"client_email"`. It looks like
   `inventory-bot@silverpot-inventory-123456.iam.gserviceaccount.com`. Paste
   that address into the Share box, set it to **Editor**, and send.

**This share step is the one people skip and it breaks everything.** The robot
cannot write to a sheet it hasn't been given access to.

5. Copy the sheet ID from the browser address bar. In
   `docs.google.com/spreadsheets/d/1AbC...XyZ/edit`, the ID is the long string
   between `/d/` and `/edit`.

### 5f. Add both to GitHub

Back to **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the entire contents of the JSON file, including the curly brackets |
| `GOOGLE_SHEET_ID` | the long string from the sheet URL |

---

# STEP 6 — First test run

In your repo, click the **Actions** tab. If GitHub asks you to enable workflows,
say yes.

You'll see **"Daily inventory sync"** in the left sidebar. Click it, then click
**Run workflow** → **Run workflow**.

Wait about a minute, then click into the run to see what happened.

**What success looks like at this stage:**

- Walmart numbers appear
- A `snapshots` tab and a `current` tab appear in your Google Sheet
- The log says something like `WARN no FBA match` for all 31 teas — **this is
  correct and expected**, because Amazon hasn't approved you yet
- The log says `Website push skipped` — also correct, we do that last

If it fails, copy the red error text and send it to me.

---

# STEP 7 — Put the dashboard online

1. Go to `dash.cloudflare.com`, sign up free if you need to
2. **Workers & Pages** → **Create** → **Pages** → **Connect to Git**
3. Authorise GitHub, pick `silverpot-inventory`
4. Framework preset: **None**
5. Build command: **leave completely empty**
6. Build output directory: type `public`
7. **Save and Deploy**

You get a web address like `silverpot-inventory.pages.dev`. Open it. You'll see
the dashboard with sample numbers until the first real sync replaces them.

---

# STEP 8 — When Amazon approves

Watch `sales@dcgnorthamerica.com` — that's the email on the case.

When approved:

1. Go to Developer Central → **Add new app client**
2. Name: `silverpot-inventory`, tick **Amazon Fulfillment** (*not* Inventory and
   Order Tracking — that role covers orders, not FBA inventory, and ticking it
   alone is what produced the 403). Add **Product Listing** if it stays denied.
3. Amazon shows you a **client ID** and **client secret** — save both
4. On the app row, dropdown → **Authorize**. This gives you a **refresh token**
   starting `Atzr|`. Save it. A refresh token keeps the roles it was minted
   with, so re-authorize and mint a fresh one any time you change the roles.
   Check it with `python -m collector.main --diagnose`.
5. Add three more GitHub secrets:

| Name | Value |
|---|---|
| `LWA_CLIENT_ID` | starts `amzn1.application-oa2-client...` |
| `LWA_CLIENT_SECRET` | the secret Amazon showed you |
| `LWA_REFRESH_TOKEN` | starts `Atzr|` |

6. Run the workflow again from the Actions tab. The Amazon column fills in.

---

# STEP 9 — Watch it for a week

Let it run daily on its own. Each morning, pick two or three teas and compare
the numbers in your Google Sheet against what Seller Central and Seller Center
actually show.

Do not connect the website until this week has passed. If the numbers are wrong
and the site is already reading them, you oversell.

---

# STEP 10 — Connect the website

Only after Step 9. Open `LOVABLE_PROMPT.md` and follow it. It has the exact
messages to send Lovable, in order.

---

## Rules that matter

- **Never paste a key or secret into a chat, a document, or a code file.** They
  go into GitHub Secrets and nowhere else.
- **Do not make the repo public.** Private only.
- **Do not click "Close this case"** on the Amazon developer case. That closes
  your application.
- **Do not delete the Helium 10 key** in the Walmart portal. Use the one
  labelled "My API Key".

## What each secret is for

| Secret | Purpose | Have it? |
|---|---|---|
| `WALMART_CLIENT_ID` | Read Walmart WFS stock | Yes |
| `WALMART_CLIENT_SECRET` | Read Walmart WFS stock | Yes |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Write to your sheet | Step 5 |
| `GOOGLE_SHEET_ID` | Which sheet to write to | Step 5 |
| `LWA_CLIENT_ID` | Read Amazon FBA stock | After approval |
| `LWA_CLIENT_SECRET` | Read Amazon FBA stock | After approval |
| `LWA_REFRESH_TOKEN` | Read Amazon FBA stock | After approval |
| `SUPABASE_URL` | Update website stock | Step 10 |
| `SUPABASE_SERVICE_KEY` | Update website stock | Step 10 |

The program checks which secrets exist and skips whatever isn't configured yet,
so partial setup is safe at every stage.
