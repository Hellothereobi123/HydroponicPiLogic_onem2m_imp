"""Minimal oneM2M HTTP-binding client (TS-0009), standard library only.

Only what the AE needs: create, retrieve, update and delete, with the oneM2M request headers, a bearer
token, and the response status code (RSC) surfaced so callers can decide what counts as success.
Behaviour was checked against ACME 2026.5.3; see docs/onem2m.md.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

# Resource types (ty) used here
TY_ACP = 1
TY_AE = 2
TY_CONTAINER = 3
TY_CONTENT_INSTANCE = 4
TY_TIME_SERIES = 29
TY_TIME_SERIES_INSTANCE = 30

# Response status codes (RSC) the AE acts on
RSC_OK = 2000
RSC_CREATED = 2001
RSC_DELETED = 2002
RSC_UPDATED = 2004
RSC_NOT_FOUND = 4004
RSC_ORIGINATOR_NO_PRIVILEGE = 4103
RSC_CONFLICT = 4105              # a resource with that name (or TSI with that dgt) exists
RSC_ALREADY_REGISTERED = 4117    # this originator already has an AE


@dataclass
class Response:
    """Every answer from the CSE, success or not. Callers decide what an RSC means for them:
    e.g. 4105 (conflict) is a failure in general, but "already uploaded" to the uploader."""
    status: int                   # HTTP status
    rsc: int | None               # oneM2M response status code (X-M2M-RSC)
    content: Any                  # parsed JSON body, or text if not JSON

    @property
    def ok(self) -> bool:
        # oneM2M success codes, judged by the RSC header rather than the HTTP status
        return self.rsc in (RSC_OK, RSC_CREATED, RSC_DELETED, RSC_UPDATED)

    def resource(self, key: str) -> dict:
        """The resource body, e.g. resource("m2m:ts")."""
        if isinstance(self.content, dict) and isinstance(self.content.get(key), dict):
            return self.content[key]
        return {}

    def describe(self) -> str:
        if isinstance(self.content, dict) and "m2m:dbg" in self.content:
            return f"{self.status}/{self.rsc}: {self.content['m2m:dbg']}"
        return f"{self.status}/{self.rsc}"


class CSEUnreachable(Exception):
    """The CSE could not be contacted at all (refused, timed out, DNS)."""


class CSEClient:
    """One CSE, one originator. Two outcomes per request:
      - the CSE answered (any HTTP status) -> a Response, which the caller inspects
      - the CSE couldn't be reached        -> CSEUnreachable, which callers retry later
    """

    def __init__(self, base_url: str, cse_rn: str, originator: str, token: str = "",
                 rvi: str = "3", timeout_s: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.cse_rn = cse_rn
        self.originator = originator
        self.token = token
        self.rvi = rvi
        self.timeout_s = timeout_s

    def path(self, *parts: str) -> str:
        """Structured, CSE-relative path, e.g. path("HydroPi-1", "readings")."""
        return "/".join((self.cse_rn, *parts))

    def request(self, method: str, path: str, ty: int | None = None,
                body: dict | None = None, query: str = "") -> Response:
        # 1. oneM2M request headers: who is asking (originator), a unique request ID, and the
        #    protocol release. ACME checks the bearer token before anything else (401 if bad).
        headers = {
            "X-M2M-Origin": self.originator,
            "X-M2M-RI": uuid.uuid4().hex,
            "X-M2M-RVI": self.rvi,
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        # 2. A create names the new resource's type in the Content-Type (";ty=30" etc.)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json" + (f";ty={ty}" if ty is not None else "")
        url = f"{self.base_url}/{path}{('?' + query) if query else ''}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)

        # 3. Send. urllib raises HTTPError for 4xx/5xx, but those are still answers from the
        #    CSE (with an RSC), so they become a Response too. Only "no answer at all" raises.
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return _response(resp.status, resp.headers.get("X-M2M-RSC"), resp.read())
        except urllib.error.HTTPError as exc:
            return _response(exc.code, exc.headers.get("X-M2M-RSC"), exc.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise CSEUnreachable(f"{method} {url}: {exc}") from exc

    # The operations the AE uses (delete: handled on-demand requests). oneM2M wraps a resource in its type key, e.g.
    # {"m2m:tsi": {...}}; `key` is that wrapper and `attributes` what goes inside.
    def create(self, parent: str, ty: int, key: str, attributes: dict) -> Response:
        return self.request("POST", parent, ty=ty, body={key: attributes})

    def retrieve(self, path: str, query: str = "") -> Response:
        return self.request("GET", path, query=query)

    def update(self, path: str, key: str, attributes: dict) -> Response:
        return self.request("PUT", path, body={key: attributes})

    def delete(self, path: str) -> Response:
        return self.request("DELETE", path)

    def close(self) -> None:
        """Nothing to close over HTTP (one connection per request); MqttCSEClient has a real one."""


def _response(status: int, rsc: str | None, raw: bytes) -> Response:
    # Lenient on purpose: a body that isn't JSON or a missing RSC header still yields a Response
    # (with content as text / rsc None), so a strange answer is logged instead of crashing.
    try:
        content: Any = json.loads(raw) if raw else None
    except ValueError:
        content = raw.decode(errors="replace")
    try:
        code = int(rsc) if rsc else None
    except ValueError:
        code = None
    return Response(status=status, rsc=code, content=content)
