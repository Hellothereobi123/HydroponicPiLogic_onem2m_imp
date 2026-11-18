"""Export stored readings to CSV, in the same columns the Supabase table and the CSE use.

On a oneM2M Pi there is no Supabase copy, and the CSE only keeps about two days, so the Pi's
SQLite database is the long-term record. This is how to get data out for analysis.

    python3 export_readings.py --since 2026-11-01 --until 2026-11-15 --out readings.csv
    python3 export_readings.py --days 7                                  # last week to stdout

Dates are UTC. Run from the pi/ directory (it reads hydro.db there, read-only).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys

import db
from config import REMOTE_RECORD_COLUMNS, remote_record


def _utc(date: str) -> float:
    return dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc).timestamp()


def main() -> None:
    parser = argparse.ArgumentParser(description="Export readings from hydro.db to CSV.")
    parser.add_argument("--since", help="start date/time, UTC (e.g. 2026-11-01 or 2026-11-01T06:00)")
    parser.add_argument("--until", help="end date/time, UTC (exclusive)")
    parser.add_argument("--days", type=float, help="instead of --since: this many days back from now")
    parser.add_argument("--out", help="CSV file to write (default: stdout)")
    args = parser.parse_args()

    now = dt.datetime.now(dt.timezone.utc).timestamp()
    since = now - args.days * 86400 if args.days else (_utc(args.since) if args.since else 0.0)
    until = _utc(args.until) if args.until else now + 1

    conn = db.connect_readonly()
    rows = conn.execute(
        "SELECT * FROM readings WHERE ts >= ? AND ts < ? ORDER BY ts", (since, until)
    ).fetchall()

    out = open(args.out, "w", newline="", encoding="utf8") if args.out else sys.stdout
    try:
        writer = csv.DictWriter(out, fieldnames=["basin_id", *REMOTE_RECORD_COLUMNS])
        writer.writeheader()
        for row in rows:
            writer.writerow({"basin_id": row["basin_id"], **remote_record(row)})
    finally:
        if args.out:
            out.close()
    print(f"exported {len(rows)} reading(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
