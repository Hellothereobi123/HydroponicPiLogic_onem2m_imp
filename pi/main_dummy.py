"""Run the HydroPi with simulated sensors (testing without hardware, on a Pi or a PC).

    python3 main_dummy.py                    # simulated loop + oneM2M uploader; seeds 3 days of history
    python3 main_dummy.py --seed-days 0      # no seeded history
    python3 main_dummy.py --no-uploader      # sensor loop only

It runs the same loop as main_real.py (loop.py: validity checks, fan control, SQLite, oneM2M
uploads and on-demand requests), with sensors/dummy.py instead of hardware. Kept apart from
real data:
    database     hydro_dummy.db, not hydro.db
    device/AE    <DEVICE_NAME>-sim, e.g. HydroPi-3f9a0c12-sim (a separate system in the app)
    Supabase     never: with HYDRO_BACKEND=supabase the readings stay in hydro_dummy.db
Everything else (HYDRO_BACKEND, ONEM2M_* settings, tokens) comes from .env as usual.

On first run (empty database) it seeds --seed-days of history so the app's 24 h / 7 days views
have data: one cycle every 5 minutes, then every 30 s for the last hour. Only the last
--upload-hours of seeded cycles go to the CSE; older ones stay local, as on a real Pi.
"""

from __future__ import annotations

import os

os.environ["HYDRO_DUMMY"] = "1"   # before config is imported anywhere: separate db and device name

import argparse  # noqa: E402
import time  # noqa: E402

import db  # noqa: E402
import loop  # noqa: E402
from config import BACKEND, BASIN_IDS, SAMPLE_INTERVAL_S  # noqa: E402
from sensors.dummy import BASIN_SENSORS, MASTER_SENSORS, build_dummy_sensors, simulated_value  # noqa: E402


def seed_history(conn, days: float, upload_hours: float) -> int:
    """Fill an empty database with `days` of simulated cycles. Returns how many were added."""
    now = time.time()
    stamps = []
    t = now - days * 86400
    while t < now - 3600:                       # older history: every 5 minutes
        stamps.append(t)
        t += 300
    while t < now:                              # last hour: every cycle, like the live loop
        stamps.append(t)
        t += SAMPLE_INTERVAL_S
    names = [*MASTER_SENSORS, *BASIN_SENSORS]
    for ts in stamps:
        values = {name: simulated_value(name, ts) for name in names}
        for basin_id in BASIN_IDS:
            db.insert_cycle(conn, basin_id, values, ts=ts)
    # Only recent cycles go to the CSE (as when a Pi starts uploading); history stays local,
    # where the app's 24 h / 7 days requests read it. Supabase never gets dummy rows.
    conn.execute("UPDATE readings SET synced_onem2m = 1 WHERE ts < ?", (now - upload_hours * 3600,))
    conn.execute("UPDATE readings SET synced = 1")
    conn.commit()
    return len(stamps) * len(BASIN_IDS)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="HydroPi with simulated sensors")
    parser.add_argument("--seed-days", type=float, default=3,
                        help="history to create when the database is empty (default 3; 0 = none)")
    parser.add_argument("--upload-hours", type=float, default=2,
                        help="how much of the seeded history to send to the CSE (default 2)")
    parser.add_argument("--no-uploader", action="store_true",
                        help="oneM2M: don't run the uploader here")
    args = parser.parse_args(argv)

    conn = db.connect()
    if args.seed_days > 0 and conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0:
        added = seed_history(conn, args.seed_days, args.upload_hours)
        print(f"[dummy] seeded {added} cycles over {args.seed_days:g} day(s) in {db.DB_PATH}")
    conn.close()

    master, per_basin = build_dummy_sensors(BASIN_IDS)
    # Supabase sync stays off in dummy mode, so simulated readings never reach the real table
    loop.run(master, per_basin, with_uploader=BACKEND == "onem2m" and not args.no_uploader,
             supabase_sync=False)


if __name__ == "__main__":
    main()
