# R4 — GovAuctions.app's full source roster + new-source access-ladder verdicts

**Scope.** Enumerate every platform GovAuctions.app aggregates (via its own `/sources`,
`/uk/sources`, `/au/sources`, `/ca/sources` pages + JSON-LD `ItemList` data), diff against
our already-verified 11-source US scorecard (2026-07-23), and run a light access-ladder
probe (robots.txt + one page/search fetch) on every source that's new to us.

---

## 1. GovAuctions.app's full roster, as published

Fetched directly from `govauctions.app/{,uk/,au/,ca/}sources` JSON-LD `ItemList` blocks
(exact `position`-ordered arrays, not a model summary — see evidence links).

| # | Source | Country | URL | GovAuctions category tag |
|---|---|---|---|---|
| 1 | GovDeals | US | govdeals.com | State/local govt surplus |
| 2 | Public Surplus | US | publicsurplus.com | State/local govt surplus |
| 3 | CivilView | US | salesweb.civilview.com | County sheriff-sale & tax-sale real estate |
| 4 | GovPlanet | US | govplanet.com | Military/federal-disposition surplus |
| 5 | J.J. Kane Auctioneers | US | jjkane.com | Govt fleet/vehicle/utility-equipment surplus |
| 6 | GSA Auctions | US | gsaauctions.gov | Federal govt surplus |
| 7 | HUD HomeStore | US | hudhomestore.gov | Federal FHA-foreclosed homes |
| 8 | Fannie Mae HomePath | US | homepath.fanniemae.com | Federal REO real estate |
| 9 | Bid4Assets | US | bid4assets.com | County tax-defaulted/sheriff/forfeiture real estate |
| 10 | Apple Auctioneering | US | appleauctioneeringco.com | US Marshals/Treasury seized personal property |
| 11 | Municibid | US | municibid.com | Small-town municipal surplus |
| 12 | Purple Wave | US | purplewave.com | Govt fleet/equipment/vehicle surplus |
| 13 | Freddie Mac HomeSteps | US | homesteps.com | Freddie Mac REO foreclosed homes |
| 14 | PropertyRoom | US | propertyroom.com | Police-seized & unclaimed property |
| 15 | Michigan MiBid | US | mibid.state.mi.us | Michigan state govt surplus |
| 16 | Illinois iBid | US | ibid.illinois.gov | Illinois state govt surplus |
| 17 | GSA Real Estate | US | realestatesales.gov | Federal real property (land/buildings) |
| 18 | CWS Marketing | US | cwsmarketing.com | US Treasury seized/forfeited property + US Customs merchandise |
| 19 | NCM Auctions | UK | ncmauctions.co.uk | Industrial plant & machinery |
| 20 | BPI Auctions | UK | bpiauctions.com | Asset, plant & vehicle auctions |
| 21 | Eddisons | UK | eddisons.com | Plant, machinery & business assets |
| 22 | Apex Auctions | UK | apexauctions.co.uk | Industrial & plant machinery |
| 23 | Ramco | UK | ramco.co.uk | Ex-MoD, commercial & industrial surplus |
| 24 | Witham (Specialist Vehicles) | UK | withamsv.com (operates as **mod-sales.com**) | Ex-MoD vehicles |
| 25 | Grays | AU | grays.com | Ex-govt & commercial surplus |
| 26 | Pickles | AU | pickles.com.au | Ex-govt & ex-fleet vehicles |
| 27 | Allbids | AU | allbids.com.au | ACT/federal govt & surplus |
| 28 | GCSurplus | CA | gcsurplus.ca | Federal govt surplus (PSPC) |
| 29 | Police Auctions Canada | CA | policeauctionscanada.com | Police-seized & unclaimed property |
| 30 | Alberta Surplus Sales | CA | surplus.gov.ab.ca | Alberta provincial govt surplus |
| 31 | GovDeals Canada | CA | govdeals.com | Canadian municipal & provincial surplus |

