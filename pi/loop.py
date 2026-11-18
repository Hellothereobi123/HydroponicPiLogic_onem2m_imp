"""The sensor loop shared by main_real.py (hardware sensors) and main_dummy.py (simulated ones).

Every SAMPLE_INTERVAL_S (30 s):
    1. read every sensor and check each value (validity.py); a failed reading is stored as NULL
       and its raw value and reason go to the faults table (spec §4, §7)
    2. drive each basin's fan from its trusted temperature (control.py)
    3. store one row per basin in SQLite (db.insert_cycle), the source of truth
    4. Supabase backend: push unsynced rows every SYNC_INTERVAL_S (sync.py)

oneM2M backend: the uploader (onem2m/uploader.py: uploads, and answers the app's on-demand
requests) runs either here, in a background thread of this process (with_uploader=True, one
command does everything), or as its own service (hydro-onem2m.service). Either way it reads
the rows this loop stores; SQLite lets both use the database at once. The sensor loop never
waits on the network, so a slow or missing CSE can't delay sampling.
"""

from __future__ import annotations

import threading
import time

import db
from config import (
    BACKEND,
    BASIN_IDS,
    DEVICE_ID_SOURCE,
    DEVICE_NAME,
    DUMMY,
    SAMPLE_INTERVAL_S,
    SYNC_BATCH_LIMIT,
    SYNC_INTERVAL_S,
    SYSTEM_NAME,
)
from control import FanController
from validity import ValidityTracker


def _read_trusted(conn, validity, sensor) -> float | None:
    """Read one sensor and return its value only if it can be trusted, else None.

    Untrusted readings are not discarded: the raw value and the reason go into the faults
    table, because they are the record of a fault (spec §7). They simply must not drive
    anything or reach the synced dataset.
    """
    ts = time.time()
    try:
        reading = sensor.read()
    except NotImplementedError:
        return None  # hardware not wired up yet; not a fault
    except Exception as exc:
        print(f"[sensor error] {sensor.name} ({sensor.basin_id}): {exc}")
        db.insert_fault(conn, sensor.basin_id, sensor.name, None, f"read error: {exc}", ts=ts)
        return None

    reason = validity.check(reading.basin_id, reading.sensor, reading.value, ts)
    if reason:
        print(f"[invalid] {reading.sensor} ({reading.basin_id}): {reason}")
        db.insert_fault(conn, reading.basin_id, reading.sensor, reading.value, reason, ts=ts)
        return None
    return reading.value


def read_all(conn, master_sensors, per_basin_sensors, fan_controllers, validity):
    """Read every sensor once and store one row per grow basin for this cycle.

    The master basin feeds every grow basin, so its values (pH, EC, water level) are read once
    and written into each basin's row.
    """
    cycle_ts = time.time()  # one timestamp for the whole cycle's row

    master_values = {sensor.name: _read_trusted(conn, validity, sensor) for sensor in master_sensors}

    for basin_id, sensors in per_basin_sensors.items():
        values = dict(master_values)
        for sensor in sensors:
            values[sensor.name] = _read_trusted(conn, validity, sensor)

        # Spec §4: never act on a reading that failed its checks. Holding the fan at its
        # current state is safer than driving it from a probe we know is lying.
        temperature = values.get("temperature")
        if temperature is not None:
            fan_controllers[basin_id].update(temperature)

        db.insert_cycle(conn, basin_id, values, ts=cycle_ts)


def sync_pending(conn):
    """Push unsynced cycles in batches until the queue drains (spec §8).

    A whole batch is marked synced on success, including rows with nothing to send (every
    value NULL). Those have nowhere to go, and leaving them unsynced would park them at the
    front of the oldest-first queue forever, eventually filling every batch.

    Supabase backend only. sync is imported here rather than at the top so a oneM2M Pi never
    loads it (or needs the supabase package and credentials).
    """
    import sync

    while True:
        readings = db.get_unsynced_readings(conn, SYNC_BATCH_LIMIT)
        if not readings:
            return
        if not sync.send_batch(readings):
            print("[sync] batch failed, will retry next cycle")
            return
        db.mark_synced(conn, [row["id"] for row in readings])


def start_onem2m_uploader() -> threading.Thread:
    """Run the oneM2M uploader (uploads + on-demand requests) in a background thread.

    uploader.run() never returns on CSE trouble (it backs off and retries). If it stops on an
    unexpected error, it's restarted after 10 s, so a bug there can't take the sensor loop down;
    a configuration error (SystemExit, e.g. more than one basin) stops it for good.
    """
    from onem2m.uploader import run as run_uploader   # only on oneM2M Pis

    def keep_running():
        while True:
            try:
                run_uploader()
            except SystemExit as exc:
                print(f"[onem2m] uploader stopped: {exc}")
                return
            except Exception as exc:
                print(f"[onem2m] uploader crashed ({exc!r}); restarting in 10 s")
                time.sleep(10)

    thread = threading.Thread(target=keep_running, name="onem2m-uploader", daemon=True)
    thread.start()
    return thread


def run(master_sensors, per_basin_sensors, *, with_uploader: bool, supabase_sync: bool = True) -> None:
    """The main loop. Never returns.

    with_uploader  oneM2M backend: also run the uploader in this process
    supabase_sync  Supabase backend: push rows every SYNC_INTERVAL_S (main_dummy.py turns it off,
                   so simulated data never reaches the real Supabase table)
    """
    mode = "DUMMY (simulated sensors)" if DUMMY else "real sensors"
    print(f"[startup] {mode}; system_name={SYSTEM_NAME} (device {DEVICE_NAME}, id from "
          f"{DEVICE_ID_SOURCE}), backend={BACKEND}, database={db.DB_PATH}")
    conn = db.connect()
    fan_controllers = {basin_id: FanController(basin_id) for basin_id in BASIN_IDS}
    validity = ValidityTracker()

    if BACKEND == "onem2m":
        if with_uploader:
            print("[startup] oneM2M: uploading and answering requests from this process")
            start_onem2m_uploader()
        else:
            print("[startup] oneM2M: readings are uploaded by hydro-onem2m.service, not this process")
    elif not supabase_sync:
        print("[startup] Supabase sync is off: readings stay in the local database")

    last_sync = time.monotonic()
    while True:
        loop_start = time.monotonic()

        read_all(conn, master_sensors, per_basin_sensors, fan_controllers, validity)

        if BACKEND == "supabase" and supabase_sync and time.monotonic() - last_sync >= SYNC_INTERVAL_S:
            sync_pending(conn)
            last_sync = time.monotonic()

        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.0, SAMPLE_INTERVAL_S - elapsed))
