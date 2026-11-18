"""On-demand requests through the CSE (phase 1: the Pi polls).

The CSE can't call a Pi behind NAT, so it carries requests and answers as a mailbox:

    app ──create──► <AE>/requests    con = {"rqi": "a1b2", "op": "history", "since": ..., ...}
    Pi  (uploader loop, every ONEM2M_REQUEST_POLL_S) reads the requests container,
        answers each request from SQLite, then deletes it
    Pi  ──create──► <AE>/responses   con = {"rqi": "a1b2", "status": 200, "part": 1, "of": 2,
                                            "records": [...]},  lbl = ["rqi:a1b2"]
    app ──retrieve─ <AE>/responses?rcn=4&fu=2&ty=4&lbl=rqi:a1b2   until every part is in

Records are config.remote_record(row), the same as the readings TimeSeries, Supabase and BLE,
so the app parses them with its usual function.

Operations:
    latest   the newest cycle                          -> 1 record (404 if there is none)
    history  cycles with since <= time < until          -> records, oldest first
             since/until: ISO 8601 (until optional: now); step_s (optional): at most one
             record per step, e.g. 600 = one every 10 minutes; limit (optional, capped at
             ONEM2M_REQUEST_MAX_ROWS): the newest `limit` are kept and "truncated" is set
Status: 200 ok, 400 bad request, 404 nothing to return, 500 error on the Pi.

Every request may carry "deadline" (ISO 8601): when the app stops waiting. A request found
after its deadline is dropped unanswered, so a Pi coming back online doesn't answer requests
nobody is waiting for. ONEM2M_REQUEST_MAX_AGE_S still applies as an upper bound.

The access policies (provision.py) let the app create in requests only, but tokens aren't tied
to originators, so every request is still checked here: known operations, sane times, capped
sizes. Requests older than ONEM2M_REQUEST_MAX_AGE_S are dropped unanswered (the app gave up).
"""

from __future__ import annotations

import datetime
import json
import re
import time

import db
from config import (
    ONEM2M_REQUEST_MAX_AGE_S,
    ONEM2M_REQUEST_MAX_ROWS,
    ONEM2M_RESPONSE_PART_RECORDS,
    remote_record,
)
from onem2m.client import RSC_NOT_FOUND, TY_CONTENT_INSTANCE, CSEClient
from onem2m.provision import Paths

OPS = ("latest", "history")
_RQI = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RequestsMissing(Exception):
    """The requests container answered 404 (e.g. the CSE's database was reset)."""


