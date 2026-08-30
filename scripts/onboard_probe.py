#!/usr/bin/env python3
"""Probe an auction site for the onboarding agent. Fetches are logged-out, UA-honest,
one request per second minimum, and STOP on any 403/CAPTCHA (report it, never bypass)."""
import json, re, sys, time, pathlib, argparse, urllib.parse, requests

UA = "Mozilla/5.0 (BLACKWHOLE onboarding probe; contact: abdel@black-whole.com)"
MARKERS = ["__APOLLO_STATE__", "__NEXT_DATA__", "window.__NUXT__", "application/ld+json"]

def fetch(url, out, name, delay):
    time.sleep(delay)
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30, allow_redirects=True)
    (out / name).write_bytes(r.content[:500_000])
    return r

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("url"); ap.add_argument("--site", required=True)
    ap.add_argument("--delay", type=float, default=2.0); a = ap.parse_args()
    base = f"{urllib.parse.urlparse(a.url).scheme}://{urllib.parse.urlparse(a.url).netloc}"
    out = pathlib.Path(f"tests/deals/fixtures/{a.site}/probe"); out.mkdir(parents=True, exist_ok=True)
    report = [f"# Probe {a.site} — {base}"]
    rob = fetch(base + "/robots.txt", out, "robots.txt", 0)
    delay = a.delay; legal = "ok"
    if rob.ok:
        m = re.search(r"(?im)^crawl-delay:\s*(\d+)", rob.text)
        if m: delay = max(delay, float(m.group(1))); legal = f"crawl-delay={m.group(1)}"
        if re.search(r"(?i)prohibited|automated means", rob.text): legal = "tos-clause-found"
    report.append(f"legal: {legal}")
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        s = fetch(base + path, out, path.strip("/").replace("/", "_"), delay)
        if s.ok and b"<" in s.content[:200]: report.append(f"sitemap: {path} HTTP {s.status_code}"); break
    page = fetch(a.url, out, "sample.html", delay)
    if page.status_code == 403: report.append("access: blocked (403 plain fetch — try Patchright, do NOT bypass a challenge)")
    else:
        hits = [mk for mk in MARKERS if mk in page.text]
        if hits: report.append(f"access: embedded-json ({', '.join(hits)})")
        elif len(re.sub(r"<[^>]+>", "", page.text).strip()) < 500: report.append("access: spa-needs-browser")
        else: report.append("access: html")
    ct = page.headers.get("content-type", ""); report.append(f"status: {page.status_code} {ct}")
    (out / "PROBE-REPORT.md").write_text("\n".join(report) + "\n"); print("\n".join(report))

if __name__ == "__main__": main()
