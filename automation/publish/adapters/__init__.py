"""Built-in publish adapters, discovered by convention.

Every non-underscore module in this package is imported by
`registry.load_builtin()` at publish time and is expected to register one
adapter at import (see `facebook.py` / `ebay.py` for the reference shape).

>>> CRAIGSLIST (BLACKWHOLE-20) PLUGS IN HERE <<<
Drop a `craigslist.py` module in this directory that ends with
`register(CraigslistAdapter())`. Give the adapter:
    platform = "craigslist"
    aliases  = ("cl",)          # so `run.py --platforms cl` resolves
    per_city = True             # Craigslist is city-scoped -> fan out per city
    async def publish(ctx, request) -> PublishResult
No changes to the orchestrator or registry are needed — it will be discovered
automatically. Until that module lands, `--platforms cl` yields a graceful
`no_adapter` result rather than an error.
"""
