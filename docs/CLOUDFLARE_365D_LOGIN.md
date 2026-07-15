# Stop re-logging into the dashboard — 365-day login runbook

**Goal:** log into `black-whole.com/admin` **at most once per device per year**,
instead of every ~month.

**Why you keep getting logged out today:** the admin dashboard sits behind
**Cloudflare Access** (a self-hosted Access application gating `/admin`,
`/api/*`, and `/screenshot/*`, with an allow-policy on your email + one-time
PIN — see `docs/superpowers/specs/2026-06-10-cloud-deploy-render-cloudflare-design.md`).
Cloudflare Access **caps its session at ~1 month** — there is no 365-day option.
So no matter what you set, Access re-prompts you roughly monthly.

**The fix (this PR):** the app now protects its own admin surface with a
**365-day signed-cookie login** (password, plus an optional authenticator code
the first time on each device). Once that is on, you can **take Cloudflare
Access off this app** and rely on the app's own year-long cookie. That is what
gets you to "log in once a year."

This is the same login system as the CRM (facebook-scraper-crm), on purpose —
same env-var names (`ADMIN_PASSWORD` / `SESSION_SECRET` / `TOTP_SECRET`), same
behavior — so it's one system to remember.

> **Safety note:** the app login is a **no-op until you set `ADMIN_PASSWORD`**.
> Until you do, the admin surface is protected *only* by Cloudflare Access. So
> the order below matters: turn on the app login **first**, confirm it works,
> and **only then** loosen Cloudflare Access. Never bypass Access while
> `ADMIN_PASSWORD` is unset — that would leave `/admin` wide open.

---

## Step 1 — Turn on the app login (Render env vars)

Render dashboard → **`black-whole-web`** service → **Environment** → add:

| Key | Value | Notes |
|-----|-------|-------|
| `ADMIN_PASSWORD` | a long passphrase you choose | **Required.** This is your login password. Unset = login disabled. |
| `SESSION_SECRET` | a long random string | **Required for durable sessions.** Generate one with `openssl rand -base64 48`. If unset, every deploy/restart logs you out again — the exact pain you're fixing, so don't skip it. |
| `TOTP_SECRET` | *(optional)* | Only if you want an authenticator code as a second factor. Leave unset to skip. See Step 4. |

Click **Save Changes**. Render redeploys the web service (~1-2 min).

These keys are already declared (as `sync: false`) in `render.yaml`, so they
show up in the dashboard ready for values — nothing secret is committed.

## Step 2 — Confirm the app login works (while Access is still up)

1. Open `https://black-whole.com/admin`. Cloudflare Access lets you through as
   usual (its login), **then** the app shows its own `/admin/login` page.
2. Enter your `ADMIN_PASSWORD`. You should land on the dashboard.
3. You're now signed in **for 365 days on this device** (a `bw_session` cookie).

If Step 2 works, the app login is live. Proceed to remove the monthly Access
prompt.

## Step 3 — Take Cloudflare Access off this app (the part that removes monthly re-login)

Cloudflare **Zero Trust** dashboard (`one.dash.cloudflare.com`) → **Access** →
**Applications** → open the **black-whole admin** application.

Pick **one** option:

- **Option A — Recommended: Bypass Access, let the app guard itself.**
  In the application's **Policies**, add a policy with **Action = `Bypass`** and
  **Include = `Everyone`** (or delete the Access application entirely). This
  stops Cloudflare from ever prompting you — the app's own 365-day password
  login is now the only gate. **Only safe because Step 1 set `ADMIN_PASSWORD`.**
  (Cloudflare still proxies/DNS/CDN the site as before; only the Access login
  gate is removed.)

- **Option B — Keep Access, just stretch it to its max (still ~monthly).**
  If you'd rather keep Cloudflare Access as a second layer, open the
  application → **Settings** → **Session Duration** → set it to the longest
  value offered (**1 month**). Also check your **login method** (Access →
  Settings → Login methods → One-time PIN) — the identity token has its own
  lifetime. This is the *most* Access can do: you'll still re-auth to Cloudflare
  about once a month, but the app cookie underneath lasts a year. Choose this
  only if you specifically want to keep Access; it does **not** fully remove the
  re-login pain.

**To actually stop the monthly re-login, use Option A.**

## Step 4 — (Optional) Authenticator code, once per device

If you set `TOTP_SECRET` in Step 1 and want a second factor:

1. On your Mac, from the repo root:
   ```
   python3 scripts/totp_provision.py --new
   ```
   It prints `TOTP_SECRET=...` and an `otpauth://` URI.
2. Put that `TOTP_SECRET` value into Render (Step 1 table) and **Save**.
3. Scan the `otpauth://` URI into an authenticator app (1Password, Google
   Authenticator, Authy...).
4. The **first** login on each device now asks for the 6-digit code as well;
   after that the device is trusted (`bw_device` cookie) and never asks again
   for a year. Logging out keeps the device trusted — it only clears the
   session, so you re-enter the password but not the code.

Leave `TOTP_SECRET` unset if you don't want this — password alone is enough for
"log in once a year."

---

## Quick reference

- **Log in:** `https://black-whole.com/admin` → password (+ code first time).
- **Lasts:** 365 days per device.
- **Sign out everywhere on this device:** `POST /api/auth/logout` (clears the
  session cookie; the device stays TOTP-trusted).
- **Rotate the password:** change `ADMIN_PASSWORD` in Render. Existing session
  cookies keep working until they expire (they aren't tied to the password).
- **Force every device to re-login:** change `SESSION_SECRET` in Render — that
  invalidates all existing session + device cookies immediately.
- **Env keys (all on `black-whole-web` in Render):** `ADMIN_PASSWORD` (required),
  `SESSION_SECRET` (required), `TOTP_SECRET` (optional).
