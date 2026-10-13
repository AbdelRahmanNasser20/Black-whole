"""Illinois iBid (`ibid.illinois.gov`) source adapter — server-rendered PHP.

LIVE RECON (verified 2026-10-09):

- No JSON anywhere; no bot wall; `robots.txt` disallows only /admin/, /js/,
  /images/, /themes/, /uploaded/ and friends — `browse.php` and `item.php`
  are allowed. Site runs "AssetWorks iBid v183" (footer).
- Listing: `GET /browse.php?id=0` (id=0 = all categories; `id=521` =
  Furniture, `id=611` = Office Equipment and Supplies). ~86 active lots
  site-wide at probe time, 1 in Furniture. Default page size is 25 (4 pages);
  the page-size preference is a plain cookie, `ibid_il_paging_pref=200`,
  which collapses the whole site into ONE page (verified: "Page 1 of 1",
  86 rows). We send that cookie on every browse request.
- Browse row (one `<tr>` per lot, 5 cells): thumbnail link → `item.php?id=N`,
  "N - <a>title</a>", "$ <strong>1,234.00</strong>", bid count, and an
  "Ends in" cell that is RELATIVE ("5h 16m", "1d 7h 16m") — or the word
  `closed` once a lot has ended (per the probe brief; none were showing at
  capture time, so `sold_sweep()` is coded defensively against that cell).
  Both page header and footer carry the server clock:
  `Current system time: Oct 9, 2026</span> <span class="servertime">11:43:55`
  — that clock is US/Central (11:43 CDT while UTC was 16:43), so
  `end_date = server_now + relative` converted from America/Chicago to UTC.
  Minute precision; good enough for the scheduler's cadence buckets.
- Detail: `GET /item.php?id=N` — category breadcrumb (`browse.php?id=521`
  → "Furniture"), title, "Auction ID: N", seller, seller location (street,
  city, ST zip, …), highest bidder, "Auction Ends In: <span class=counter>5
  hours 15 minutes</span>" or `<span class="errfont">closed</span>` plus the
  literal line "This auction is closed", "# of bids: 7", "Current Bid:
  $ <strong>202.50</strong>", picture gallery (S3 links), and the HTML
  description. A closed lot keeps its final bid + bid count on the page
  (`item.php?id=418000`, Furniture, "STOOLS (6)", 7 bids, closed) — so
  `poll()` reads a real final, `capture_method` = `api_final`-grade.
- An unknown / purged id answered HTTP 502 (`item.php?id=417900`), not 404.
  A 502 is therefore ambiguous (purged OR the host hiccupping) and is treated
  as a fetch failure (no observation, retried next run), never as `gone`.
  Only a clean 404 past `end_date` is `gone`.
- Scope: iBid's own category tree is the primary furniture signal (every lot
  in `Furniture` is kept — "STOOLS (6)" would never match `FURNITURE_TERMS`),
  plus any lot site-wide whose browse title matches `FURNITURE_TERMS`. The
  detail page is fetched for those candidates only (a handful per run, 1
  req/s), and the `FURNITURE_TERMS` check is re-run on title + description
  for the title-matched ones. So discover() costs 2 browse pages + N detail
  pages, N = furniture candidates.
- IDs: `source_lot_id` = the numeric iBid auction id as a string (`"418484"`).
- Money: USD. Thousands separators stripped.

raw keys (stable, read by the web layer): `title`, `city`, `state`,
`category`, `image_url`, `url`, plus everything parsed from the page under
`ibid` (bid, bids, ends_text, seller, location, images, description_text,
server_time) and the browse row under `browse_row` when present.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import unescape
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from recorder.models import Observation
from recorder.sources.base import FURNITURE_TERMS, PollBudget, SourceFetchFailed, polite_get

SOURCE = "ibid_il"

BASE_URL = "https://ibid.illinois.gov"
BROWSE_URL = f"{BASE_URL}/browse.php"
ITEM_URL = f"{BASE_URL}/item.php"
ALL_CATEGORY_ID = "0"
FURNITURE_CATEGORY_ID = "521"
FURNITURE_CATEGORY_NAME = "Furniture"
PAGING_COOKIE = "ibid_il_paging_pref=200"
SITE_TZ = ZoneInfo("America/Chicago")
DESCRIPTION_MAX_CHARS = 2000

_ITEM_ID_RE = re.compile(r"item\.php\?id=(\d+)")
_MONEY_RE = re.compile(r"\$\s*<strong>([\d,]+\.?\d*)</strong>")
_SERVER_TIME_RE = re.compile(
    r"Current system time:\s*([A-Za-z]{3} \d{1,2}, \d{4})</span>\s*<span class=\"servertime\">(\d{1,2}:\d{2}:\d{2})"
)
_REL_RE = re.compile(r"(\d+)\s*(d|day|days|h|hour|hours|m|min|minute|minutes)\b", re.I)
_CITY_STATE_RE = re.compile(r",\s*([A-Za-z .'\-]+?),\s*([A-Z]{2})\s+\d{5}")


def _money(text: str | None) -> Decimal | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.]", "", text)
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _int(text: str | None) -> int | None:
    if text is None:
        return None
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


def _server_time(html: str) -> datetime | None:
    """The page's own clock (US/Central) as an aware UTC datetime."""
    m = _SERVER_TIME_RE.search(html)
    if not m:
        return None
    try:
        local = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%b %d, %Y %H:%M:%S")
    except ValueError:
        return None
    return local.replace(tzinfo=SITE_TZ).astimezone(timezone.utc)


