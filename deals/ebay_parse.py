"""Parse eBay sold-search result HTML (2026 .s-card layout).

Canonical implementation — the Pi comps service deploys a copy of this file
(see scripts/deploy_pi_comps.sh). If eBay A/Bs back the legacy .s-item
layout, extend HERE and redeploy; the fixture test catches drift."""
import re
import statistics
from bs4 import BeautifulSoup

def parse_sold_page(body: str) -> dict:
    soup = BeautifulSoup(body, "html.parser")
    items = []
    for card in soup.select(".s-card[data-listingid]"):
        title_el = card.select_one(".s-card__title")
        price_el = card.select_one(".s-card__price")
        if not title_el or not price_el:
            continue
        title = title_el.get_text(" ", strip=True)
        if title.lower() in ("shop on ebay", ""):
            continue
        m = re.search(r"\$([\d,]+(?:\.\d{2})?)", price_el.get_text())
        if not m:
            continue
        link = card.select_one("a.s-card__link[href]")
        url = (link["href"].split("?")[0] if link else "")
        cond_el = card.select_one(".s-card__subtitle")
        cap_el = card.select_one(".s-card__caption")
        items.append({
            "listing_id": card.get("data-listingid"),
            "title": title,
            "price": float(m.group(1).replace(",", "")),
            "condition": cond_el.get_text(" ", strip=True) if cond_el else None,
            "sold_note": cap_el.get_text(" ", strip=True) if cap_el else None,
            "url": url,
        })
    prices = [i["price"] for i in items]
    return {
        "count": len(items),
        "median": round(statistics.median(prices), 2) if prices else None,
        "mean": round(statistics.mean(prices), 2) if prices else None,
        "items": items,
    }
