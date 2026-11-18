"""Create this Pi's resources on the CSE. Safe to run on every start.

    <cseBase>
    └─ HydroPi-<hash>   AE, lbl=[type:hydropi, system:<name>]
       ├─ acp           accessControlPolicy: this AE full access; the app read + discover only
       ├─ acp-requests  accessControlPolicy: this AE full access; the app create only
       ├─ readings      timeSeries, one instance per 30 s cycle, missing-data detection on
       ├─ vision        container, camera.py metrics
       ├─ requests      container (acp-requests): on-demand requests from the app
       └─ responses     container (acp): the Pi's answers, labelled rqi:<request id>

"Already exists" answers (RSC 4105 for a name, 4117 for an originator that is already
registered) count as success, so a restart or a second run changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import (
    ONEM2M_AE_RN,
    ONEM2M_APP_ID,
    ONEM2M_LABEL,
    ONEM2M_MISSING_DATA_TIMER_MS,
    ONEM2M_READER_ORIGINATORS,
    ONEM2M_READINGS_MNI,
    ONEM2M_REQUEST_MAX_AGE_S,
    ONEM2M_REQUEST_MNI,
    ONEM2M_REQUESTER_ORIGINATORS,
    ONEM2M_REQUESTS_RN,
    ONEM2M_RESPONSE_MBS,
    ONEM2M_RESPONSE_MIA_S,
    ONEM2M_RESPONSE_MNI,
    ONEM2M_RESPONSES_RN,
    ONEM2M_RVI,
    ONEM2M_VISION_MNI,
    SAMPLE_INTERVAL_S,
    SYSTEM_NAME,
)
from onem2m.client import (
    RSC_ALREADY_REGISTERED,
    RSC_CONFLICT,
    TY_ACP,
    TY_AE,
    TY_CONTAINER,
    TY_TIME_SERIES,
    CSEClient,
    Response,
)

ACP_RN = "acp"
REQUESTS_ACP_RN = "acp-requests"
READINGS_RN = "readings"
VISION_RN = "vision"

# accessControlOperations bit flags (TS-0004)
OP_CREATE, OP_RETRIEVE, OP_UPDATE, OP_DELETE, OP_NOTIFY, OP_DISCOVER = 1, 2, 4, 8, 16, 32
OP_ALL = OP_CREATE | OP_RETRIEVE | OP_UPDATE | OP_DELETE | OP_NOTIFY | OP_DISCOVER   # 63
OP_READ_ONLY = OP_RETRIEVE | OP_DISCOVER                                              # 34


class ProvisionError(Exception):
    pass


@dataclass
class Paths:
    ae: str
    readings: str
    vision: str
    requests: str
    responses: str


def _require(resp: Response, step: str, *also_ok: int) -> Response:
    """Pass the response through if it succeeded, or if its RSC is one this step accepts
    (e.g. 4105 "already exists"); otherwise stop provisioning with a ProvisionError."""
    if resp.ok or resp.rsc in also_ok:
        return resp
    raise ProvisionError(f"{step} failed: {resp.describe()}")


def _privileges(owner: str, others: list[str], others_ops: int) -> dict:
    """ACP privileges (pv): who may do what to resources that use this policy."""
    rules = [{"acor": [owner], "acop": OP_ALL}]          # this Pi: everything
    if others:
        rules.append({"acor": others, "acop": others_ops})
    return {"acr": rules}


def _ensure_acp(client: CSEClient, ae_path: str, rn: str, pv: dict) -> str:
    """Create (or, on a re-run, refresh) an access policy under the AE; returns its resource ID.

    pv = rights to the resources it protects; pvs = rights to the policy itself (only this Pi
    may change it). On a re-run the create returns 4105, so update pv instead, so that a changed
    originator list takes effect. Resources point at policies by ri, not by name.
    """
    body = {"rn": rn, "pv": pv, "pvs": {"acr": [{"acor": [client.originator], "acop": OP_ALL}]}}
    resp = client.create(ae_path, TY_ACP, "m2m:acp", body)
    if resp.rsc == RSC_CONFLICT:
        resp = _require(client.update(f"{ae_path}/{rn}", "m2m:acp", {"pv": pv}), f"update ACP {rn}")
    ri = _require(resp, f"create ACP {rn}").resource("m2m:acp").get("ri")
    if not ri:
        raise ProvisionError(f"ACP {rn} has no resource ID: {resp.describe()}")
    return ri


def provision(client: CSEClient) -> Paths:
    """Register the AE and make sure its access policies and resources exist.

    Order matters: the ACPs live under the AE, so the AE comes first; the other resources are
    created with an ACP attached (acpi), so the ACPs come before them.
    Raises ProvisionError on any refusal, and CSEUnreachable (from the client) if the CSE
    can't be reached; uploader.run() retries both.
    """
    ae_path = client.path(ONEM2M_AE_RN)

    # 1. Register the AE under the CSEBase. 4117 = this originator already has its AE (a
    #    restart), which is fine. Registration is limited to CHydroPi-* in acme.ini.
    _require(client.create(client.path(), TY_AE, "m2m:ae", {
        "rn": ONEM2M_AE_RN,
        "api": ONEM2M_APP_ID,
        "rr": False,                         # behind NAT: the CSE can't call us
        "srv": [ONEM2M_RVI],
        "lbl": [ONEM2M_LABEL, f"system:{SYSTEM_NAME}"],
    }), "register AE", RSC_ALREADY_REGISTERED)

    # 2. Access policies under the AE:
    #    acp           the app may read and discover (readings, vision, responses, the AE)
    #    acp-requests  the app may only create (post a request); it can't read others' requests
    acp_ri = _ensure_acp(client, ae_path, ACP_RN,
                         _privileges(client.originator, ONEM2M_READER_ORIGINATORS, OP_READ_ONLY))
    requests_acp_ri = _ensure_acp(client, ae_path, REQUESTS_ACP_RN,
                                  _privileges(client.originator, ONEM2M_REQUESTER_ORIGINATORS, OP_CREATE))

    # 3. Protect the AE itself with the read policy (so the app can discover it), then refresh
    #    its labels. Separate updates: ACME requires acpi to be the only attribute in an update.
    _require(client.update(ae_path, "m2m:ae", {"acpi": [acp_ri]}), "attach ACP to AE")
    # The label carries the friendly system name, which can change between runs. The app also
    # finds a system's AE by it (discovery with lbl=system:<name>).
    _require(client.update(ae_path, "m2m:ae", {"lbl": [ONEM2M_LABEL, f"system:{SYSTEM_NAME}"]}),
             "update AE labels")

    # 4. The readings timeSeries: one instance per 30 s cycle, keeping the newest `mni`.
    #    4105 = it already exists (its settings aren't changed on a re-run).
    _require(client.create(ae_path, TY_TIME_SERIES, "m2m:ts", {
        "rn": READINGS_RN,
        "mni": ONEM2M_READINGS_MNI,
        "pei": SAMPLE_INTERVAL_S * 1000,     # expected period, ms
        "mdd": True,                         # flag cycles that never arrive
        "mdt": ONEM2M_MISSING_DATA_TIMER_MS,  # ACME requires mdt whenever mdd is on
        "mdn": 10,
        "acpi": [acp_ri],
    }), "create readings timeSeries", RSC_CONFLICT)

    # 5. The vision container for camera.py's metrics (one contentInstance per capture)
    _require(client.create(ae_path, TY_CONTAINER, "m2m:cnt", {
        "rn": VISION_RN, "mni": ONEM2M_VISION_MNI, "acpi": [acp_ri],
    }), "create vision container", RSC_CONFLICT)

    # 6. On-demand requests and their answers (onem2m/ondemand.py). Short maximum ages (mia)
    #    make both transport, not storage: an old request or reply disappears on its own.
    _require(client.create(ae_path, TY_CONTAINER, "m2m:cnt", {
        "rn": ONEM2M_REQUESTS_RN, "mni": ONEM2M_REQUEST_MNI, "mia": ONEM2M_REQUEST_MAX_AGE_S,
        "acpi": [requests_acp_ri],
    }), "create requests container", RSC_CONFLICT)
    _require(client.create(ae_path, TY_CONTAINER, "m2m:cnt", {
        "rn": ONEM2M_RESPONSES_RN, "mni": ONEM2M_RESPONSE_MNI, "mbs": ONEM2M_RESPONSE_MBS,
        "mia": ONEM2M_RESPONSE_MIA_S, "acpi": [acp_ri],
    }), "create responses container", RSC_CONFLICT)

    # Where the uploader, camera and request handler work from now on
    return Paths(ae=ae_path,
                 readings=client.path(ONEM2M_AE_RN, READINGS_RN),
                 vision=client.path(ONEM2M_AE_RN, VISION_RN),
                 requests=client.path(ONEM2M_AE_RN, ONEM2M_REQUESTS_RN),
                 responses=client.path(ONEM2M_AE_RN, ONEM2M_RESPONSES_RN))