def _relative(text: str | None) -> tuple[timedelta | None, bool]:
    """'1d 7h 16m' / '5 hours 15 minutes' → (timedelta, closed=False);
    'closed' → (None, True); unparseable → (None, False)."""
    if not text:
        return None, False
    t = text.strip().lower()
    if "closed" in t:
        return None, True
    total = timedelta()
    found = False
    for num, unit in _REL_RE.findall(t):
        found = True
        n = int(num)
        if unit.startswith("d"):
            total += timedelta(days=n)
        elif unit.startswith("h"):
            total += timedelta(hours=n)
        else:
            total += timedelta(minutes=n)
    return (total if found else None), False


_ABS_END_RE = re.compile(r"\(([A-Za-z]{3} \d{1,2}, \d{4})\s*-\s*(\d{1,2}:\d{2} [AP]M)\)")


def _absolute_end(text: str | None) -> datetime | None:
    """'1 day, 7 hours (Oct 10, 2026 - 07:28 PM)' → aware UTC datetime."""
    if not text:
        return None
    m = _ABS_END_RE.search(text)
    if not m:
        return None
    try:
        local = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%b %d, %Y %I:%M %p")
    except ValueError:
        return None
    return local.replace(tzinfo=SITE_TZ).astimezone(timezone.utc)


def _end_date(server_now: datetime | None, rel: timedelta | None) -> datetime | None:
    if server_now is None or rel is None:
        return None
    return server_now + rel


def _city_state(location: str | None) -> tuple[str | None, str | None]:
    if not location:
        return None, None
    m = _CITY_STATE_RE.search(location)
    if not m:
        return None, None
    return m.group(1).strip(), m.group(2)


def _is_furniture_text(text: str) -> bool:
    t = (text or "").lower()
    return any(term in t for term in FURNITURE_TERMS)


# --- browse page ---------------------------------------------------------

def parse_browse(html: str) -> tuple[list[dict], datetime | None]:
    """Rows of the browse table + the page's server clock (UTC)."""
    soup = BeautifulSoup(html, "html.parser")
    server_now = _server_time(html)
    rows: list[dict] = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) != 5:
            continue
        link = tds[1].find("a", href=_ITEM_ID_RE)
        if link is None:
            continue
        lot_id = _ITEM_ID_RE.search(link["href"]).group(1)
        img = tds[0].find("img")
        bid_html = str(tds[2])
        m_bid = _MONEY_RE.search(bid_html)
        ends_text = tds[4].get_text(" ", strip=True)
        rel, closed = _relative(ends_text)
        rows.append({
            "lot_id": lot_id,
            "title": unescape(link.get_text(" ", strip=True)),
            "url": f"{ITEM_URL}?id={lot_id}",
            "thumb": img["src"] if img and img.has_attr("src") else None,
            "current_bid": _money(m_bid.group(1)) if m_bid else None,
            "bid_count": _int(tds[3].get_text(" ", strip=True)),
            "ends_text": ends_text,
            "closed": closed,
            "end_date": _end_date(server_now, rel),
        })
    return rows, server_now


def _fetch_browse(category_id: str) -> str | None:
    """None on ANY fetch failure (network, non-200, no table); the HTML
    otherwise. Callers decide whether that failure is fatal."""
    try:
        resp = polite_get(BROWSE_URL, params={"id": category_id},
                          headers={"Cookie": PAGING_COOKIE})
    except requests.exceptions.RequestException as e:
        print(f"[{SOURCE}] RECORDER ERROR: browse id={category_id} request failed: {e}")
        return None
    if resp.status_code != 200:
        print(f"[{SOURCE}] RECORDER ERROR: browse id={category_id} HTTP {resp.status_code}")
        return None
    if "item.php?id=" not in resp.text and "Bids #" not in resp.text:
        print(f"[{SOURCE}] RECORDER ERROR: browse id={category_id} — unrecognised page shape")
        return None
    return resp.text


