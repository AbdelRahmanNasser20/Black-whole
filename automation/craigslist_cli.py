"""Standalone CLI for Craigslist multi-city cross-posting (BLACKWHOLE-20).

Kept separate from ``run.py`` on purpose so this ticket stays additive and
doesn't collide with the multi-platform orchestrator (BLACKWHOLE-21).

Examples
--------
Dry run (default — prepares copy for every city, submits nothing)::

    python -m automation.craigslist_cli \
        --cities phoenix,atlanta,losangeles \
        --chair-type "Tan Metal Folding Chairs" --quantity 240 --price 15

Preview the exact per-city copy that would be posted::

    python -m automation.craigslist_cli --cities phx,ga,la \
        --chair-type "Banquet Chairs" --quantity 300 --price 20 --show-body

A live run additionally requires the CRAIGSLIST_LIVE=1 env gate; even then the
browser stops at Craigslist's review page and never clicks publish::

    CRAIGSLIST_LIVE=1 python -m automation.craigslist_cli --cities phoenix \
        --chair-type "Banquet Chairs" --quantity 300 --price 20 --live
"""

import asyncio
from pathlib import Path

import click

from .craigslist import CraigslistListing, cross_post


def _split_cities(raw: str) -> list[str]:
    return [c.strip() for c in (raw or "").split(",") if c.strip()]


def _collect_images(images_dir: str | None) -> list[Path]:
    if not images_dir:
        return []
    root = Path(images_dir).expanduser()
    if not root.is_dir():
        return []
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    return sorted(p for p in root.iterdir() if p.suffix.lower() in exts)


@click.command()
@click.option("--cities", required=True,
              help="Comma-separated city slugs/aliases, e.g. phoenix,atlanta,losangeles")
@click.option("--chair-type", required=True, help="e.g. 'Tan Metal Folding Chairs'")
@click.option("--quantity", required=True, help="Units available, e.g. 240")
@click.option("--price", required=True, type=int, help="Per-chair asking price (USD)")
@click.option("--description", default="", help="Product prose (condition, material, etc.)")
@click.option("--dimensions", default="", help="e.g. '18\" seat, 32\" tall'")
@click.option("--state", default="", help="Home state of the inventory (optional)")
@click.option("--zip-code", "zip_code", default="", help="Pickup ZIP (optional)")
@click.option("--images-dir", default="", help="Folder of images to attach (live runs)")
@click.option("--email", default="", help="Reply email for the posting (live runs)")
@click.option("--llm/--no-llm", default=False,
              help="Use Gemini for per-city copy variation (falls back if no key)")
@click.option("--live", is_flag=True,
              help="Attempt a real post. Also requires CRAIGSLIST_LIVE=1; stops at review, never publishes.")
@click.option("--show-body", is_flag=True, help="Print the full per-city body copy")
def main(cities, chair_type, quantity, price, description, dimensions, state,
         zip_code, images_dir, email, llm, live, show_body):
    city_list = _split_cities(cities)
    if not city_list:
        raise click.UsageError("--cities resolved to an empty list")

    listing = CraigslistListing(
        chair_type=chair_type,
        quantity=str(quantity),
        price=price,
        description_text=description,
        dimensions=dimensions,
        state=state,
        zip_code=zip_code,
        images=_collect_images(images_dir),
        contact_email=email,
    )

    mode = "LIVE (review-only)" if live else "DRY RUN"
    click.echo(f"[craigslist] {mode} — {len(city_list)} city(ies): {', '.join(city_list)}")

    drafts = asyncio.run(cross_post(
        listing, city_list, dry_run=not live, use_llm=llm,
    ))

    for d in drafts:
        click.echo("")
        click.echo(f"  ● {d.city_label} [{d.subdomain}] — {d.status}")
        if d.status == "skipped":
            click.echo(f"    skipped: {d.error}")
            continue
        click.echo(f"    url:   {d.post_url}")
        click.echo(f"    title: {d.title}")
        if show_body:
            for line in d.body.splitlines():
                click.echo(f"      {line}")
        if d.detail_url:
            click.echo(f"    review at: {d.detail_url}")
        if d.error:
            click.echo(f"    error: {d.error}")

    n_ready = sum(1 for d in drafts if d.status in ("dry_run", "prepared"))
    click.echo("")
    click.echo(f"[craigslist] done — {n_ready}/{len(drafts)} city drafts ready. "
               f"{'Review and publish each in the browser.' if live else 'No posts submitted (dry run).'}")


if __name__ == "__main__":
    main()
