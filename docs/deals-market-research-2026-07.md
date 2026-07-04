# Deals tracker — category market research (2026-07-03)

Why these categories are in `deals/cli.py DEFAULT_CATEGORIES` and how to comp
lots in each. Supply numbers come from a 14,400-lot sample of the GovDeals
maestro firehose (120 pages, soonest-ending, 2026-07-03); demand/resale claims
come from flipper-community and reseller-guide research (sources at bottom).

## The structural edge

Items sell cheap on GovDeals because pickup is local, as-is, and few bidders
can transport / repair / evaluate. Our edge (truck + storage in GA/LA/IL/AZ +
zero-bid sweep) maps onto categories where the discount is **transport-gated**
(kitchen, fitness, mowers — same as chairs) or **knowledge-gated** (radios,
test gear, band instruments).

FB Marketplace conversion sweet spot: $100–300 items convert best; price bulky
goods ~18–22% under retail-equivalent to sell 3× faster.

## Tracked categories (code → canonical bucket)

| Bucket | Codes | Sampled lots (0-bid) | Buy → resale | Channel | Velocity |
|---|---|---|---|---|---|
| `tools_shop` | 90, 249, 375, 28I, 153, 159 (+95 welding, map-only) | ~260 (~130) | pallet lots near no-bid → working Milwaukee/DeWalt hold 40–70% of retail | FB local; eBay for premium | **Fastest** — impact drivers sell in days |
| `kitchen_restaurant` | 287, 21, 632, 631, 630, 25U | ~290 (~190) | 30–50% below dealer at auction → Hobart 20qt $1,000–2,500 used | FB/CL local; countertop ships | Medium; Hobart/Vulcan/True fast |
| `computers_electronics` (sweep: laptops/desktops/tablets/parts/monitors) | 219, 217, 218, 29, 291, 220 | ~790 (~330) | $10–50/unit bulk → $150–380/unit eBay (25–38% net) | eBay | High but saturated post-Win10-EOL |
| `comms_radios` | 28, 28S | ~80 (~40) | scrap-adjacent lots → Motorola APX $300–1,500/unit | eBay | Medium, persistent (vol. FDs, security, hams) |
| `lab_test_equipment` | 57, 57M (map also 326, 57I, 57D, 575) | ~150 (~95) | often no-bid → legacy Keysight/Tek constant eBay demand | eBay | Slow-medium, very high margin |
| `medical_equipment` | 67, 301 | ~150 (~95) | county-health surplus cheap → exam tables, wheelchairs, stainless | FB local + eBay | Medium; Class I ONLY |
| `fitness_equipment` | 147, 208 | ~120 (~50) | full weight rooms go cheap → commercial-grade retains 25–45% at 5–10 yr | FB local | Medium; spikes January |
| `musical_instruments` | 70 | ~31 (~8) | $10–50/unit district lots → $100–400/horn (Yamaha/Bach/Selmer/Bundy) | eBay/Reverb | Medium; spikes Jul–Sep |
| `lawn_landscaping` | 71, 373 (map also 40) | ~91 (~40) | municipal ZTRs $500–2,500 running → +$300–900/flip (documented $50k/yr niche) | FB local | High in season; GA/LA/AZ extend it |

Pre-existing: `seating_furniture` (372, 47B, 47C, 47A, 46, 47D, 28E),
`general_merchandise` (266, LLM-scanned), `av_equipment` (22).

## Comp-matching checklist (apply before trusting any comp)

1. **Exact model string, not category.** Life Fitness 95T ≠ "commercial
   treadmill"; APX 6000 VHF ≠ APX 6000 7/800MHz; DeWalt 20V ≠ 60V FlexVolt.
2. **Completeness delta.** Batteries+charger = +30–50% on tools/laptops;
   probes/cables on test gear; remotes on projectors. Comp bare vs kit
   separately.
3. **Meter readings.** Engine hours (mowers/generators), lamp hours
   (projectors), page counts. Municipal gear is high-meter — comp same-meter
   sold listings, not category averages.
4. **As-is against as-is.** Untested typically clears 40–60% of
   tested-working price. Never comp an untested lot against a tested listing.
5. **Calibration / certification.** Current cal cert ≈ 2× on test equipment;
   NSF sticker matters on kitchen.
6. **Power requirements.** 3-phase kills the FB-homeowner market (kitchen,
   shop, some commercial cardio). Check the data plate photo before bidding.
7. **Institutional locks.** School Chromebook MDM enrollment ≈ worthless;
   BIOS passwords; radio inhibit/flashcodes; Cisco RTU licenses don't
   transfer.
8. **Win11 line.** Intel 8th gen splits laptop lots into two comp pools;
   7th-gen and older = parts pricing only.

## Deliberately NOT tracked

- **Printers/copiers (222)** — negative-value e-waste; toner exceeds unit value.
- **Networking (221)** — Cisco license non-transfer + fast depreciation; ITAD
  wholesalers own it.
- **Generic office furniture (47A desks kept only for the seating sweep)** —
  auction price ≈ resale price ≈ pennies. Exception is a SKU hunt: Herman
  Miller Aeron / Steelcase Leap in mixed lots.
- **Bicycles (143)** — police auctions professionally run now; thin bargains,
  low $/hour.
- **Residential-grade treadmills/ellipticals** — free on FB constantly (code
  147 includes them; filter at comp level for commercial brands).
- **CRT/XGA-era AV, interactive whiteboards** — the classic school-surplus
  trap inside code 22 lots.
- **Class II+ medical (CPAP, Rx devices)** — marketplace-prohibited.
- **Heavy construction / titled trailers** — margins exist but wrong shape for
  solo+velocity; title paperwork. Revisit trailers (94I, 65x) when ready.

## Platform mechanics worth remembering

- Buyer premium 7.5–12.5% per listing (`DEALS_BUYER_PREMIUM_PCT` default
  12.5%); pickup window ~10 business days, weekday business hours.
- GovDeals ~$903M FY2025 volume — supply growing.
- PublicSurplus is school-district heavy (band instruments, classroom AV,
  laptops) with fewer bidders — highest-priority future adapter.

## Sources

- govauctions.app guides (GovDeals FAQ, complete guide, pre-bid checklist)
- resellingrevealed.com — PublicSurplus flipping (28 mics $31 → ~$2,200)
- sidehustlenation.com — mower flipping ($50k/yr, $300 min-profit rule)
- underpriced.app — power tools / bikes / cameras / instruments flip guides
- shelftrend.com — refurbished-laptop margin analysis (25–38% net)
- totalfitnessoutlet.com, garagegymreviews.com — used-fitness value retention
- ebay.com APX category, used-radios.com — radio resale ranges
- pciauctions.com — Hobart secondary pricing
- sondercare.com, medmartonline.com — medical resale legality (Class I vs II)
- accio.com, smartbuy.alibaba.com — FB Marketplace conversion data
