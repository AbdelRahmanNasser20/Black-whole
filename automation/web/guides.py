"""Buyer guides — `/guides` and `/guides/{slug}` (AI SEO, 2026-10-09).

Why: the questions chair buyers actually type — on Reddit, into Google, into
ChatGPT — are not "banquet chairs Atlanta" but "where do I buy 500 banquet
chairs in bulk and what should I check first", "what stacking chair suits a
church fellowship hall", "banquet or folding chairs for a venue", "used
church chairs near me". A lot page comes and goes; a guide is the stable
URL that answers the question and then points at whatever is on the floor
today. Each guide carries FAQPage JSON-LD and is listed in the sitemap and
in /llms.txt.

Honesty rules (do not loosen):
- Copy is original, written here, and names no competitor's price.
- Every number about *our* stock (chairs, lots, cities, per-chair floor
  price) comes from inventory.list_public() / city_pages.listing() at
  request time, so the page can never claim chairs we do not hold.
- No reviews, testimonials or ratings — real or invented.
- `storage_note` is never read; pickup addresses never render.
"""

from __future__ import annotations

import html as _html_mod
import re
from typing import Any

from automation import inventory, lot_urls
from automation.web import city_pages, seo_copy

# Cities a guide may name by hand. The answer for each is built from live
# data: a city with no live lot gets the honest "nothing on the floor" line.
NAMED_CITIES: tuple[tuple[str, str], ...] = (
    ("Atlanta", "GA"), ("Phoenix", "AZ"), ("Boise", "ID"), ("Las Vegas", "NV"), ("Nashville", "TN"),
)

Section = tuple[str, list[str]]  # (heading, paragraphs)
QA = tuple[str, str]


def _guide(slug: str, *, title: str, h1: str, description: str, eyebrow: str, lede: str,
           sections: list[Section], faq: list[QA], related: list[str]) -> dict[str, Any]:
    return {
        "slug": slug, "path": f"/guides/{slug}", "title": title, "h1": h1,
        "description": description[:155], "eyebrow": eyebrow, "lede": lede,
        "sections": sections, "faq": faq, "related": related,
    }


