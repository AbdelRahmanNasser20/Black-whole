"""Two front doors, one app: the chair storefront on PUBLIC_BASE_URL and the
liquidator platform on PLATFORM_HOST.

Off (PLATFORM_HOST unset): nothing happens, every URL serves as before.
On:
* platform host, "/"            → serves the /platform landing (path rewrite, no redirect)
* platform host, platform path  → serves
* platform host, shared path    → serves (static, contact, terms, robots, health …)
* platform host, anything else  → 301 to the same path on the chair host
* chair host, platform path     → 301 to the same path on the platform host
* "/robots.txt" and "/sitemap.xml" on the platform host rewrite to the platform
  versions (/platform/robots.txt, /sitemap-index.xml), so each host tells crawlers
  about its own pages only.
A localhost / 127.0.0.1 / testclient host is never redirected: a dev box with
PLATFORM_HOST set still reaches every route.

Pure ASGI so it runs before every http middleware (the scrape guard keys on
the final path). Query strings survive a redirect untouched."""
from __future__ import annotations

from urllib.parse import quote

from .. import config

PLATFORM_PREFIXES = ("/platform", "/deals", "/sources", "/liquidators", "/sitemaps/", "/distress")
PLATFORM_EXACT = frozenset({"/sitemap-index.xml"})
SHARED_PREFIXES = ("/static/", "/contact", "/subscribe", "/unsubscribe", "/terms", "/privacy",
                   "/api/health", "/event", "/_event", "/stripe/", "/favicon", "/.well-known/")
# Root files (BingSiteAuth.xml, the IndexNow key, robots, sitemap) pass on both hosts.
SHARED_ROOT_SUFFIXES = (".txt", ".xml", ".ico")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "testserver", "testclient")


def platform_host() -> str:
    return config.PLATFORM_HOST


def _host(scope) -> str:
    for k, v in scope.get("headers") or ():
        if k == b"host":
            host = v.decode("latin-1").split(":")[0].strip().lower()
            return host.removeprefix("www.")
    return ""


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    """`/deals` and `/deals/...` match "/deals"; `/dealsx` does not. A prefix ending
    in "/" (e.g. "/sitemaps/") matches only what sits under it."""
    for pre in prefixes:
        if pre.endswith("/"):
            if path.startswith(pre):
                return True
        elif path == pre or path.startswith(pre + "/"):
            return True
    return False


def is_platform_path(path: str) -> bool:
    return path in PLATFORM_EXACT or _under(path, PLATFORM_PREFIXES)


def is_shared_path(path: str) -> bool:
    if _under(path, SHARED_PREFIXES):
        return True
    return path.count("/") == 1 and path.endswith(SHARED_ROOT_SUFFIXES)


def decide(host: str, path: str) -> tuple[str, str]:
    """(action, target): action is "pass", "rewrite" (target = new path) or
    "redirect" (target = absolute URL without the query string)."""
    phost = platform_host()
    host = (host or "").lower().removeprefix("www.")
    if not phost or not host or host in LOCAL_HOSTS:
        return "pass", path
    if host == phost:
        if path == "/":
            return "rewrite", "/platform"
        if path == "/robots.txt":
            return "rewrite", "/platform/robots.txt"
        if path == "/sitemap.xml":
            return "rewrite", "/sitemap-index.xml"
        if is_platform_path(path) or is_shared_path(path):
            return "pass", path
        return "redirect", f"{config.PUBLIC_BASE_URL}{path}"
    if is_platform_path(path):
        return "redirect", f"{config.PLATFORM_BASE_URL}{path}"
    return "pass", path


class HostSplitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not platform_host():
            return await self.app(scope, receive, send)
        path = scope.get("path") or "/"
        action, target = decide(_host(scope), path)
        if action == "rewrite":
            scope = dict(scope, path=target, raw_path=target.encode())
        elif action == "redirect":
            qs = scope.get("query_string") or b""
            location = target + ("?" + qs.decode("latin-1") if qs else "")
            await send({"type": "http.response.start", "status": 301,
                        "headers": [(b"location", quote(location, safe=":/?&=%+@,;$!*'()[]~").encode()),
                                    (b"cache-control", b"public, max-age=3600")]})
            await send({"type": "http.response.body", "body": b""})
            return
        return await self.app(scope, receive, send)