# --- item page -----------------------------------------------------------

def parse_item(html: str) -> dict | None:
    """Everything the detail page says about one lot, or None when the page
    is not a lot page (no 'Auction ID')."""
    soup = BeautifulSoup(html, "html.parser")
    text_all = soup.get_text(" ", strip=True)
    m_id = re.search(r"Auction ID:\s*(\d+)", text_all)
    if not m_id:
        return None
    lot_id = m_id.group(1)
    server_now = _server_time(html)

    title = None
    box = soup.find("div", class_="titTable2")
    if box:
        left = box.find("div", class_="lefttext")
        if left:
            title = unescape(left.get_text(" ", strip=True))

    category = None
    crumbs = [a for a in soup.find_all("a", href=re.compile(r"browse\.php\?id=\d+"))
              if "Item Category" in (a.parent.get_text(" ", strip=True) if a.parent else "")]
    if crumbs:
        category = crumbs[-1].get_text(" ", strip=True)

    def _field(label: str) -> str | None:
        lab = soup.find("div", class_="itemdetlabel", string=re.compile(re.escape(label)))
        if not lab:
            return None
        val = lab.find_next_sibling("div", class_="itemdet")
        return val.get_text(" ", strip=True) if val else None

    seller = _field("Seller:")
    location = _field("Seller location:")
    highest_bidder = _field("Highest bidder:")
    ends_text = _field("Auction Ends In:")
    counter = soup.find("span", class_="ending_counter")
    if counter:
        ends_text = counter.get_text(" ", strip=True)
    rel, closed = _relative(ends_text)
    if "This auction is closed" in text_all:
        closed = True
    # The detail counter also prints the absolute close in parentheses —
    # "1 day, 7 hours (Oct 10, 2026 - 07:28 PM)" — which beats the
    # hour-truncated relative part. Site-local (America/Chicago).
    absolute = _absolute_end(ends_text)
    bids_div = soup.find("div", id="bids")
    bid_count = _int(bids_div.get_text(" ", strip=True)) if bids_div else None
    cur = soup.find("div", id="currentBid")
    current_bid = _money(cur.get_text(" ", strip=True)) if cur else None

    images = [a["href"] for a in soup.select("div#lightbox a.gallery[href]")]
    if not images:
        hero = soup.select_one("div.itemdetimage img[src]")
        if hero:
            images = [hero["src"]]

    description_text = None
    anchor = soup.find("a", attrs={"name": "description"})
    if anchor:
        block = anchor.find_parent("div", class_="titTable4")
        body = block.find_next_sibling("div", class_="table2") if block else None
        if body:
            description_text = body.get_text(" ", strip=True)[:DESCRIPTION_MAX_CHARS]

    city, state = _city_state(location)
    return {
        "lot_id": lot_id,
        "title": title,
        "category": category,
        "seller": seller,
        "location": location,
        "city": city,
        "state": state,
        "highest_bidder": highest_bidder,
        "ends_text": ends_text,
        "closed": closed,
        "end_date": absolute or _end_date(server_now, rel),
        "bid_count": bid_count,
        "current_bid": current_bid,
        "images": images,
        "description_text": description_text,
        "server_time": server_now.isoformat() if server_now else None,
        "url": f"{ITEM_URL}?id={lot_id}",
    }


def _fetch_item(lot_id: str) -> tuple[int | None, dict | None]:
    """(http_status, parsed) — status None on a transport error; parsed None
    on non-200 or an unrecognised page."""
    try:
        resp = polite_get(ITEM_URL, params={"id": lot_id})
    except requests.exceptions.RequestException as e:
        print(f"[{SOURCE}] RECORDER ERROR: item {lot_id} request failed: {e}")
        return None, None
    if resp.status_code != 200:
        return resp.status_code, None
    parsed = parse_item(resp.text)
    if parsed is None:
        print(f"[{SOURCE}] RECORDER ERROR: item {lot_id} — unrecognised page shape")
    return 200, parsed