GUIDES: list[dict[str, Any]] = [
    _guide(
        "where-to-buy-banquet-chairs-in-bulk",
        title="Where to Buy Banquet Chairs in Bulk (New, Used, Surplus) | Black Whole",
        h1="WHERE TO BUY BANQUET CHAIRS IN BULK",
        description="The four places to source 100–2,000 banquet chairs, what to check before you commit, and when a used matched lot beats a catalog order.",
        eyebrow="BUYER GUIDE · SOURCING",
        lede=("You need a few hundred banquet chairs, one finish, delivered by a date. There are four "
              "ways to get them, and the right one depends on your timeline, your budget and whether "
              "the room has to match a catalog photo or just has to seat people."),
        sections=[
            ("THE FOUR SOURCES", [
                "**Catalog retailers.** The big online seating stores sell new commercial banquet chairs "
                "in any quantity, usually drop-shipped from a factory or an importer's warehouse. You get a "
                "warranty, a spec sheet and a sample program. You also pay the full new price, and on a "
                "large order the lead time is set by a container, not by a truck.",
                "**Direct from the importer or factory.** Past a few hundred chairs it can be cheaper to "
                "buy the container yourself. The trade is cash up front, a 60–120 day wait, customs and "
                "duty paperwork, and no one to call if the foam is thinner than the sample.",
                "**Used matched lots.** Hotels, convention centers, universities and churches replace "
                "whole rooms of chairs at once. Those sets — one model, one finish, often lightly used — "
                "are resold by the lot by liquidators like us. The price is a fraction of catalog because "
                "the first owner already paid for the chair; the catch is that you buy what exists, in "
                "the city where it sits, as-is.",
                "**Surplus auctions.** Government and institutional surplus sites auction the same sets "
                "to the public. Prices can be lower still, but you are bidding blind on a count, "
                "arranging your own removal inside a short pickup window, and competing with resellers "
                "who do this every week. This is where we buy.",
            ]),
            ("WHAT TO CHECK BEFORE YOU COMMIT", [
                "**Hold one chair first.** A sample tells you frame gauge, foam density, stack height and "
                "whether the fabric is the colour in the photo. Any serious seller — new or used — will "
                "ship one or let you see one.",
                "**Matched set, not a matched description.** Ask whether every chair is the same model "
                "and dye lot. Two factory runs of \"burgundy\" do not match under ballroom lighting.",
                "**Stacking and storage.** How high do they stack, do they need a dolly, and does your "
                "storage room have the floor space for the stacks? A chair that stacks ten high needs "
                "a tenth of the floor of one that stacks four high.",
                "**Weight and freight.** Pallet count and total weight decide the freight bill. A "
                "quote should say how many pallets, how they are wrapped, and whether liftgate and "
                "inside delivery are included.",
                "**Lead time in writing.** For new chairs, ask whether stock is on the ground in the "
                "US or on a boat. For used lots, ask how fast the seller can palletise and when the "
                "truck can be booked.",
                "**Warranty versus as-is.** New chairs come with a warranty you may never use. Used "
                "lots are as-is; the substitute is photos of the actual chairs, a count you can "
                "verify at pickup, and a seller who answers the phone.",
            ]),
            ("WHEN USED WINS", [
                "A used matched lot is the right buy when the room has to be seated this month, when "
                "the budget is the constraint, or when the chairs are going into a fellowship hall, "
                "a rental fleet or a banquet room that will scuff them anyway. A new catalog order "
                "is the right buy when you need a specific finish to match an existing room, more "
                "than one lot can supply, or a warranty on paper for a board.",
                "Our lots are listed with the real count, the real city and the per-chair price. "
                "Pickup in that city is free; freight is quoted per ZIP on the lot page before you "
                "commit to anything.",
            ]),
        ],
        faq=[
            ("Where can I buy banquet chairs in bulk?",
             "Four places: online catalog retailers (new, warranty, full price), direct from an importer "
             "or factory (cheapest new, longest wait), used matched lots from a liquidator like Black Whole "
             "(one venue's set, sold by the lot at a fraction of new), and government or institutional "
             "surplus auctions (lowest price, you handle removal and bid blind)."),
            ("How many banquet chairs can I buy at once?",
             "From a catalog, as many as the factory can ship. From a used lot, the quantity is whatever "
             "that venue owned — our lots run from under a hundred to a few thousand chairs, and most "
             "split by the pallet for a local buyer."),
            ("What should I check before ordering 500 banquet chairs?",
             "Hold a sample; confirm every chair is one model and dye lot; check stack height and dolly "
             "needs against your storage; get the pallet count and weight behind the freight quote; get "
             "the lead time in writing; and know whether you are buying a warranty or buying as-is with photos."),
            ("Are used banquet chairs worth it?",
             "For a fellowship hall, a rental fleet or a venue on a budget, usually yes — commercial "
             "banquet chairs are built for decades of stacking, and a lightly used matched set from a "
             "hotel costs a fraction of the same chair new. If the room must match an existing finish "
             "exactly, buy new."),
        ],
        related=["best-stacking-chairs-for-a-church-fellowship-hall", "banquet-vs-folding-chairs-for-a-venue"],
    ),
    _guide(
        "best-stacking-chairs-for-a-church-fellowship-hall",
        title="Best Stacking Chairs for a Church Fellowship Hall | Black Whole",
        h1="STACKING CHAIRS FOR A CHURCH FELLOWSHIP HALL",
        description="Padded banquet, wire-frame stacker or plastic shell: which stacking chair fits a fellowship hall, how to size the room, and how to buy a used set.",
        eyebrow="BUYER GUIDE · CHURCHES",
        lede=("A fellowship hall is the hardest room in the building to seat: Sunday lunch at round "
              "tables, a funeral reception the next day, youth group on Wednesday, cleared to the walls "
              "for the rummage sale. The chair has to stack fast, survive volunteers and look fine "
              "under fluorescent light. Here is how the three common types compare."),
        sections=[
            ("THE THREE CHAIR TYPES", [
                "**Padded banquet (stacking) chairs.** The hotel ballroom chair: steel frame, padded "
                "seat and back, fabric or vinyl. Most comfortable for a two-hour dinner, the best "
                "look at a round table, and they stack eight to ten high on a dolly. They are the "
                "heaviest of the three and the fabric shows coffee and crayon. This is the chair "
                "most churches end up with for the hall.",
                "**Wire-frame and sled-base stackers.** The conference-center chair: a thin padded "
                "shell on a chrome or powder-coated rod frame. Lighter, stacks very high, often "
                "links in rows for a service or a concert. Less comfortable for a long dinner; "
                "excellent for a multi-use room that becomes a sanctuary overflow.",
                "**Plastic shell and resin folding chairs.** Lightest, cheapest, wipe clean, and "
                "the ones youth groups cannot break. They read as temporary at a wedding reception "
                "and they are the least comfortable past an hour.",
            ]),
            ("WHAT ACTUALLY MATTERS IN A HALL", [
                "**Stack height and dollies.** The chairs come out and go back every week. A chair "
                "that stacks ten high on a dolly is set up and struck by two people; one that stacks "
                "four high on the floor is a Saturday job for a crew.",
                "**Weight.** Volunteers in their seventies stack chairs too. Pick up one chair, then "
                "imagine the fiftieth.",
                "**Cleanable upholstery.** Vinyl or a tight commercial fabric wipes; loose weaves "
                "hold spaghetti sauce for years.",
                "**One matched set.** Mismatched donations are what the hall has now. A single "
                "model and finish, bought at once, is the whole reason to buy in bulk.",
                "**Ganging and linking.** If the hall doubles as overflow seating for services, "
                "chairs that clip together in rows keep the fire marshal happy.",
            ]),
            ("SIZING THE ROOM", [
                "Rule-of-thumb planning: round-table banquet seating needs roughly ten to twelve "
                "square feet per person including aisles; rows of chairs for a service need six to "
                "eight. Count the seats the hall holds at tables, add the overflow you want for "
                "rows, and buy that many plus a few percent for the ones that eventually break. "
                "Then check that the stacks fit in the storage room.",
            ]),
            ("BUYING A USED SET", [
                "Hotels and convention centers replace whole ballrooms of padded banquet chairs on a "
                "schedule, long before the chairs are worn out. Those matched sets are what we buy "
                "at surplus auctions and resell by the lot, with the real count and photos of the "
                "actual chairs. Pickup in the lot's city is free — a church van convoy is a normal "
                "Saturday for us — and freight is quoted per ZIP on each lot page. If the board or "
                "the pastor has to approve it first, say so; we hold lots while you get the vote.",
            ]),
        ],
        faq=[
            ("What is the best stacking chair for a church fellowship hall?",
             "For most halls, a padded steel-frame banquet chair: comfortable for a long meal, looks "
             "right at round tables, stacks eight to ten high on a dolly. Choose a wire-frame stacker "
             "if the room doubles as overflow seating in rows, and plastic shells only where cost and "
             "cleanability beat comfort."),
            ("How many chairs does a fellowship hall need?",
             "Count the seats at tables (about ten to twelve square feet per person including aisles), "
             "add the row seating you want for overflow (six to eight square feet per chair), and buy "
             "that total plus a few percent spare. Check the storage room fits the stacks before you buy."),
            ("Can a church buy used banquet chairs?",
             "Yes — matched sets from hotels and convention centers come up constantly, usually lightly "
             "used, and a church pays a fraction of the new catalog price. We list ours by the lot with the "
             "real count and city; pickup there is free and freight is quoted on the lot page."),
            ("Do banquet chairs link together for rows?",
             "Some do: many banquet and most wire-frame stackers have ganging clips or can take an "
             "add-on linking bracket. Ask before buying if the hall will be used for services or concerts "
             "— most fire codes require linked rows above a certain seat count."),
        ],
        related=["where-to-buy-banquet-chairs-in-bulk", "used-church-chairs-near-me"],
    ),
    _guide(
        "banquet-vs-folding-chairs-for-a-venue",
        title="Banquet Chairs vs Folding Chairs for a Venue | Black Whole",
        h1="BANQUET CHAIRS VS FOLDING CHAIRS FOR A VENUE",
        description="Comfort, look, storage, labor, lifespan and resale — how padded banquet chairs and folding chairs compare for an event venue, and when each one wins.",
        eyebrow="BUYER GUIDE · VENUES",
        lede=("Every new venue has the same argument: padded banquet chairs that look like a hotel, "
              "or folding chairs that fit in the closet. Both are right in the right room. This is "
              "the honest comparison, from people who have moved thousands of each."),
        sections=[
            ("HEAD TO HEAD", [
                "**Comfort.** Banquet chairs win. A padded seat and back carry a guest through a "
                "three-hour wedding reception; a folding chair carries them through a ceremony.",
                "**Look.** Banquet chairs photograph as a finished room with no cover. Folding "
                "chairs — metal or resin — read as temporary unless you spend on covers and sashes, "
                "which then become their own laundry problem.",
                "**Storage.** Folding chairs win, and it is not close. A cart of fifty folding "
                "chairs takes the floor space of one stack of ten banquet chairs. If storage is "
                "a closet, this decides it.",
                "**Labor.** Banquet chairs on dollies set up fast and strike fast; folding chairs "
                "are quick per chair but every one is handled twice. Over a season the difference "
                "is small; over a crew's back it is not.",
                "**Durability and lifespan.** Commercial banquet chairs are built for decades of "
                "stacking; frames outlast the fabric, and fabric can be recovered. Folding chairs "
                "bend at the hinge and are replaced, not repaired.",
                "**Resale.** A matched set of banquet chairs holds value — it is exactly what we "
                "buy from venues that close or refresh. Folding chairs resell for very little.",
            ]),
            ("WHEN FOLDING CHAIRS WIN", [
                "Outdoor ceremonies, lawns and tents; rooms with no real storage; venues that rent "
                "the space bare and let the planner bring seating; backup seating for the one "
                "event a year that exceeds the banquet count.",
            ]),
            ("WHEN BANQUET CHAIRS WIN", [
                "Any room where guests sit for a meal; any venue that sells its own photos; any "
                "operation with a storage room and a dolly. Most full-service venues run a core "
                "set of banquet chairs and a smaller reserve of folders for overflow and outdoor use.",
            ]),
            ("THE COST QUESTION", [
                "New, a commercial padded banquet chair costs several times a folding chair. Used, "
                "the gap nearly closes: hotels replace matched banquet sets long before they wear "
                "out, and those lots sell for a fraction of catalog. Our lot pages show the live "
                "per-chair price and the count on the floor, so you can price a room without a "
                "sales call. Pickup in the lot's city is free; the freight form on each lot prices "
                "your ZIP.",
            ]),
        ],
        faq=[
            ("Are banquet chairs or folding chairs better for a venue?",
             "For seated meals, indoor receptions and venues that sell their own look, padded banquet "
             "chairs. For outdoor ceremonies, tight storage or overflow, folding chairs. Most full-service "
             "venues run a core banquet set plus a smaller reserve of folders."),
            ("How much storage do banquet chairs need?",
             "Far more than folding chairs. Banquet chairs stack eight to ten high on a dolly; a stack "
             "takes about the floor area of one chair. Folding chairs on a cart store roughly five times "
             "as many in the same footprint. Measure the storage room before choosing."),
            ("Do banquet chairs last longer than folding chairs?",
             "Yes. Commercial banquet chair frames last decades and can be reupholstered; folding chairs "
             "fail at the hinge and are replaced. That is also why used banquet sets hold resale value."),
            ("Is it cheaper to buy used banquet chairs than new folding chairs?",
             "Often close. A used matched banquet set from a hotel costs a fraction of the same chair new, "
             "which can bring it near the price of new commercial folding chairs. Compare against the live "
             "per-chair price on a lot page rather than a catalog estimate."),
        ],
        related=["where-to-buy-banquet-chairs-in-bulk", "best-stacking-chairs-for-a-church-fellowship-hall"],
    ),
    _guide(
        "used-church-chairs-near-me",
        title="Used Church Chairs Near Me — Atlanta, Phoenix, Boise & More | Black Whole",
        h1="USED CHURCH CHAIRS NEAR YOU",
        description="Where used church and banquet chairs are on the floor right now — by city, with counts and per-chair prices from live inventory — and how pickup works.",
        eyebrow="BUYER GUIDE · BY CITY",
        lede=("\"Used church chairs near me\" usually means: a matched set, within a drive, that a few "
              "vans can collect on a Saturday. The list below is built from our live inventory, so a "
              "city only appears while chairs are actually staged there."),
        sections=[
            ("HOW PICKUP WORKS", [
                "Every lot sits in the city on its page — a warehouse or a storage unit, not a showroom. "
                "Pickup there is free: bring a box truck, a trailer or a few vans, and we load with you. "
                "The exact address comes with your confirmation, never on the public page. Most lots "
                "split by the pallet for a local buyer, so a small church does not have to take a "
                "hotel's whole ballroom.",
            ]),
            ("IF YOUR CITY IS NOT ON THE LIST", [
                "We palletise, shrink-wrap and ship LTL or by dedicated truck anywhere in the "
                "continental US; every lot page has a freight form that prices your ZIP before you "
                "commit. The map shows every lot with its distance from you, and the alert list at "
                "the bottom of the inventory page hears about a new lot in your region before it is "
                "listed.",
            ]),
            ("WHAT TO CHECK ON A USED LOT", [
                "Photos of the actual chairs, not a catalog image. A count you can verify at pickup. "
                "One model and finish across the set. Fabric condition on a few chairs pulled from "
                "the middle of a stack, not the top. Whether the lot includes dollies. Our lot pages "
                "answer all of these up front; anything they do not, the contact form does.",
            ]),
        ],
        faq=[],  # built live in page(): one Q per NAMED_CITIES entry + the general ones
        related=["best-stacking-chairs-for-a-church-fellowship-hall", "where-to-buy-banquet-chairs-in-bulk"],
    ),
]

