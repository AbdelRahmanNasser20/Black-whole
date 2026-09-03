"""Apply a scripts/sql/*.sql file statement-by-statement in autocommit mode.

Needed for CREATE INDEX CONCURRENTLY, which Postgres refuses inside a
transaction block. Splits on ';' — keep migration files free of ';' inside
string literals.
"""
import pathlib
import sys

from automation import config  # noqa: F401  (loads .env)
from automation import db


def main(path: str) -> None:
    sql = pathlib.Path(path).read_text()
    stmts = [s.strip() for s in sql.split(";") if s.strip()]
    conn = db.connect(autocommit=True)
    try:
        for s in stmts:
            body = "\n".join(l for l in s.splitlines() if not l.strip().startswith("--")).strip()
            if not body:
                continue
            print(f"→ {body[:70]}…")
            conn.execute(body)
    finally:
        conn.close()
    print("ok")


if __name__ == "__main__":
    main(sys.argv[1])
