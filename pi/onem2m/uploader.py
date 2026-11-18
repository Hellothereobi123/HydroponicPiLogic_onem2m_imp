"""Upload stored cycles to the oneM2M CSE, and publish camera metrics.

Runs as its own service (hydro-onem2m.service, via onem2m_sync.py), separate from the sensor
loop, so a slow or stopped CSE can never delay sampling. SQLite stays the source of truth: a row
is marked synced_onem2m only after the CSE has accepted it, so an ACME restart loses nothing.

Each cycle becomes one timeSeriesInstance whose content is config.remote_record(row), the same
record the Supabase sync and the BLE server send, and whose dataGenerationTime is the cycle
time. The CSE refuses a second instance with the same dataGenerationTime (RSC 4105), which makes
uploads safe to repeat: a duplicate means "already there".

The same loop also answers on-demand requests from the app (onem2m/ondemand.py), checking for
them every ONEM2M_REQUEST_POLL_S seconds between uploads.
"""

from __future__ import annotations

import time

import db
from config import (
    BASIN_IDS,
    ONEM2M_CSE_ID,
    ONEM2M_CSE_RN,
    ONEM2M_CSE_URL,
    ONEM2M_MQTT_HOST,
    ONEM2M_MQTT_PORT,
    ONEM2M_ORIGINATOR,
    ONEM2M_REQUEST_POLL_S,
    ONEM2M_RVI,
    ONEM2M_SERIALIZATION,
    ONEM2M_TIMEOUT_S,
    ONEM2M_TOKEN,
    ONEM2M_TRANSPORT,
    ONEM2M_UPLOAD_BATCH,
    ONEM2M_UPLOAD_INTERVAL_S,
    REMOTE_COLUMNS,
    remote_record,
)
from onem2m.client import (
    RSC_CONFLICT,
    RSC_NOT_FOUND,
    TY_CONTENT_INSTANCE,
    TY_TIME_SERIES_INSTANCE,
    CSEClient,
    CSEUnreachable,
)
from onem2m.ondemand import RequestsMissing, serve_requests
from onem2m.provision import Paths, ProvisionError, provision

SYNC_COLUMN = "synced_onem2m"
_MAX_BACKOFF_S = 60


class ResourcesMissing(Exception):
    """The CSE answered 404 for our resources, e.g. after its database was reset."""


def make_client() -> CSEClient:
    """The CSE client for ONEM2M_TRANSPORT: HTTP (CSEClient) or MQTT (MqttCSEClient, via
    gRPB-M2M). Both have the same methods, so nothing else depends on the transport."""
    if ONEM2M_TRANSPORT == "mqtt":
        from onem2m.mqtt_client import MqttCSEClient   # needs grpb-m2m; only imported when used
        return MqttCSEClient(ONEM2M_MQTT_HOST, ONEM2M_MQTT_PORT, ONEM2M_CSE_RN, ONEM2M_CSE_ID,
                             ONEM2M_ORIGINATOR, serialization=ONEM2M_SERIALIZATION,
                             timeout_s=max(ONEM2M_TIMEOUT_S, 10))
    return CSEClient(ONEM2M_CSE_URL, ONEM2M_CSE_RN, ONEM2M_ORIGINATOR, ONEM2M_TOKEN,
                     rvi=ONEM2M_RVI, timeout_s=ONEM2M_TIMEOUT_S)


def describe_target() -> str:
    """Where the client sends, for the startup log line."""
    if ONEM2M_TRANSPORT == "mqtt":
        return (f"mqtt://{ONEM2M_MQTT_HOST}:{ONEM2M_MQTT_PORT} (CSE {ONEM2M_CSE_ID}/{ONEM2M_CSE_RN}, "
                f"{ONEM2M_SERIALIZATION})")
    return f"{ONEM2M_CSE_URL}/{ONEM2M_CSE_RN}"


def data_generation_time(ts: float) -> str:
    """oneM2M absolute timestamp (UTC), e.g. 20261115T062455. ACME's dgt needs absolute time."""
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(ts))


