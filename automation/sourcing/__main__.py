"""CLI for DMV sourcing alerts (BLACKWHOLE-19).

    python -m automation.sourcing searches   # print saved-search defs + register SQL (no writes)
    python -m automation.sourcing channels    # print the follow-through listing plan
    python -m automation.sourcing preview      # dry-run: read deal_lots, print matched DMV lots
    python -m automation.sourcing preview --send   # attempt a real Telegram send (guarded)

SEND IS OFF BY DEFAULT. `preview` runs dry-run (reads deal_lots, prints the
digest, sends nothing) unless `--send` is passed AND Telegram is configured.
`searches` NEVER writes to the DB — it only prints the INSERT statements you can
run yourself to register the DMV saved searches.
"""
from __future__ import annotations

import json
import logging

import click

from . import dmv
from .digest import run_dmv_sourcing_alert


@click.group()
@click.option("-v", "--verbose", is_flag=True)
def cli(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


@cli.command()
def searches() -> None:
    """Print the DMV saved-search definitions + the SQL to register them.

    Prints only — running the INSERTs is left to the operator (no live writes).
    """
    click.echo(f"# {len(dmv.SAVED_SEARCHES)} DMV saved searches "
               f"(GovDeals rows are registerable into `saved_searches`):\n")
    for s in dmv.SAVED_SEARCHES:
        params_json = json.dumps(s.params)
        click.echo(f"-- [{s.source}] {s.name}")
        click.echo(
            "INSERT INTO saved_searches (name, params, alert) VALUES "
            f"('{s.name}', '{params_json}'::jsonb, {str(s.alert).lower()}) "
            "ON CONFLICT (name) DO UPDATE SET params = EXCLUDED.params, "
            "alert = EXCLUDED.alert;"
        )
    click.echo(
        "\n# NOTE: the saved-search runner filters a single `state` only; the "
        "'within 100 mi of DC' radius is applied by `python -m automation.sourcing "
        "preview`.\n# PublicSurplus rows are intent-only (deal_lots is GovDeals)."
    )


@cli.command()
def channels() -> None:
    """Print the follow-through listing plan (list once sourced)."""
    click.echo("# DMV follow-through — list a won lot across these metros:\n")
    for p in dmv.FOLLOW_THROUGH:
        click.echo(f"• {p.metro}: platforms={','.join(p.platforms)} "
                   f"craigslist={','.join(p.craigslist_cities)}")


@cli.command()
@click.option("--send", is_flag=True,
              help="Attempt a real Telegram send. Requires TELEGRAM_* config; "
                   "otherwise stays dry-run.")
def preview(send: bool) -> None:
    """Read DMV lots from deal_lots and print the matched-lot digest (dry-run)."""
    report = run_dmv_sourcing_alert(send_enabled=send)
    click.echo(json.dumps(report.as_dict(), indent=2, default=str))
    click.echo("\n" + report.digest)
    click.echo(
        f"\n{'DRY-RUN' if report.dry_run else 'LIVE'} | lots={report.total_lots} "
        f"matched={report.matched} sent={report.sent}"
    )


if __name__ == "__main__":
    cli()
