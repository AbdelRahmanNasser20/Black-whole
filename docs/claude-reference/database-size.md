<!-- Moved verbatim from ../../CLAUDE.md on 2026-08-28 (trim to <=8 KB). Original kept as ../../CLAUDE.md.pre-trim-2026-08-28 -->

## Database size — READ BEFORE ADDING A COLUMN THAT STORES A BLOB

**Supabase free tier goes READ-ONLY at 500 MB of database size**, and on
2026-08-28 it did — mid-purge, after 100,000 rows. The cause was
`deal_lots.raw`, the full GovDeals maestro response kept forever on every lot:
~2.7 KB against a 473-byte row, 61% of it provably redundant
(`raw.assetLongDescription` == the `description` column,
`raw.assetShortDescription` == `title`, 300/300 identical).

**The fix is tiering, not deletion.** A closed GovDeals page cannot be
re-scraped, and closed-auction history is the deal-finding dataset — so `raw`
moves to Cloudflare R2 (10 GB free, **zero egress**) once a lot closes:

- `deals/raw_archive.py` + `deals.cli archive-raw` + the daily
  `deals-archive-raw` cron. Export → **read the object back** → compare keys →
  only then null. Any mismatch aborts with nothing nulled. R2 unconfigured is a
  hard error, never a silent skip.
- `scripts/purge_backed_up_raw.py` drains a historical backlog. It re-proves
  coverage at run time and purges only a `TEMP TABLE` of proven keys — lots
  close continuously (125 did during one run), and a blanket predicate
  re-evaluates at execution time, so a lot closing mid-purge would otherwise be
  nulled with its blob nowhere.
- `scripts/query_cold_archive.py` reads it back: DuckDB over HTTPS, in place,
  nothing downloaded whole, nothing restored into Postgres.

  ```bash
  ./.venv/bin/python scripts/query_cold_archive.py --like "banquet chair" --zero-bid
  ```

**Nulling a column does not shrink the database.** Postgres only returns space
to the filesystem when the relation is rewritten, and `VACUUM FULL` rewrites by
building a second copy — so on a full volume the one command that can shrink
the database is the one that cannot run (`DiskFull: could not extend file`).
The way out is that **`VACUUM FULL pg_toast.pg_toast_<oid>` is accepted on its
own** and needs only the *live* TOAST size. Blobs are exactly what TOAST holds:

    deal_lots TOAST   538 MB -> 106 MB  (33s)   -> then the whole-table rewrite fit
    database         1147 MB ->  539 MB

`scripts/reclaim_db_space.py --all` encodes that order (smallest table first,
each table's TOAST before the table itself, biggest last). `VACUUM FULL` takes
an ACCESS EXCLUSIVE lock — suspend the Render crons first if a blocked run
would matter.

**The rule going forward:** any new column that stores a provider response,
a description, or any other unbounded blob needs an archival path *before* it
ships, not after it fills the disk.