class BadRequest(Exception):
    """The request can't be answered as asked; the app gets this status and message."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ReplyRefused(Exception):
    """The CSE refused a reply part (e.g. bigger than the responses container allows)."""


def _parse_time(value, name: str) -> float:
    """ISO 8601 (as the app and remote_record write it) -> Unix seconds."""
    if not isinstance(value, str) or not value:
        raise BadRequest(f"{name} must be an ISO 8601 time")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise BadRequest(f"{name} is not an ISO 8601 time: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _creation_age_s(cin: dict) -> float | None:
    """Seconds since the CSE created this instance (ct, e.g. 20261117T204307,123456)."""
    ct = cin.get("ct")
    if not isinstance(ct, str) or len(ct) < 15:
        return None
    try:
        created = datetime.datetime.strptime(ct[:15], "%Y%m%dT%H%M%S").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return time.time() - created.timestamp()


def _positive_int(value, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BadRequest(f"{name} must be a whole number >= 0")
    return value


def handle(conn, request: dict) -> tuple[int, list[dict], bool]:
    """Answer one request from SQLite: (status, records, truncated). Raises BadRequest."""
    op = request.get("op")
    if op not in OPS:
        raise BadRequest(f"unknown op {op!r}; use one of {', '.join(OPS)}")

    if op == "latest":
        rows = db.get_recent_readings(conn, 1)
        if not rows:
            raise BadRequest("no readings stored yet", 404)
        return 200, [remote_record(rows[0])], False

    # history
    since = _parse_time(request.get("since"), "since")
    until = _parse_time(request["until"], "until") if request.get("until") else time.time()
    if since >= until:
        raise BadRequest("since must be before until")
    step = _positive_int(request.get("step_s"), "step_s", 0)
    limit = min(_positive_int(request.get("limit"), "limit", ONEM2M_REQUEST_MAX_ROWS) or ONEM2M_REQUEST_MAX_ROWS,
                ONEM2M_REQUEST_MAX_ROWS)

    records: list[dict] = []
    last_bucket = None
    truncated = False
    for row in db.get_readings_between(conn, since, until):
        if step:
            bucket = int((row["ts"] - since) // step)
            if bucket == last_bucket:
                continue                     # at most one record per step: the first in it
            last_bucket = bucket
        records.append(remote_record(row))
        if len(records) > limit:             # keep the newest `limit`
            records.pop(0)
            truncated = True
    return 200, records, truncated


def _reply(client: CSEClient, paths: Paths, rqi: str, status: int, records: list[dict],
           truncated: bool = False, error: str | None = None) -> None:
    """Post the answer in parts of ONEM2M_RESPONSE_PART_RECORDS records (always at least one)."""
    size = ONEM2M_RESPONSE_PART_RECORDS
    chunks = [records[i:i + size] for i in range(0, len(records), size)] or [[]]
    for number, chunk in enumerate(chunks, start=1):
        con = {"rqi": rqi, "status": status, "part": number, "of": len(chunks), "records": chunk}
        if truncated:
            con["truncated"] = True
        if error:
            con["error"] = error
        resp = client.create(paths.responses, TY_CONTENT_INSTANCE, "m2m:cin",
                             {"con": con, "lbl": [f"rqi:{rqi}"]})
        if resp.rsc == RSC_NOT_FOUND:
            raise RequestsMissing(resp.describe())
        if not resp.ok:
            raise ReplyRefused(f"part {number}/{len(chunks)}: {resp.describe()}")


def serve_requests(conn, client: CSEClient, paths: Paths) -> int:
    """Answer every pending request, oldest first. Returns how many were answered.

    CSEUnreachable propagates (uploader.run backs off); RequestsMissing means provision again.
    A request is deleted once it's answered or dropped, so it's never answered twice; if the
    CSE goes away mid-reply, it stays and is answered again (the app ignores duplicate parts).
    """
    resp = client.retrieve(paths.requests, "rcn=4&fu=2&ty=4")
    if resp.rsc == RSC_NOT_FOUND:
        raise RequestsMissing(resp.describe())
    if not resp.ok:
        print(f"[onem2m] can't read requests: {resp.describe()}")
        return 0
    instances = resp.resource("m2m:cnt").get("m2m:cin") or []
    answered = 0
    for cin in sorted(instances, key=lambda c: c.get("ct", "")):
        path = f"{paths.requests}/{cin.get('rn')}"
        con = cin.get("con")
        if isinstance(con, str):
            try:
                con = json.loads(con)
            except ValueError:
                con = None
        rqi = con.get("rqi") if isinstance(con, dict) else None

        age = _creation_age_s(cin)
        past_deadline = False
        if isinstance(con, dict) and con.get("deadline"):
            try:
                past_deadline = time.time() > _parse_time(con["deadline"], "deadline")
            except BadRequest:
                pass                         # an unreadable deadline is ignored; max age still applies
        if age is not None and age > ONEM2M_REQUEST_MAX_AGE_S:
            print(f"[onem2m] dropping request {rqi!r}: {age:.0f} s old")
        elif past_deadline:
            print(f"[onem2m] dropping request {rqi!r}: the app stopped waiting at {con['deadline']}")
        elif not (isinstance(rqi, str) and _RQI.match(rqi)):
            print(f"[onem2m] dropping request without a valid rqi: {cin.get('rn')}")
        else:
            # 1. Work out the answer. Anything wrong with the request, or a bug or database
            #    problem on the Pi, becomes an error answer instead of crashing the uploader.
            error = None
            try:
                status, records, truncated = handle(conn, con)
            except BadRequest as exc:
                status, records, truncated, error = exc.status, [], False, str(exc)
            except Exception as exc:
                status, records, truncated, error = 500, [], False, f"{type(exc).__name__}: {exc}"
            # 2. Post it. CSEUnreachable / RequestsMissing propagate and leave the request in
            #    place for the next pass; a refused reply is logged and the request dropped.
            try:
                _reply(client, paths, rqi, status, records, truncated, error)
                print(f"[onem2m] answered {con.get('op')!r} request {rqi}: {status}, "
                      f"{len(records)} record(s){', ' + error if error else ''}")
                answered += 1
            except ReplyRefused as exc:
                print(f"[onem2m] CSE refused the reply to {rqi}: {exc}")
        client.delete(path)                  # handled or dropped: never look at it again
    return answered
