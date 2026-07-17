# Inventory Alerts Signup — PRD (BLACKWHOLE-10)

Buyers register on **black-whole.com** for email/SMS alerts about chair lots near
them or matching what they want. New sellable inventory → blast the matching
subscribers (throttled, logged, one-click unsubscribe). Sister ticket
**BWCRM-26** builds the CRM-side UI for the subscriber↔contact join; this doc
owns the shared schema + events.

> **Strategic intent.** The alert list *replaces* parking buyers inside the CRM.
> Today a "Parked / await_hub" buyer sits in the pipeline waiting for a manual
> re-engage. Tomorrow: they register on the site, the site manages the wake-up
> (`Parked --reengage_trigger--> Needs reply` in the CRM FSM becomes an alert
> send), and the CRM just *sees* the link. We manage them there and also connect
> them here.

## 1. Problem + goals

**Problem.** Demand outlives inventory. Buyers ask "do you have black banquet
chairs near Atlanta?" when we don't — and by the time we do, the thread is cold.
The re-engagement loop is 100% manual: the operator remembers a parked buyer,
scrolls FB Messenger, types a message. Meanwhile the site's only capture path is
the one-off `inquiries` contact form, and the `/listings` empty state has a dead
**NOTIFY ME** button pointing at `/#contact`.

**Goals**

- Capture buyer intent as a durable, *queryable* subscription: who, channel,
  where (zip + radius), what (chair type / color / qty band).
- On new sellable inventory, notify only the subscribers it actually matches —
  never the whole list.
- Every send is logged, capped, deduped (never re-alert the same lot), and
  carries a working unsubscribe.
- Join subscribers to CRM `contacts` on email/phone so both systems see one
  buyer.

**Non-goals**

- **No buyer accounts/login.** No passwords, no profile pages. A subscription is
  a row + an unsubscribe token, nothing more.
- **No marketing-automation platform.** No drip campaigns, segments, A/B
  subject lines, open-rate dashboards. One trigger (new inventory), one message.
- No auto-posting links into FB Marketplace threads (locked decision, §2).
- No replacement of the `inquiries` table — that stays the one-off "I want THIS
  lot now" path.

## 2. Personas + capture flows

**Persona A — the far bulk buyer.** "Need 800 black banquet chairs, I'm in
Houston." We have nothing in TX. Today: `Parked / out_of_area`. Tomorrow: alert
subscriber, radius 300 mi, qty band 500+.

**Persona B — the local event business.** Buys 50–200 every season. Wants a ping
whenever anything lands within an hour of them.

**Persona C — the picky type-hunter.** Only wants gold chiavari, anywhere.
Interest filter carries the match; geo is wide or ignored.

### Flow 1 — operator-adds-from-chat (P0)