def upload_pending(conn, client: CSEClient, paths: Paths) -> int:
    """Upload one batch of unsynced cycles, oldest first. Returns how many rows were settled.

    Rows with no sensor values at all are settled without uploading, like the Supabase sync,
    so they can't sit at the front of the queue forever.
    """
    # 1. The oldest rows the CSE doesn't have yet (synced_onem2m = 0), at most one batch
    rows = db.get_unsynced_readings(conn, ONEM2M_UPLOAD_BATCH, column=SYNC_COLUMN)
    settled: list[int] = []      # row ids that are done: uploaded, already there, or empty
    try:
        for row in rows:
            # 2. The same record Supabase and Bluetooth get, so the app parses one format
            record = remote_record(row)
            if all(record[remote] is None for remote in REMOTE_COLUMNS.values()):
                settled.append(row["id"])     # every sensor failed this cycle: nothing to send
                continue

            # 3. One timeSeriesInstance per cycle. dgt (the cycle's time) is what makes a
            #    repeat upload harmless: the CSE refuses a second instance with the same dgt.
            resp = client.create(paths.readings, TY_TIME_SERIES_INSTANCE, "m2m:tsi", {
                "dgt": data_generation_time(row["ts"]),
                "con": record,
            })

            # 4. Decide per answer, keeping rows in order (stop at the first real problem)
            if resp.ok or resp.rsc == RSC_CONFLICT:
                settled.append(row["id"])     # stored now, or stored by an earlier attempt
            elif resp.rsc == RSC_NOT_FOUND:
                raise ResourcesMissing(resp.describe())   # run() provisions again
            else:
                print(f"[onem2m] upload refused, will retry: {resp.describe()}")
                break                         # leave this row and the rest queued
    finally:
        # 5. Record progress even when the loop stopped early or an exception is on its way
        #    out (CSE unreachable mid-batch), so settled rows are never uploaded twice.
        db.mark_synced(conn, settled, column=SYNC_COLUMN)  # keep progress even if we bail out
    return len(settled)


def publish_vision(metrics: dict) -> bool:
    """Store one set of camera metrics in the vision container. For camera.py.

    Provisions first (cheap, and safe to repeat) so it works even before the uploader has run.
    Returns False instead of raising, so a CSE problem never stops the camera or refill loop.
    """
    client = make_client()
    try:
        paths = provision(client)       # camera.py is a separate process: it can't reuse run()'s paths
        resp = client.create(paths.vision, TY_CONTENT_INSTANCE, "m2m:cin", {"con": metrics})
    except (CSEUnreachable, ProvisionError) as exc:
        print(f"[onem2m] vision upload failed: {exc}")
        return False
    finally:
        client.close()                  # an MQTT client holds a connection; HTTP: nothing to do
    if not resp.ok:
        print(f"[onem2m] vision upload refused: {resp.describe()}")
    return resp.ok


def run() -> None:
    """Provision, then keep the CSE up to date with SQLite. Never exits on CSE trouble."""
    if len(BASIN_IDS) > 1:
        # Basins in one cycle share a timestamp, and the CSE allows one instance per
        # dataGenerationTime, so a second basin's row would be dropped as a duplicate.
        raise SystemExit("oneM2M mode supports one basin; give each basin its own TimeSeries first")

    conn = db.connect()                 # also adds the synced_onem2m column on first run
    client = make_client()
    paths: Paths | None = None          # None = not provisioned (yet, or any more)
    backoff = ONEM2M_UPLOAD_INTERVAL_S
    print(f"[onem2m] uploading to {describe_target()} as {ONEM2M_ORIGINATOR}")

    # The loop has three states:
    #   not provisioned -> provision(); on success, start working
    #   working         -> each pass (every ONEM2M_REQUEST_POLL_S): answer pending on-demand
    #                      requests, and every ONEM2M_UPLOAD_INTERVAL_S upload one batch
    #                      (straight on with the next pass if there's a backlog)
    #   CSE trouble     -> wait (backoff doubles up to 60 s), then try again
    # Rows wait safely in SQLite the whole time, so nothing is lost in any state.
    next_upload = 0.0                   # time.monotonic() when the next upload is due
    while True:
        try:
            if paths is None:
                paths = provision(client)
                print(f"[onem2m] provisioned {paths.ae}")
            backlog = False
            if time.monotonic() >= next_upload:
                settled = upload_pending(conn, client, paths)
                if settled:
                    print(f"[onem2m] uploaded {settled} cycle(s)")
                # A full batch means a backlog: upload again on the very next pass
                backlog = settled >= ONEM2M_UPLOAD_BATCH
                next_upload = 0.0 if backlog else time.monotonic() + ONEM2M_UPLOAD_INTERVAL_S
            serve_requests(conn, client, paths)
            backoff = ONEM2M_UPLOAD_INTERVAL_S          # healthy again: reset the backoff
            time.sleep(0 if backlog else ONEM2M_REQUEST_POLL_S)
        except (ResourcesMissing, RequestsMissing) as exc:
            # Our AE or its resources vanished (CSE database reset, AE deleted): rebuild them on
            # the next pass. The rows that hit the 404 are still queued.
            print(f"[onem2m] resources missing ({exc}); provisioning again")
            paths = None
        except (CSEUnreachable, ProvisionError) as exc:
            # CSE down, network gone, or the CSE refused provisioning: wait and retry
            print(f"[onem2m] {exc}; retrying in {backoff} s")
            time.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)
