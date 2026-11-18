"""oneM2M over MQTT (TS-0010 binding) for the AE, through the gRPB-M2M library.

Same interface as client.CSEClient (path, create, retrieve, update, delete, returning a
client.Response and raising CSEUnreachable), so provision.py, uploader.py and ondemand.py work
unchanged on either transport. Chosen with ONEM2M_TRANSPORT=mqtt (see config.py).

    Pi ──publish /oneM2M/req/<originator>/<cse-id>/pb──► broker ──► CSE (MQTT binding)
    Pi ◄──────── /oneM2M/resp/<originator>/<cse-id>/pb── broker ◄── CSE

Serialization "pb" (Protobuf) needs a CSE with gRPB-M2M's ACME patch; "json" works with stock ACME.
The CSE checks the originator against its access policies as over HTTP, but MQTT carries no
bearer token: who can reach the broker matters (firewall / broker settings).

The connection to the broker opens on first use and then stays open; the library reconnects by
itself after a drop (every 1-10 s). While it's down, requests raise CSEUnreachable, and
uploader.run() backs off and retries as it does when an HTTP CSE is down.
"""

from __future__ import annotations

from urllib.parse import parse_qsl

from onem2m.client import CSEUnreachable, Response

OP_CREATE, OP_RETRIEVE, OP_UPDATE, OP_DELETE = 1, 2, 3, 4

# Rough HTTP equivalents of oneM2M response codes, so Response.status reads as it does over HTTP
_HTTP_STATUS = {2000: 200, 2001: 201, 2002: 200, 2004: 200, 4000: 400, 4004: 404, 4103: 403,
                4105: 409, 4107: 403, 4117: 409}


def query_to_params(query: str) -> dict:
    """The HTTP query strings the AE code uses ('rcn=4&fu=2&ty=4&lbl=x') as oneM2M primitive
    parameters: rcn on its own, the rest as filter criteria (fc)."""
    params: dict = {}
    fc: dict = {}
    for key, value in parse_qsl(query, keep_blank_values=False):
        if key == "rcn":
            params["rcn"] = int(value)
        elif key in ("fu", "lim", "ofst"):
            fc[key] = int(value)
        elif key == "ty":
            fc.setdefault("ty", []).append(int(value))
        elif key == "lbl":
            fc.setdefault("lbl", []).append(value)
        elif key in ("cra", "crb"):
            fc[key] = value
        else:
            raise ValueError(f"query parameter {key!r} isn't mapped for MQTT")
    if fc:
        params["fc"] = fc
    return params


class MqttCSEClient:
    """One CSE, one originator, over a long-lived MQTT connection."""

    def __init__(self, host: str, port: int, cse_rn: str, cse_id: str, originator: str,
                 serialization: str = "pb", timeout_s: float = 10.0):
        self.host, self.port, self.cse_rn, self.cse_id = host, port, cse_rn, cse_id
        self.originator, self.serialization, self.timeout_s = originator, serialization, timeout_s
        self.base_url = f"mqtt://{host}:{port}"     # for log messages, like CSEClient.base_url
        self._client = None                         # grpb Onem2mClient, created on first use

    def path(self, *parts: str) -> str:
        """Structured, CSE-relative path, e.g. path("HydroPi-1", "readings")."""
        return "/".join((self.cse_rn, *parts))

    def _connected(self):
        """The library client, connected. The first call starts the connection; later calls wait
        for it to be (re)connected, briefly, since the library keeps reconnecting in the background."""
        from grpb.onem2m.client import Onem2mClient, Onem2mError   # optional dependency (grpb-m2m)

        if self._client is None:
            self._client = Onem2mClient(self.originator, serialization=self.serialization, host=self.host,
                                        port=self.port, cse_id=self.cse_id, timeout=self.timeout_s)
            try:
                self._client.connect(timeout=self.timeout_s)
            except Onem2mError as exc:
                raise CSEUnreachable(f"MQTT broker {self.host}:{self.port}: {exc}") from exc
        elif not self._client._ep.ready.wait(self.timeout_s):     # reconnecting after a drop
            raise CSEUnreachable(f"MQTT broker {self.host}:{self.port}: not connected")
        return self._client

    def request(self, op: int, path: str, ty: int | None = None, pc: dict | None = None,
                query: str = "") -> Response:
        from grpb.onem2m.client import Onem2mError

        client = self._connected()
        try:
            resp = client.request(op, path, ty=ty, pc=pc, **query_to_params(query))
        except Onem2mError as exc:                  # no answer within the timeout
            raise CSEUnreachable(f"{path}: {exc}") from exc
        rsc = resp.rsc
        status = _HTTP_STATUS.get(rsc, 500 if rsc and rsc >= 5000 else 400)
        return Response(status=status, rsc=rsc, content=resp.pc)

    # Same operations as CSEClient: oneM2M wraps a resource in its type key, e.g. {"m2m:tsi": {...}}
    def create(self, parent: str, ty: int, key: str, attributes: dict) -> Response:
        return self.request(OP_CREATE, parent, ty=ty, pc={key: attributes})

    def retrieve(self, path: str, query: str = "") -> Response:
        return self.request(OP_RETRIEVE, path, query=query)

    def update(self, path: str, key: str, attributes: dict) -> Response:
        return self.request(OP_UPDATE, path, pc={key: attributes})

    def delete(self, path: str) -> Response:
        return self.request(OP_DELETE, path)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