_BY_SLUG = {g["slug"]: g for g in GUIDES}
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _para_html(text: str) -> str:
    """Escape a hand-written paragraph and turn **lead-ins** into <strong>.
    Only copy from this module ever passes through here — never a DB field."""
    return _BOLD.sub(r"<strong>\1</strong>", _html_mod.escape(text, quote=False))


def listing() -> list[dict[str, Any]]:
    """Index-page records, in the order above."""
    return [{k: g[k] for k in ("slug", "path", "title", "h1", "description", "eyebrow")} for g in GUIDES]


def _live() -> dict[str, Any]:
    """Live stock facts for the dynamic blocks. Every number here is the
    ledger's; nothing is typed by hand."""
    cities = city_pages.listing(indexable_only=True)
    rows = inventory.list_public()
    prices = [float(r["price_per_chair"]) for r in rows
              if r.get("price_per_chair") and float(r["price_per_chair"]) > 0]
    return {
        "cities": cities,
        "lots": len(rows),
        "chairs": sum(int(r.get("quantity_remaining") or 0) for r in rows),
        "min_price": min(prices) if prices else None,
        "by_slug": {c["slug"]: c for c in cities},
    }


def _city_answer(city: str, state: str, live: dict[str, Any]) -> str:
    rec = live["by_slug"].get(lot_urls.city_slug(city, state))
    label = f"{city}, {state}"
    if rec and rec["chairs"]:
        n = len(rec["live"])
        price = f" from ${rec['min_price']:,.0f} per chair" if rec.get("min_price") else ""
        return (f"Yes — {rec['chairs']:,} chairs across {n} lot{'s' if n != 1 else ''} are on the floor in "
                f"{label} right now{price}. Pickup there is free; see the {label} page for every lot.")
    return (f"Nothing is staged in {label} at the moment. The map shows the closest lots and their "
            f"distance, we ship LTL anywhere in the continental US, and the alert list hears about "
            f"the next {city} lot before it is listed.")


