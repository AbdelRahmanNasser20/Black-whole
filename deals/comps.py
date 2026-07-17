# deals/comps.py
"""Client for the Pi sold-comps microservice (scripts/pi_comps_service.py).

Pluggable like automation/llm: comps_provider_from_env() returns None when
unconfigured and callers degrade to llm_estimate verdicts — comps failures
must never block the analyze pass."""
import os
from dataclasses import dataclass
import httpx

@dataclass
class Comp:
    listing_id: str
    title: str
    price: float
    condition: str | None
    url: str

@dataclass
class CompsResult:
    query: str
    count: int
    median: float | None
    items: list[Comp]
    cached: bool

class CompsUnavailable(Exception):
    pass

class PiCompsProvider:
    def __init__(self, base_url: str, key: str, timeout: float = 90.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self._key = key

    def fetch(self, query: str) -> CompsResult:
        try:
            r = self._client.get("/comps", params={"q": query},
                                 headers={"X-Comps-Key": self._key})
        except httpx.HTTPError as e:
            raise CompsUnavailable(f"comps request failed: {e}") from e
        if r.status_code != 200:
            raise CompsUnavailable(f"comps status {r.status_code}: {r.text[:200]}")
        d = r.json()
        items = [Comp(i.get("listing_id") or "", i["title"], float(i["price"]),
                      i.get("condition"), i.get("url") or "") for i in d.get("items", [])]
        return CompsResult(d.get("query", query), d.get("count", len(items)),
                           d.get("median"), items, bool(d.get("cached")))

def comps_provider_from_env(env: dict | None = None) -> PiCompsProvider | None:
    env = env if env is not None else os.environ
    url, key = env.get("COMPS_URL"), env.get("COMPS_KEY")
    if not url or not key:
        return None
    return PiCompsProvider(url, key)
