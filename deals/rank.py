# deals/rank.py
"""Claude-CLI adversarial re-rank of the day's top verdicts. Local-only
(TTY machine with `claude` on PATH); second brain on the shortlist."""
import json
import subprocess
import sys
from automation import db

_CONTRACT = ('Respond ONLY as compact JSON: [{"index": <int>, "score": <0-10>, '
             '"notes": "<risk notes, <=120 chars>"}] — one entry per lot, '
             'score = how confident you are this is a real profitable flip.')

def build_rank_prompt(verdicts: list[dict]) -> str:
    lines = ["You are auditing resale-arbitrage verdicts on government-surplus "
             "auction lots. Judge each skeptically: comp relevance, liquidity, "
             "condition risk, freight reality.", ""]
    for i, v in enumerate(verdicts):
        ident = v.get("identity") or {}
        comps = ", ".join(f"${c['price']:.0f} {c['title'][:40]}"
                          for c in (v.get("comps") or [])[:5])
        lines.append(f"{i}: {ident.get('quantity', 1)}x {ident.get('brand') or '?'} "
                     f"{ident.get('item_type', '?')} — est ${v['est_resale']:.0f}, "
                     f"margin {v['margin_pct']:.0f}%, landed ${v['landed_cost']:.0f}, "
                     f"{v['comp_count']} comps [{comps}] ({v['confidence']})")
    lines += ["", _CONTRACT]
    return "\n".join(lines)

def parse_rank_response(text: str) -> list[dict]:
    t = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        out = json.loads(t)
        return [r for r in out if isinstance(r.get("index"), int)
                and isinstance(r.get("score"), (int, float))]
    except (json.JSONDecodeError, TypeError, AttributeError):
        return []

def run_rank(top_n: int = 20) -> int:
    verdicts = db.fetch_all("""SELECT * FROM deal_verdicts
        WHERE analyzed_at > now() - interval '24 hours' AND method = 'comps'
        ORDER BY margin_pct DESC LIMIT %s""", (top_n,))
    if not verdicts:
        print("no comp-grounded verdicts in the last 24h"); return 0
    proc = subprocess.run(["claude", "-p", build_rank_prompt(verdicts),
                           "--output-format", "text"],
                          capture_output=True, text=True, timeout=300)
    ranks = parse_rank_response(proc.stdout)
    if not ranks:
        print(f"claude produced no parseable ranking: {proc.stdout[:200]!r}",
              file=sys.stderr)
        return 0
    for r in ranks:
        v = verdicts[r["index"]]
        db.execute("""UPDATE deal_verdicts SET rank_score=%s, rank_notes=%s
            WHERE asset_id=%s AND account_id=%s AND auction_id=%s AND analyzed_at=%s""",
            (float(r["score"]), (r.get("notes") or "")[:300],
             v["asset_id"], v["account_id"], v["auction_id"], v["analyzed_at"]))
    return len(ranks)