def _city_faq(live: dict[str, Any]) -> list[QA]:
    items: list[QA] = [(f"Are there used church chairs for sale near {city}?", _city_answer(city, state, live))
                       for city, state in NAMED_CITIES]
    items += [
        ("How do I pick up a lot of used chairs?",
         "Pickup in the lot's city is free. Bring a box truck, trailer or a few vans and we load with you; "
         "the address comes with your confirmation. Most lots split by the pallet so you can take part of a set."),
        ("Can you deliver used church chairs to my city?",
         "Yes. We palletise and shrink-wrap the chairs and ship LTL or by dedicated truck anywhere in the "
         "continental US. The freight form on each lot page prices your ZIP before you commit."),
    ]
    return items


def page(slug: str) -> dict[str, Any] | None:
    """Everything guide.html needs, or None for an unknown slug."""
    g = _BY_SLUG.get(slug)
    if g is None:
        return None
    try:
        live = _live()
    except Exception:  # noqa: BLE001 — a guide must render without the DB
        live = {"cities": [], "lots": 0, "chairs": 0, "min_price": None, "by_slug": {}}
    out = dict(g)
    out["live"] = live
    out["sections"] = [(h, [_para_html(p) for p in paras]) for h, paras in g["sections"]]
    faq = list(g["faq"]) or _city_faq(live)
    out["faq"] = faq
    out["faq_jsonld"] = seo_copy.faq_jsonld(faq)
    out["related"] = [_BY_SLUG[s] for s in g["related"] if s in _BY_SLUG]
    out["city_list"] = [
        {"label": c["label"], "path": c["path"], "chairs": c["chairs"],
         "lots": len(c["live"]), "min_price": c.get("min_price")}
        for c in live["cities"]
    ]
    return out