**Count discrepancy.** GovAuctions.app's own FAQ JSON-LD states *"GovAuctions.app indexes
32 official government auction sources across 4 countries"*, but the four sources pages'
`ItemList` arrays sum to exactly **31** (18 US + 6 UK + 3 AU + 4 CA — verified by counting
`ListItem` nodes in raw JSON-LD, not a page summary). Either the "32" is stale marketing
copy, or a 32nd source exists on a page we didn't find (no `/nz/sources`, `/eu/sources`,
etc. exist per sitemap). Flagging, not resolving.

**Notable omission:** GovAuctions.app does **not** index **HiBid**, despite HiBid being a
large multi-tenant GraphQL platform used by hundreds of small government/municipal sellers
(it's in our own scorecard). Either a deliberate gap or an opportunity for our aggregator.

Evidence:
- [govauctions.app/sources](https://govauctions.app/sources)
- [govauctions.app/uk/sources](https://govauctions.app/uk/sources)
- [govauctions.app/au/sources](https://govauctions.app/au/sources)
- [govauctions.app/ca/sources](https://govauctions.app/ca/sources)

---

## 2. Overlap with our existing 11-source scorecard

Of GovAuctions.app's 18 US sources, **10** are already in our verified scorecard:
GovDeals, Public Surplus, GovPlanet, GSA Auctions, Bid4Assets, Municibid, Purple Wave,
PropertyRoom, Michigan MiBid, Illinois iBid. (Our scorecard's 11th, HiBid, isn't in
GovAuctions' list at all — see above.)

That leaves **8 new US sources** + **13 new international sources** = **21 total new
sources** probed below.

---

## 3. Access-ladder verdicts — the 21 new sources

**Ladder tiers used:** (1) Official API → (2) Hidden internal JSON/XHR → (3) RSS →
(4) Browser scrape required. "Furniture volume" is a qualitative estimate from category
labels/live listing samples only — no bulk crawl was run.

| Source | Country | Est. liquidation-furniture volume | Access verdict | Sold-prices availability | Anti-bot |
|---|---|---|---|---|---|
| **CivilView** | US | None — sheriff-sale/tax-sale **real estate** only, no goods | (4) Browser/HTML scrape, per-county pages. No robots.txt file (404) | Docket-style, per-county, not itemized comps | None detected (IIS/ASP.NET MVC + AWS ALB, plain) |
| **J.J. Kane Auctioneers** | US | None — vehicles, fleet & utility equipment only, no furniture category found | (4) Browser scrape. WordPress marketing site (Yoast robots.txt, allow-all + sitemap); actual auction/bid platform is separate infra, not probed | Not visible on public pages (upcoming-only) | None detected |
| **HUD HomeStore** | US | None — federal foreclosed **homes** only | **Robots.txt blocks the entire site** (`Disallow: /`) — do not scrape | N/A | Explicit crawl-ban (not a technical block, a policy one) |
| **Fannie Mae HomePath** | US | None — REO foreclosed **homes** only | (4) Heavy Angular SPA (`app-root`, bundled JS); no conventional robots.txt served (returns app shell). Likely a JSON API backs its map search but unconfirmed | Not probed (irrelevant vertical) | Unconfirmed |
| **Apple Auctioneering** | US | None confirmed — homepage messaging is "**All Seizures – No Dealer Consignments**," vehicles-only | Auctions actually run on third-party **HiBid** (`appletowing.hibid.com`) — reuses HiBid's known access method (GraphQL + Cloudflare), already in our scorecard | Not probed here (see HiBid) | Wix-hosted marketing site, allow-all robots.txt except PetalBot |
| **Freddie Mac HomeSteps** | US | None — REO foreclosed **homes** only | (4) Drupal site, robots.txt only disallows `/core/`, `/profiles/` — mostly crawlable, but irrelevant vertical | Not probed (irrelevant vertical) | None detected |
| **GSA Real Estate** | US | None — federal **real property** (land/buildings) | **Robots.txt blocks the entire site** (`Disallow: /`) — do not scrape | N/A | Explicit crawl-ban |
| **CWS Marketing** (operates as **CWSAMS**) | US | Low–moderate — "General Merchandise," antiques, collectibles catch-all; no dedicated furniture line, but plausible in seized-property lots | (4) Browser scrape only — **robots.txt request itself returned HTTP 403** (bare `curl` blocked; browser-rendered fetch succeeded), i.e. WAF/edge fingerprinting, not a clean allow | "Past Auctions" link exists but sold prices not directly observed | **Yes** — selective bot-blocking at the edge |
| **NCM Auctions** | UK | **Good fit** — explicit "Office furniture & supplies" category (5 live lots seen) alongside industrial/catering/gym/plant liquidation | (4) Browser scrape. Runs on **Auction Technology Group (Metropress)** white-label platform (`bidonline.ncmauctions.co.uk`, Azure CDN) — same footer credit found on Ramco, suggesting a shared ATG backend worth probing once for a common JSON API | Not visible on the fetched page (upcoming-only); "Past auctions" link unexplored | None detected on marketing domain (10s crawl-delay only) |
| **BPI Auctions** | UK | Low — broad liquidation/insolvency auctioneer (agri, plant, forklifts, catering, "Home & Garden") but no dedicated furniture category found | (4) Browser scrape, allow-all robots.txt + sitemap | Not visible on fetched page | Minimal — one honeypot form field only |
| **Eddisons** | UK | Main domain (eddisons.com) = **real estate only**, irrelevant. The plant/machinery/business-assets arm (**eddisonsassets.com**, per GovAuctions' own tag) returned **HTTP 500** on probe — inconclusive, retry later | (4) Browser scrape (main domain allow-all robots.txt); assets subdomain unconfirmed | Not probed (error) | Unconfirmed |
| **Apex Auctions** | UK | Unconfirmed — industrial & plant machinery framing suggests low furniture relevance; `/auctions` path 404'd, correct listings URL not found in this pass | (4) Browser scrape, allow-all robots.txt + sitemap-index present | Not probed | None detected |
| **Ramco** | UK | Unconfirmed but plausible — explicitly offers "public-sector asset disposal" + "full-site clearances" (office-furniture-adjacent), though the one sampled auction page showed industrial/textile/AV/gym categories, no furniture line confirmed | (4) Browser scrape. Live auctions at `auctions.ramco.uk`, same **ATG/Metropress** platform as NCM Auctions | Not visible on fetched page | None detected; marketing-domain robots.txt only disallows sales-funnel landing pages |
| **Witham (Specialist Vehicles)** | UK | None — ex-MoD vehicles/plant only, confirmed zero furniture categories | (4) Browser scrape (Next.js app). Robots.txt explicitly disallows `/api/` — **a JSON API exists internally but is robots-gated**; respect it, don't hit it directly | Not visible; irrelevant vertical | **Cloudflare confirmed** (email-obfuscation markers present) |
| **Grays** | AU | **Good fit** — explicit "Office Furniture and Equipment" category, 42 live lots seen (desks, filing cabinets, storage units) sourced from ex-government/commercial surplus incl. WA Police forfeitures | (4) Browser scrape; **Next.js** stack (`x-powered-by: Next.js` header confirmed) — plausible hidden JSON/API route backing search, worth a follow-up XHR probe | Not seen on this search page; Grays likely has a separate "sold" archive (common for AU industrial auctioneers) — unconfirmed | None detected |
| **Pickles** | AU | None — ex-govt & ex-fleet **vehicles** only per GovAuctions' own tag | Not deep-probed given zero furniture relevance; robots.txt shows a mature, actively-defended crawl surface (long locale/search-pattern disallow list) | Not probed | Not probed |
| **Allbids** | AU | **Good fit** — explicit "Office Furniture" + "Office & Business Equipment" + "Computers" categories; "100,000+ items/year" from govt depts, police, estate executors | (4) Browser scrape required — **bare `curl` to homepage returned HTTP 403**, WebFetch (browser-like) succeeded — same selective-bot-blocking pattern as CWS Marketing | Not seen on homepage | **Yes** — blocks naive HTTP clients (IIS/.NET stack, robots.txt has targeted disallows) |
| **GCSurplus** | CA | Plausible but unconfirmed — Canada's federal (PSPC) surplus program, GSA-equivalent; category browse not reached due to a redirect/cert quirk hit during probing | (4) Browser scrape. **No robots.txt file** (404/redirect-loop) → no explicit bot policy stated. Legacy ColdFusion (`.cfm`) + Government-of-Canada WET-BOEW framework — almost certainly no JSON API | Not probed | None detected — fragile/legacy infra (a historical "cyber security vulnerability" notice banner is still live on the 404 page), not deliberate bot defense |
| **Police Auctions Canada** | CA | Unconfirmed — "police-seized & unclaimed property," mixed general merchandise, furniture plausible but no dedicated category confirmed | (4) Browser scrape only — hardest of the CA sources | Not probed | **Yes — confirmed Cloudflare** (`cf-ray` header present; plain fetch blocked with 403). Robots.txt also disallows `/RealTime/` and references `/signalr/`, confirming a live-bid WebSocket backend that should not be hit directly |
| **Alberta Surplus Sales** | CA | Plausible — provincial general-surplus program (Public Auctions / Online Auctions / Buy It Now / Tenders sections), but category-level furniture presence unconfirmed | (4) Browser scrape. **No robots.txt file** (404) → no explicit bot policy. Custom-built govt site, not a known platform | Not probed | None detected |
| **GovDeals Canada** | CA | Same distribution as US GovDeals (chairs/office furniture routinely appear) | Same domain, same platform as US GovDeals — **reuses the maestro JSON API already in our scorecard**. No new access method to characterize | Same as US GovDeals (not separately re-verified here) | Same as US GovDeals |

---

## 4. Practical read for BLACKWHOLE

- **Best new furniture-liquidation candidates:** Grays (AU), Allbids (AU), NCM Auctions
  (UK) all have an *explicit* office-furniture/business-assets category with live listings
  confirmed by direct fetch — not inferred. Ramco and BPI Auctions (UK) are plausible via
  "public-sector asset disposal" / "liquidation & bankruptcy" framing but need a deeper
  category-page check before committing engineering time.
- **Zero-value for a furniture/goods comps dataset, confirmed:** CivilView, HUD HomeStore,
  Fannie Mae HomePath, Freddie Mac HomeSteps, GSA Real Estate, Witham/mod-sales.com,
  Pickles — all real-estate- or vehicles-only. Two of these (HUD HomeStore, GSA Real
  Estate) are **also robots.txt-blocked outright**, so they're doubly not worth building
  against.
- **Selective anti-bot pattern worth knowing:** three sources (CWS Marketing, Allbids,
  Police Auctions Canada) block plain `curl`/HTTP-client requests (403 or robots.txt 403)
  while allowing browser-rendered fetches — i.e., basic User-Agent/TLS-fingerprint or
  Cloudflare JS-challenge gating, not full bot walls. This is the same tier as several
  sources already in our scorecard (GovPlanet's AWS-WAF, HiBid's Cloudflare) — solvable
  with the same Patchright non-headless approach already standard in this codebase, not a
  new problem class.
- **Shared white-label platform spotted:** NCM Auctions and Ramco (both UK) run on the
  same **Auction Technology Group / Metropress** backend. If BLACKWHOLE ever wants UK
  coverage, probing that platform's shared JSON/XHR layer once could unlock both (and
  possibly more ATG-powered UK auction houses) for the price of one integration.
- **GovAuctions.app's own gap (HiBid)** is a legitimate differentiation angle if we ever
  build a public-facing aggregator — HiBid alone likely carries meaningful chair/furniture
  volume across its many small-municipality tenants (already in our scorecard, GraphQL +
  Cloudflare) and GovAuctions.app doesn't touch it.

---

## 5. Method notes / caveats

- All probing was "light" per instructions: one `robots.txt` fetch + one search/listing
  page fetch per new source, no bulk crawling, no auth bypass attempts, no paywall
  circumvention.
- Two sources (`withamsv.com`, `ncmauctions.com`) as spelled on GovAuctions.app's own pages
  do **not** resolve/serve correctly — the real operating domains are `mod-sales.com` and
  `ncmauctions.co.uk` respectively (found via search + TLS-cert-mismatch error messages).
  GovAuctions.app's source list itself appears to have stale/legacy domain strings for at
  least these two.
- A handful of fetches failed cleanly and weren't retried further given the "light
  probing" budget: `eddisonsassets.com` (HTTP 500), `apexauctions.co.uk/auctions` (404,
  correct path not found), `gcsurplus.ca` category browse (redirect/cert quirk). These are
  flagged as unconfirmed above rather than guessed at.
- Furniture-volume estimates are qualitative, based on category labels and one sample of
  live listings each — not a market-sizing exercise. Treat as "worth a second look" vs.
  "skip," not as forecast numbers.

---

## 6. Evidence links

- [govauctions.app/sources](https://govauctions.app/sources) (US, 18 sources, JSON-LD verified)
- [govauctions.app/uk/sources](https://govauctions.app/uk/sources) (UK, 6 sources)
- [govauctions.app/au/sources](https://govauctions.app/au/sources) (AU, 3 sources)
- [govauctions.app/ca/sources](https://govauctions.app/ca/sources) (CA, 4 sources)
- [govauctions.app/robots.txt](https://govauctions.app/robots.txt)
- [salesweb.civilview.com](https://salesweb.civilview.com/)
- [jjkane.com/auctions](https://www.jjkane.com/auctions)
- [hudhomestore.gov/robots.txt](https://www.hudhomestore.gov/robots.txt) — full disallow
- [homepath.fanniemae.com](https://homepath.fanniemae.com/)
- [appleauctioneeringco.com](https://www.appleauctioneeringco.com/)
- [homesteps.com/robots.txt](https://www.homesteps.com/robots.txt)
- [realestatesales.gov/robots.txt](https://www.realestatesales.gov/robots.txt) — full disallow
- [cwsmarketing.com](https://www.cwsmarketing.com/)
- [ncmauctions.co.uk](https://www.ncmauctions.co.uk/) / [bidonline.ncmauctions.co.uk/auctions](https://bidonline.ncmauctions.co.uk/auctions)
- [bpiauctions.com/auctions](https://www.bpiauctions.com/auctions)
- [eddisons.com/auctions](https://www.eddisons.com/auctions/)
- [apexauctions.co.uk/robots.txt](https://www.apexauctions.co.uk/robots.txt)
- [ramco.co.uk/public-sector-asset-disposal](https://www.ramco.co.uk/public-sector-asset-disposal) / [auctions.ramco.uk/auctions](https://auctions.ramco.uk/auctions)
- [mod-sales.com/stock](https://www.mod-sales.com/stock) (Witham's real domain)
- [grays.com/search/office-furniture](https://www.grays.com/search/office-furniture)
- [allbids.com.au](https://www.allbids.com.au/)
- [pickles.com.au/robots.txt](https://www.pickles.com.au/robots.txt)
- [gcsurplus.ca](https://www.gcsurplus.ca/mn-eng.cfm) (redirect-heavy, legacy .cfm site)
- [policeauctionscanada.com](https://www.policeauctionscanada.com/) — Cloudflare-confirmed
- [surplus.gov.ab.ca](https://surplus.gov.ab.ca/)
