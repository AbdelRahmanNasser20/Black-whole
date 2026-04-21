import asyncio
import re
from pathlib import Path
import httpx


def _ext_from_url(url: str) -> str:
    m = re.search(r"\.(jpe?g|png|webp)(?:\?|$)", url, re.I)
    return f".{m.group(1).lower()}" if m else ".jpg"


DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.govdeals.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


async def _fetch(client: httpx.AsyncClient, url: str, dest: Path) -> Path:
    r = await client.get(url, timeout=60.0, follow_redirects=True)
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest


async def download_all(urls: list[str], folder: Path, prefix: str) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(headers=DOWNLOAD_HEADERS) as client:
        tasks = []
        paths = []
        for i, u in enumerate(urls, start=1):
            dest = folder / f"{prefix}_{i}{_ext_from_url(u)}"
            paths.append(dest)
            tasks.append(_fetch(client, u, dest))
        results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = []
    for p, res in zip(paths, results):
        if isinstance(res, Exception):
            print(f"[download failed {p.name}: {res}]")
        else:
            ok.append(p)
    return ok
