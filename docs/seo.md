# SEO — how black-whole.com gets found on Google

Shipped in BLACKWHOLE-11. This doc covers what the code does automatically
and the one-time manual steps only the operator can do.

## What the site now does automatically

- **Every public page describes itself** — unique `<title>` +
  `<meta name="description">`, canonical URL, and Open Graph tags (so
  links pasted into FB/iMessage/Slack unfurl with the hero photo).
- **Lot pages carry the local-ranking payload** — the title and
  description include quantity, price, and pickup city/state
  ("100× Burgundy Banquet Chairs — Athens, GA"), and an invisible
  JSON-LD `Product` block tells Google the price, availability, condition,
  and the exact city/state/zip where the chairs sit
  (`Offer.availableAtOrFrom`). This is what targets searches like
  *"banquet chairs for sale athens ga"*.
- **`/sitemap.xml`** — regenerated on every request from the inventory
  ledger; live lots appear, sold-out/hidden lots drop out on their own.
- **`/robots.txt`** — invites crawlers in, keeps them out of `/admin` and
  `/api/`, and points at the sitemap.

No cron, no rebuild step — it all renders from the ledger at request time.

## One-time operator setup (≈15 minutes)

1. **Google Search Console** — <https://search.google.com/search-console>
   1. Add property → **URL prefix** → `https://black-whole.com`.
   2. Choose the **HTML tag** verification method. Copy just the token
      (the `content="..."` value).
   3. In Render → `black-whole-web` → Environment, add
      `GOOGLE_SITE_VERIFICATION=<token>` and redeploy.
   4. Back in Search Console, click **Verify**.
2. **Submit the sitemap** — Search Console → Sitemaps → enter
   `sitemap.xml` → Submit.
3. *(Optional)* **Google Business Profile** —
   <https://business.google.com>. Only worth it if you want the business
   itself on Google Maps at its home base; it requires a real address +
   phone and a postcard/video verification. It does **not** help lots in
   other cities rank — the lot pages handle that.

## What to expect

- Indexing starts within days of sitemap submission; **ranking takes
  weeks to months** on a young domain. Watch Search Console →
  Performance for which queries you're appearing on.
- Each new city of inventory becomes rankable automatically the moment
  its lot page is public — nothing extra to do per lot.
- Force-check a single page: Search Console → URL Inspection → paste the
  lot URL → Request Indexing (useful for hot lots).

## Env vars

| Var | Default | Purpose |
|---|---|---|
| `PUBLIC_BASE_URL` | `https://black-whole.com` | canonical/OG/sitemap origin |
| `GOOGLE_SITE_VERIFICATION` | *(unset)* | Search Console HTML-tag token |

## Later (not built, deliberately)

- Per-city landing pages ("chairs in Boise") — revisit when there are
  enough lots per city that the pages aren't thin.
- `ItemList` JSON-LD on `/listings`, `FAQPage` on `/sell`.
