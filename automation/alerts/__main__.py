"""CLI for the alert blast job (BLACKWHOLE-10).

    python -m automation.alerts preview <lot_id>     # matcher only, no send
    python -m automation.alerts blast   <lot_id>     # dry-run by default

SEND IS OFF BY DEFAULT. `blast` runs dry-run (logs what would go out, emails
nothing, writes nothing) unless BOTH `--send` is passed AND the environment has
`ALERTS_SEND_ENABLED=1` with a registered `ALERTS_EMAIL_PROVIDER`. The `--send`
flag alone can't email anything — the config gate is the real switch.
"""
from __future__ import annotations

import json
import logging
import sys

import click

from .. import config
from . import blast as blast_mod


def _print(report) -> None:
    d = report.as_dict()
    # Trim recipient bodies to keep the console readable.
    click.echo(json.dumps(d, indent=2, default=str))
    click.echo(
        f"\n{'DRY-RUN' if report.dry_run else 'LIVE'} | lot={report.lot_id} "
        f"provider={report.provider} | subscribers={report.total_subscribers} "
        f"matched={report.matched} suppressed_dedupe={report.suppressed_dedupe} "
        f"sent={report.sent} failed={report.failed} capped={report.capped}"
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True)
def cli(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


@cli.command()
@click.argument("lot_id")
def preview(lot_id: str) -> None:
    """Match subscribers to a lot and print recipients + reasons. No send."""
    _print(blast_mod.preview_blast(lot_id))


@cli.command()
@click.argument("lot_id")
@click.option(
    "--send", is_flag=True,
    help="Attempt a real send. Requires ALERTS_SEND_ENABLED=1 + a provider; "
         "otherwise still dry-run.",
)
def blast(lot_id: str, send: bool) -> None:
    """Run the blast for a lot (dry-run unless --send AND config allow it)."""
    send_enabled = bool(send and config.ALERTS_SEND_ENABLED)
    if send and not config.ALERTS_SEND_ENABLED:
        click.echo(
            "refusing to send: ALERTS_SEND_ENABLED is not set — staying dry-run.",
            err=True,
        )
    report = blast_mod.run_blast(lot_id, send_enabled=send_enabled)
    _print(report)
    sys.exit(0)


if __name__ == "__main__":
    cli()
