"""URL codec for the public auction-lot page.

    /deals/lot/{title-slug}-{st}-{source}-{key}

* `source` is a hyphen-free token (`TOKENS`), `key` is the lot's native key
  with `/` written as `.` and `-` as `~`, so neither part holds a hyphen and
  `rsplit('-', 2)` always finds them.
* Only source + key identify the lot. The text in front is decoration built
  from the title and the state; a request whose text differs from the
  canonical one is answered with a 301 (see `lot_page_routes`).
* No database column: the slug derives from keys that never change, so a URL
  that was published keeps resolving.

Every public link to an auction lot is built here (`build` / `for_row`).
Never hand-write `/deals/lot/...` or the old `/deals/{a}/{b}/{c}`.
"""
from __future__ import annotations

import re
import unicodedata

PREFIX = "/deals/lot/"
# our source key -> URL token (no hyphens, no underscores)
TOKENS: dict[str, str] = {
    "govdeals": "govdeals", "allsurplus": "allsurplus", "txauction": "txauction",
    "gsa": "gsa", "purple_wave": "purplewave", "public_surplus": "publicsurplus",
    "municibid": "municibid", "mibid": "mibid",
}
SOURCES: dict[str, str] = {v: k for k, v in TOKENS.items()}

SLUG_MAX = 60
_KEY_RX = re.compile(r"^[A-Za-z0-9/_:-]{1,80}$")        # a native key we can encode
_ENC_RX = re.compile(r"^[A-Za-z0-9._:~]{1,80}$")        # an encoded key in a URL
_STATE_RX = re.compile(r"^[a-z]{2}$")


def slugify(title: object) -> str:
    text = unicodedata.normalize("NFKD", str(title or "")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(text) > SLUG_MAX:
        text = text[:SLUG_MAX].rsplit("-", 1)[0] or text[:SLUG_MAX]
    return text.strip("-") or "lot"


def encode_key(key: object) -> str | None:
    key = str(key or "")
    if not _KEY_RX.match(key):
        return None
    return key.replace("/", ".").replace("-", "~")


def decode_key(enc: str) -> str | None:
    if not _ENC_RX.match(enc or ""):
        return None
    return enc.replace(".", "/").replace("~", "-")


def build(source: object, key: object, *, title: object = None, state: object = None) -> str | None:
    """The canonical path, or None when this lot cannot have a page."""
    token = TOKENS.get(str(source or ""))
    enc = encode_key(key)
    if not token or not enc:
        return None
    st = str(state or "").strip().lower()
    st = st if _STATE_RX.match(st) else "us"
    return f"{PREFIX}{slugify(title)}-{st}-{token}-{enc}"


def parse(tail: str) -> tuple[str, str] | None:
    """`{anything}-{source}-{key}` -> (source, native key), or None."""
    parts = (tail or "").rsplit("-", 2)
    if len(parts) < 2:
        return None
    source = SOURCES.get(parts[-2])
    key = decode_key(parts[-1])
    if not source or not key:
        return None
    return source, key


def key_of(row: dict) -> str | None:
    """A row's native key: the snapshot lot id, deal_lots.native_id, or the id trio."""
    if row.get("source_lot_id"):
        return str(row["source_lot_id"])
    if row.get("native_id"):
        return str(row["native_id"])
    if row.get("lot_key"):
        return str(row["lot_key"])
    if None not in (row.get("asset_id"), row.get("account_id"), row.get("auction_id")):
        return f"{row['asset_id']}/{row['account_id']}/{row['auction_id']}"
    return None


def for_row(row: dict) -> str | None:
    """Canonical path for any public lot row (deal_lots, snapshot or archive shape)."""
    return build(row.get("source") or row.get("site"), key_of(row),
                 title=row.get("title"), state=row.get("state"))