def _to_observation(item: dict, browse_row: dict | None = None) -> Observation:
    raw = {
        "title": item.get("title"),
        "city": item.get("city"),
        "state": item.get("state") or "IL",
        "category": item.get("category"),
        "image_url": (item.get("images") or [None])[0] or (browse_row or {}).get("thumb"),
        "url": item["url"],
        "currency": "USD",
        "ibid": item,
    }
    if browse_row:
        raw["browse_row"] = browse_row
    end_date = item.get("end_date")
    if end_date is None and browse_row:
        end_date = browse_row.get("end_date")
    return Observation(
        source=SOURCE,
        source_lot_id=str(item["lot_id"]),
        status="closed" if item.get("closed") else "active",
        raw=_jsonable(raw),
        current_bid=item.get("current_bid"),
        bid_count=item.get("bid_count"),
        end_date=end_date,
    )


def _jsonable(obj):
    """raw goes to a JSON column — Decimals/datetimes become strings."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


class IBidIllinoisSource:
    SOURCE = SOURCE

    def _candidates(self) -> tuple[dict[str, dict], dict[str, dict] | None]:
        """(furniture-category rows by id, all-site rows by id or None if the
        all-site page failed)."""
        all_html = _fetch_browse(ALL_CATEGORY_ID)
        if all_html is None:
            raise SourceFetchFailed("discover() aborted — browse.php?id=0 fetch failed", url=BROWSE_URL)
        all_rows, _ = parse_browse(all_html)
        furn_html = _fetch_browse(FURNITURE_CATEGORY_ID)
        furn_rows = parse_browse(furn_html)[0] if furn_html is not None else []
        if furn_html is None:
            print(f"[{SOURCE}] WARNING: Furniture category page failed — title matches only this run")
        return ({r["lot_id"]: r for r in furn_rows}, {r["lot_id"]: r for r in all_rows})

    def discover(self) -> list[Observation]:
        furn_by_id, all_by_id = self._candidates()
        wanted: dict[str, dict] = dict(furn_by_id)
        for lid, row in all_by_id.items():
            if lid not in wanted and _is_furniture_text(row["title"]):
                wanted[lid] = row
        out: list[Observation] = []
        for lid, row in wanted.items():
            if row.get("closed"):
                continue
            status, item = _fetch_item(lid)
            if item is None:
                print(f"[{SOURCE}] WARNING: skipping lot {lid} — detail fetch failed (HTTP {status})")
                continue
            in_category = (item.get("category") == FURNITURE_CATEGORY_NAME) or lid in furn_by_id
            if not in_category and not _is_furniture_text(
                f"{item.get('title') or ''} {item.get('description_text') or ''}"
            ):
                continue
            if item.get("closed"):
                continue
            out.append(_to_observation(item, row))
        if not out:
            print(
                f"[{SOURCE}] WARNING: discover() found 0 furniture lots out of "
                f"{len(all_by_id)} active site-wide — check FURNITURE_TERMS / category id for drift"
            )
        return out

    def poll(self, lots: list[dict]) -> list[Observation]:
        if not lots:
            return []
        now = datetime.now(timezone.utc)
        budget = PollBudget()
        observations: list[Observation] = []
        for lot in lots:
            if budget.exhausted:
                print(f"[{SOURCE}] RECORDER ERROR: poll() aborted after "
                      f"{budget.consecutive} consecutive failures — {budget.stats(len(lots))}")
                break
            lot_id = str(lot["source_lot_id"])
            status, item = _fetch_item(lot_id)
            if item is not None:
                budget.record(True)
                observations.append(_to_observation(item))
                continue
            if status == 404:
                budget.record(True)
                end_date = lot.get("end_date")
                if end_date is not None and end_date <= now:
                    observations.append(Observation(
                        source=SOURCE, source_lot_id=lot_id, status="gone",
                        raw={"recorder_probe": {"result": "not_found", "http_status": 404,
                                                "url": f"{ITEM_URL}?id={lot_id}"}},
                    ))
                continue
            # transport error, 5xx (502 = purged OR hiccup — ambiguous), bad shape
            budget.record(False)
        return observations

    def sold_sweep(self) -> list[Observation]:
        """Browse rows whose 'Ends in' cell reads `closed` (the site shows a
        just-closed lot there with its final $ and bid count). Zero rows is
        normal — most closes are captured by `poll()` instead."""
        out: list[Observation] = []
        seen: set[str] = set()
        for category_id in (FURNITURE_CATEGORY_ID, ALL_CATEGORY_ID):
            html = _fetch_browse(category_id)
            if html is None:
                continue
            rows, _ = parse_browse(html)
            for row in rows:
                lid = row["lot_id"]
                if lid in seen or not row.get("closed"):
                    continue
                if category_id == ALL_CATEGORY_ID and not _is_furniture_text(row["title"]):
                    continue
                seen.add(lid)
                status, item = _fetch_item(lid)
                if item is None:
                    continue
                item["closed"] = True
                out.append(_to_observation(item, row))
        return out