**LOCKED decision: never mass-post links in FB.** Meta reads link blasts as spam
(and the account is already fragile — see the CRM's anti-detection posture).
The opt-in happens *in conversation*; the site link goes out 1:1, after consent,
as a normal reply.

Suggested copy, operator's voice (short, plain):

> "want me to add you to my alerts? i'll ping you when chairs land near you.
> email or number?"

> "got it — you're on the list. anything close to {city} and i'll text you.
> here's the site if you want to browse: black-whole.com/listings"

The operator then adds them via the admin form (or the MCP/CRM later): name,
email-or-phone, zip, rough interest. The buyer's consent message in the thread
is the consent record — we store the `thread_url` + a quoted snippet.

### Flow 2 — public signup form (P1)

- Wire the dead **NOTIFY ME** button (`listings.html` empty state, currently
  `/#contact`) to the signup form.
- A slim signup block on `/listings` (always, not just when empty) + a section
  on the landing page.

Suggested copy:

> **Get first dibs.** New truckloads land every few weeks. Tell us where you
> are and what you need — we'll ping you when a match hits the floor. No spam,
> one-tap unsubscribe.

Fields: name · email or phone (≥1 required, same rule as `create_inquiry`) ·
zip · radius (dropdown: 50 / 100 / 250 / anywhere) · chair type (any/banquet/
chiavari/folding/…) · color (any/black/gold/…) · quantity band.

### Flow 3 — invite Parked CRM contacts (P2)

Qualifying Parked buyers (`await_hub`, `await_restock`, `high_volume`,
`out_of_area` per the CRM FSM tags) get a 1:1 invite in their existing thread —
same channel they already talked to us on, so it's a reply, not a blast:

> "still hunting chairs? i set up alerts on my site — register once and you'll
> get a ping the moment something lands near {city}: black-whole.com/listings"

BWCRM-26 adds the "Invite to alerts" button + shows link status on the contact.

## 3. Data model (Supabase Postgres)

Three new tables, distinct from `inquiries` (one-off lead ≠ standing
subscription). Same DB as `inventory` (project `nihgzltpjriekyqqucbd`), accessed
via `automation/db.py` like everything else — no ORM, schema via migration.

### `subscribers`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `uuid` PK `default gen_random_uuid()` | |
| `name` | `text not null` | |
| `email` | `citext` unique, nullable | normalized lowercase; **join key → CRM `contacts.email`** |
| `phone` | `text` unique, nullable | **E.164** (`+1480…`); **join key → CRM `contacts.phone`** (CRM stores buyer-shared phone/email, default `""` — normalize both sides before matching) |
| `channel` | `text` check `('email','sms','both')` | must be consistent with which of email/phone is set |
| `zip_code` | `text` nullable | 5-digit |
| `state` | `text` nullable | 2-letter, same normalization as CRM `state_zips.normalize_state` |
| `lat` / `lon` | `double precision` nullable | resolved at insert: zip → `pgeocode` (same as CRM `geo_utils.zip_to_latlon`), else state centroid; `geo_precision` `('zip','state',null)` |
| `radius_miles` | `int default 100` | `0` = anywhere (interest-only match) |
| `source` | `text` check `('operator_chat','public_form','crm_invite')` | |
| `consented_at` | `timestamptz not null` | |
| `consent_evidence` | `text` | quoted opt-in message / "public form" + IP |
| `crm_thread_url` | `text` nullable | optional exact join → CRM `contacts.thread_url` (canonical facebook.com form) |
| `unsubscribe_token` | `text unique not null` | ≥32 bytes urlsafe random, generated server-side |
| `status` | `text` check `('active','unsubscribed','bounced','invalid') default 'active'` | |
| `unsubscribed_at` | `timestamptz` | |
| `created_at` / `updated_at` | `timestamptz` | |

Constraint: `check (email is not null or phone is not null)`.

### `subscriber_interests`

Child table (not JSONB): the matcher is a SQL-side filter and one subscriber can
hold several independent wants ("any chiavari" AND "500+ banquet near me").
`NULL = wildcard` on every filter column.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `bigserial` PK | |
| `subscriber_id` | `uuid` FK → `subscribers(id)` `on delete cascade` | |
| `chair_type` | `text` nullable | matches `inventory.chair_type`, normalized lowercase |
| `color` | `text` nullable | needs a `color` column on `inventory` or LLM-extract from title/description (open question §8) |
| `qty_min` / `qty_max` | `int` nullable | band vs `inventory.quantity_remaining`; bands offered in UI: 1–49, 50–199, 200–499, 500+ |
| `created_at` | `timestamptz` | |

A subscriber with zero interest rows = "anything near me" (geo-only match).

### `alert_sends`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `bigserial` PK | |
| `blast_id` | `uuid not null` | groups one lot's blast run |
| `subscriber_id` | `uuid` FK → `subscribers(id)` | |
| `lot_id` | `text` FK → `inventory(lot_id)` | |
| `channel` | `text` check `('email','sms')` | |
| `status` | `text` check `('queued','sent','delivered','bounced','failed','suppressed')` | |
| `provider_message_id` | `text` | Resend/Twilio id, for webhook status updates later |
| `match_reason` | `jsonb` | `{distance_miles, geo_precision, interest_id}` — audit + CRM display |
| `error` | `text` | |
| `sent_at` | `timestamptz` | |

**Dedupe invariant:** `unique (subscriber_id, lot_id, channel)` — a subscriber
is never alerted twice about the same lot, even across re-blasts or a
quantity/price edit. (Re-alerting on *restock after sell-out* would need an
explicit new blast with the constraint row deleted — deliberate operator act.)

### CRM join (shared contract with BWCRM-26)

- Join keys, in precedence order: `crm_thread_url = contacts.thread_url` (exact)
  → `lower(email) = lower(contacts.email)` → E.164 `phone = contacts.phone`.
- Matching runs **CRM-side** (BWCRM-26) as a view/query — no FK across concerns,
  both apps read the same Supabase DB.
- Events the CRM consumes (rows, not a bus): a new `subscribers` row where a join
  key matches a contact ⇒ CRM shows "on alert list" (and can auto-move
  Parked → managed-by-site); an `alert_sends` row ⇒ CRM shows "alerted about
  lot X on {date}" and may surface the thread in **Today** as a follow-up.

## 4. Matching engine

Runs server-side in the FastAPI app (pure Python + one SQL pass), mirroring the
CRM's geo approach (`geo_utils.haversine_miles`, zip→latlon via `pgeocode`).

1. **Resolve the lot's coordinates.** `inventory.zip_code` → latlon; fallback
   state centroid; tag `precision ∈ (zip, state, none)` — identical ladder to
   the CRM's `record_latlon` and the site's `/api/inventory/map`.
2. **Candidate set (SQL).** `subscribers.status = 'active'`, joined to
   `subscriber_interests`, filtered on the cheap columns first
   (chair_type/color equality-or-null, qty band vs `quantity_remaining`).
3. **Distance filter (Python).** `haversine_miles(sub, lot) <= radius_miles`.
   `radius_miles = 0` skips geo. If *either* side is state-precision only,
   compare centroids but degrade honestly: also pass on `state == state`
   (centroid math on two state centroids is ±200 mi noise).
4. **Dedupe.** `INSERT … ON CONFLICT (subscriber_id, lot_id, channel) DO
   NOTHING` into `alert_sends` as `queued` — the constraint *is* the dedupe.
5. **Batching.** Queue rows are drained by the sender (§5) with throttle; a
   blast is one `blast_id`, and the admin preview (§6) shows the exact recipient
   list + match reasons before anything sends.

Vocabulary note: `chair_type` and `color` must be normalized enums on both the
signup form and `inventory` rows (lowercase, fixed list), or matching silently
misses. Free-text interest is a non-goal.

## 5. Blast pipeline

### Trigger

New `inventory` rows appear from three writers: the pipeline (`upsert_from_run`),
`insert_manual`, and admin edits that flip status to sellable. Options:

| Option | Verdict |
| --- | --- |
| Hook the daily Render cron | ✅ P1 default. Add a nightly step (piggyback `scripts/run_discovery.sh` cadence or a second cron in `render.yaml`) that scans `inventory` for sellable rows with `alerted_at IS NULL`, stages matches as `queued`, and pings the operator (Telegram, already wired in `telegram_alerts.py`) for one-tap confirm. |
| Supabase DB webhook / edge function on INSERT | ❌ later. More moving parts, and inserts land as `draft` first — status flips are the real signal. |
| Admin "Blast" button per lot | ✅ P0. Manual, previewed, always available. |

**Ethos carry-over from the CRM: staged, never silently auto-sent.** The scan
stages + notifies; a human confirms. Add `inventory.alerted_at timestamptz` to
mark a lot processed. Auto-confirm can be revisited once P1 has run clean for a
month.

### Provider comparison (costs as of early 2026 — re-verify before signup)

**Email**

| | Resend | SendGrid |
| --- | --- | --- |
| Free tier | 3,000/mo (100/day) | 100/day |
| First paid tier | $20/mo → 50k | ~$20/mo → 50k |
| API/DX | Minimal REST, 5-line send, first-class `List-Unsubscribe` | Heavier API, legacy console |
| Deliverability setup | Own domain + SPF/DKIM/DMARC (both) | same |

**→ Recommend Resend.** Volume here is tens of sends per blast, a few blasts a
month — free tier covers it forever, and the API is the smallest to integrate.

**SMS**

| | Twilio | Telnyx | AWS SNS |
| --- | --- | --- | --- |
| Per US SMS | ~$0.0079 + carrier fees | ~$0.004 | ~$0.006–0.008 |
| Number | ~$1.15/mo | ~$1/mo | via Pinpoint |
| A2P 10DLC | Guided registration (~$4 brand one-time, ~$1.50–10/mo campaign) | Supported, more manual | Clunky |
| Inbound webhooks | Excellent | Good | Weak |

**→ Recommend Twilio.** At this volume the per-message delta is pennies; A2P
10DLC registration (mandatory for US application-to-person SMS) is the actual
hurdle and Twilio's flow is the most operator-survivable. Inbound webhook also
answers "where do replies land" (§8) — forward to Telegram.

### Throttle + caps

- Sequential sends, ≥1s spacing (Twilio long-code throughput is ~1 msg/s
  anyway; Resend default 2 req/s).
- `MAX_ALERTS_PER_DAY` env cap (start 100) — same pattern as the CRM's
  `MAX_SENDS_PER_DAY`. Overflow stays `queued` for the next day.
- SMS only between 09:00–20:00 recipient-local (approximate by lot/subscriber
  state tz) — TCPA quiet-hours safe harbor is 8am–9pm.

### Compliance

- **CAN-SPAM (email):** working unsubscribe link honored ≤10 business days
  (ours is instant), physical postal address in the footer, truthful
  subject/from. Add `List-Unsubscribe` + `List-Unsubscribe-Post` (RFC 8058)
  headers so Gmail shows native one-tap.
- **TCPA (SMS):** marketing texts require **prior express written consent**.
  A signed-by-typing web form checkbox qualifies; so does the buyer's typed
  "yes add my number" in the FB thread — which is why `consent_evidence` +
  `consented_at` are not-null-in-practice. Honor `STOP` (Twilio handles the
  keyword natively; our webhook must also flip `status='unsubscribed'`).
  Every SMS ends with "Reply STOP to opt out".
- **Single vs double opt-in: recommend single**, with evidence. Most
  subscribers are operator-added mid-conversation — a double-opt-in
  confirmation email at that moment is friction that kills the list before it
  exists, and neither CAN-SPAM nor TCPA requires double. Mitigations: store
  consent evidence on every row; send a welcome message ("you're on the list —
  unsubscribe: {link}") that doubles as address verification (hard bounce →
  `status='bounced'`); public form gets a honeypot field + per-IP rate limit
  instead of confirmation friction. Revisit double opt-in for the public email
  form only if spam signups actually appear (§8).

## 6. API surface (FastAPI, `automation/web/app.py`)

Public:

- `POST /alerts/subscribe` — body `{name, email?, phone?, zip?, radius_miles?,
  interests: [{chair_type?, color?, qty_min?, qty_max?}]}`. Validates ≥1 of
  email/phone (mirror `create_inquiry`), normalizes email/E.164/zip, resolves
  lat/lon, generates token, inserts. Honeypot field + per-IP rate limit
  (in-process, same spirit as the CRM send caps). Returns `{ok: true}` only —
  never echoes the row.
- `GET /alerts/unsubscribe?token=…` — flips `status='unsubscribed'`, renders a
  plain "you're off the list" page. Idempotent; unknown token renders the same
  page (no oracle). Also accept `POST` for RFC 8058 one-tap.

Admin (operator-only):

- `GET /api/alerts/subscribers` — list + filters (status/source/state).
- `POST /api/alerts/subscribers` — operator-add (Flow 1), accepts
  `crm_thread_url` + `consent_evidence`.
- `PATCH /api/alerts/subscribers/{id}` / `DELETE` — edit, hard-delete on request.
- `POST /api/alerts/blast/{lot_id}/preview` — runs the matcher, returns
  recipients + `match_reason`s, sends nothing.
- `POST /api/alerts/blast/{lot_id}` — stages `alert_sends` + drains the queue.
- `GET /api/alerts/sends?lot_id=|subscriber_id=` — the send log.

### Security boundary (must-fix before ship)

RLS is currently **disabled workspace-wide** and this feature adds a public
write path into a PII table. Minimal safe design:

1. **Only the FastAPI server touches these tables** — the browser posts JSON to
   `/alerts/subscribe`; no Supabase anon key exists in any page. The server
   uses the `BLACKWHOLE_DB_URL` DSN (effectively service-role).
2. **Enable RLS on `subscribers`, `subscriber_interests`, `alert_sends` with
   zero anon policies** at creation, even though other tables have RLS off.
   The Postgres-DSN path bypasses RLS, so nothing breaks — but the anon API key
   (which is public by design) can never read subscriber PII. This is the
   cheapest real boundary and doesn't wait on the workspace-wide RLS cleanup.
3. **Gate the admin routes.** `/admin` and `/api/*` currently ship unauthenticated;
   that was tolerable for run-launching, not for listing emails/phones. Minimum:
   a shared-secret header/basic-auth from an env var (`ADMIN_TOKEN`) on every
   `/api/alerts/*` admin route + `/admin`, checked in one dependency.
4. Unsubscribe token is a capability URL: ≥32 random bytes, constant-time
   compare, single-column index.

## 7. Rollout

### P0 — operator-add + manual blast

Tables + migration; admin add/edit/list; matcher; per-lot **Blast** button with
preview; Resend email sends; unsubscribe endpoint; welcome message; admin auth
gate; RLS-on-with-no-policies for the new tables.

*Accepts when:* operator adds a subscriber from a chat in <30s; blast preview
shows correct matches for a test lot (zip-radius + interest + wildcard cases);
a real email lands with a working unsubscribe that flips status; re-blasting
the same lot sends 0 (dedupe); `alert_sends` rows exist for every attempt;
`/alerts/subscribe` posted with the anon key against PostgREST returns denied.

### P1 — public form + auto-match

`POST /alerts/subscribe` public; NOTIFY ME wired + signup block on `/listings`
and landing; Twilio SMS channel (A2P 10DLC registered, STOP webhook); nightly
`alerted_at IS NULL` scan → staged blast + Telegram confirm ping; daily cap +
quiet hours.

*Accepts when:* a stranger can subscribe from the site on a phone in <60s;
honeypot/rate-limit blocks a scripted 100-signup burst; SMS send + STOP
round-trip flips status; nightly scan stages (not sends) and the Telegram
confirm delivers the blast; caps + quiet hours observably enforced in the log.

### P2 — CRM join + Parked→site handoff

Join view on email/phone/thread_url (BWCRM-26 owns UI); "Invite to alerts" from
a CRM thread pre-fills a subscriber (Flow 3 copy); CRM shows alert-list badge +
send history on contacts; Parked contacts with an active subscription stop
appearing in manual re-engage queues (the site owns the wake-up).

*Accepts when:* a subscriber added with a known buyer's email shows linked on
the CRM contact; an alert send surfaces on the CRM thread; a Parked+subscribed
contact is excluded from the CRM's follow-up queue; invite flow produces a
correctly-sourced (`crm_invite`) subscriber with `crm_thread_url` set.

## 8. Open questions

1. **"Ships nationwide" lots (the Boise 3,700).** A 3,700-chair lot the buyer
   freights themselves matches *every* bulk subscriber regardless of radius.
   Proposal: `inventory.ships_nationwide bool` — when true, skip the distance
   filter for interests with `qty_min >= 500` (freight only makes sense at
   volume). Needs operator sign-off on the qty threshold.
2. **Where do blast replies land?** Email: `reply_to` = operator Gmail — fine.
   SMS: replies hit the Twilio number; proposal: webhook → Telegram forward
   (reuse `telegram_alerts.py`) + log as a note, operator answers by text or
   moves them to Messenger. Do we ever want two-way SMS in the CRM proper?
3. **Double opt-in later?** Ship single (§5). Define the tripwire now: if
   bounce rate >5% or any spam-trap hit on the public form, add email
   confirmation for `source='public_form'` only.
4. **`color` on inventory.** No `color` column exists today — add one (enum) and
   backfill via the existing LLM refine step, or drop color from P0/P1 matching?
5. **Does the alert list fully retire CRM "Parked"?** Strategic intent says yes,
   but not every parked buyer will register. Interim rule (P2 accept criteria)
   treats subscription as the exit from manual parking; full retirement of the
   Parked re-engage queue is a BWCRM-26+1 decision.
6. **Restock re-alerts.** The dedupe constraint blocks re-alerting a lot that
   sold out and restocked. Is an explicit "re-open blast" admin action enough?
