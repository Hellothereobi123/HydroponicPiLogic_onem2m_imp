# oneM2M backend (proof of concept)

The Pi can send its readings to a **oneM2M CSE** instead of Supabase. For the proof of concept
the CSE is [ACME](https://acmecse.net) running **on the Pi itself**, so the Pi is both:

- a **CSE** (`acme-cse.service`, configured in `acme/acme.ini`), and
- an **AE** (`hydro-onem2m.service`, code in `pi/onem2m/`) that registers with that CSE and
  publishes readings.

The HydroOS app (the `homegrow_frontend_onem2m` copy) reads from the CSE, read-only. Later the
CSE moves to (or registers with) an IN-CSE on Amazon EC2; see "Moving to EC2" below.

`HYDRO_BACKEND` picks **one** backend per Pi: `supabase` (the original path, unchanged) or
`onem2m`. A oneM2M Pi never writes to Supabase. Bluetooth works the same either way.

```
 sensors → hydro-sensors (main.py) → SQLite hydro.db ──► hydro-onem2m (AE) ──localhost──► ACME CSE ◄── app (LAN, HTTP)
                                                     └──► hydro-ble ───────── Bluetooth ───────────────► app (nearby)
```

SQLite stays the source of truth. A row is marked `synced_onem2m` only after the CSE accepts
it, so an ACME restart or outage loses nothing; uploads resume where they stopped.

## Resources

```
cse-hydropi                         MN-CSE, CSE-ID /id-hydropi
└─ HydroPi-<hash>   AE              originator CHydroPi-<hash>, lbl=[type:hydropi, system:<name>]
   ├─ acp           ACP             this AE: everything; app (CHydroOS-app): retrieve + discover
   ├─ acp-requests  ACP             this AE: everything; app: create only
   ├─ readings      timeSeries      one instance per 30 s cycle, mni 5760 (2 days),
   │   └─ <tsi>                     dgt = cycle time, con = config.remote_record(row)
   ├─ vision        container       camera.py metrics, one contentInstance per capture
   ├─ requests      container       on-demand requests from the app (acp-requests), max age 60 s
   └─ responses     container       the Pi's answers, lbl=[rqi:<id>], max age 5 min
```

A reading's `con` is the same record the Supabase sync and the Bluetooth server send
(`recorded_at`, `ph`, `ec`, `temperature_c`, `humidity`, `light_intensity`, `co2` as an integer,
`water_temp`, `water_level` as true/false, `system_name`, `username`), so the app parses all
three sources with one function.

## Setup on the Pi

1. **Install ACME** in its own virtualenv (it brings many packages the sensor code doesn't need):
   ```bash
   python3 -m venv /home/pi/acme-venv
   /home/pi/acme-venv/bin/pip install acmecse
   ```
2. **Set the CSE secret.** Copy `acme/.env.example` to `acme/.env` and set
   `ACME_SECURITY_SECRET` to a long random value.
3. **Create tokens**, one for this Pi and one for the app:
   ```bash
   cd pi
   python3 -m onem2m.make_token --secret <ACME_SECURITY_SECRET>    # run twice
   ```
   Put both printed **hash** lines in `acme/certs/http_token_auth.txt` (see the `.example`).
   The Pi's **token** goes in `pi/.env` as `ONEM2M_TOKEN`; the app's token goes in the app.
4. **Configure the Pi** in `pi/.env` (see `pi/.env.example`):
   `HYDRO_BACKEND=onem2m`, `ONEM2M_TOKEN=...`. The defaults for `ONEM2M_CSE_URL`
   (`http://127.0.0.1:8080`) and `ONEM2M_CSE_RN` (`cse-hydropi`) match `acme/acme.ini`.
5. **Install the services** (adjust `User` and paths in the files if needed):
   ```bash
   cd pi
   sudo cp systemd/acme-cse.service systemd/hydro-onem2m.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now acme-cse hydro-onem2m
   sudo systemctl restart hydro-sensors     # picks up HYDRO_BACKEND
   ```
6. **Check it:**
   ```bash
   journalctl -u hydro-onem2m -f          # "[onem2m] provisioned ..." then "uploaded N cycle(s)"
   curl -s http://127.0.0.1:8080/cse-hydropi/HydroPi-<hash>/readings/la \
     -H "X-M2M-Origin: CHydroOS-app" -H "X-M2M-RI: 1" -H "X-M2M-RVI: 3" \
     -H "Accept: application/json" -H "Authorization: Bearer <app token>"
   ```
   The AE name is printed by `main.py` at startup (`device HydroPi-...`).

The app reaches the CSE at `http://<Pi's LAN IP>:8080`, so the phone must be on the same
network (or use Tailscale for off-site demos).

## Long-term data

The CSE keeps about two days of readings. On a oneM2M Pi the full history is the Pi's SQLite
database; export it with:

```bash
cd pi && python3 export_readings.py --since 2026-11-01 --out readings.csv
```

Back up `hydro.db` (or the CSV) off the Pi regularly: with no Supabase copy, a failed SD card
would lose history.

## On-demand requests (phase 1: the Pi polls)

The app can ask the Pi for data the CSE doesn't hold, such as history older than its two days.
The CSE carries the request and the answer like a mailbox, because it can't call a Pi behind NAT:

```
app ──create──► <AE>/requests    con = {"rqi": "a1b2", "op": "history", "since": "...", "step_s": 600, "deadline": "..."}
Pi  checks every 3 s (in the hydro-onem2m loop, between uploads), answers from SQLite, deletes the request
Pi  ──create──► <AE>/responses   con = {"rqi": "a1b2", "status": 200, "part": 1, "of": 2, "records": [...]}, lbl = [rqi:a1b2]
app ──retrieve─ <AE>/responses?rcn=4&fu=2&ty=4&lbl=rqi:a1b2      until every part is in
```

- **Operations** (`pi/onem2m/ondemand.py`):
  - `latest`: the newest cycle.
  - `history`: `since`, optional `until`, optional `step_s` (at most one record per step) and
    `limit`. At most 2,000 records; when more match, the newest are kept and `truncated` is set.
- **Records** are `config.remote_record(row)`, as in `readings`. Replies come in parts of 300
  records.
- **Status:** 200 ok, 400 bad request (with `error`), 404 nothing to return, 500 a problem on
  the Pi.
- **Stale requests are dropped unanswered:** past the app's `deadline`, or older than
  `ONEM2M_REQUEST_MAX_AGE_S` (60 s). A Pi coming back online doesn't answer requests nobody is
  waiting for.
- **Access:** the app may only *create* in `requests` (separate policy `acp-requests`) and
  read `responses`. Tokens aren't tied to originators, so the Pi still checks every request
  (known operations, valid times, capped sizes).
- **The app** (`homegrow_frontend_onem2m`): the History tab's **24 h** and **7 days** buttons
  use this, with one reading per 10 minutes or per hour. The app finds the Pi's AE by its
  `system:<name>` label.
- **Phase 2/3** (not built) replace the 3 s polling with a polling channel or MQTT
  notifications, for sub-second answers. The containers and message format stay the same.

## ACME behaviour this code relies on (checked with ACME 2026.5.3)

- An **MN-CSE with no registrar** serves requests normally. `enableRemoteCSE=false` for now.
- Bearer tokens: the token file holds **HMAC-SHA256(token, secret)**, not tokens
  (`onem2m/make_token.py` computes it). A missing or unknown token gets HTTP 401.
- Registering an AE again returns **4117** ("already registered"); creating an existing name
  returns **4105**. Both are treated as success, so provisioning can run on every start.
- A second timeSeriesInstance with the same `dgt` is refused with **4105**, which makes uploads
  repeat-safe: a duplicate means "already there".
- `mdd=true` (missing-data detection) also needs `mdt`.
- In an update, `acpi` must be the **only** attribute.
- `ofst` is 1-based, and discovery results are sorted **alphabetically** by default, so "last N
  instances" by offset is unreliable. Clients ask for a time window instead:
  `?rcn=4&fu=2&ty=30&cra=<UTC yyyymmddThhmmss>`, then sort by `dgt`/`recorded_at`.
- Only originators matching `CHydroPi-*` may register (`[cse.registration]`); the app reads
  without registering. Others get 4107. `CAdmin` is in the list too: with
  `enableRemoteCSE=true`, ACME's startup script runs as `CAdmin`, and startup fails with
  "Originator not allowed" if it isn't allowed.
- An IN-CSE must have **no** `registrar*` settings, or it refuses to start.
- `allowedCSROriginators` takes SP-relative CSE-IDs **with** the leading `/` (e.g.
  `/id-hydropi*`); without it ACME refuses to start, despite the comment in its default file.
- TinyDB writes are batched (`writeDelay=30`) to spare the SD card.

## Transport: HTTP (default) or MQTT with Protobuf (gRPB-M2M)

`ONEM2M_TRANSPORT` picks how the Pi reaches the CSE. The app is unaffected: it keeps reading
the same resources over HTTP/JSON.

| | `http` (default) | `mqtt` |
|---|---|---|
| Client | `onem2m/client.py` (stdlib) | `onem2m/mqtt_client.py`, using [gRPB-M2M](https://github.com/deepk2001/gRPB-M2M) (`pip install grpb-m2m`) |
| Binding | HTTP, one request per call | oneM2M MQTT binding (TS-0010), one long-lived connection to a broker |
| Encoding | JSON | `ONEM2M_SERIALIZATION`: `pb` (Protobuf), `json` or `cbor` |
| Auth | Bearer token | None at the MQTT level; who can reach the broker is limited by the firewall |
| Settings | `ONEM2M_CSE_URL`, `ONEM2M_TOKEN` | `ONEM2M_MQTT_HOST`, `ONEM2M_MQTT_PORT`, `ONEM2M_CSE_ID` (topic CSE-ID: `id-hydropi` locally, `id-in` in the cloud) |

Both clients have the same methods, so provisioning, uploads and on-demand requests run
unchanged. The MQTT client reconnects by itself after a drop; while it's down, requests count
as "CSE unreachable" and the uploader backs off as usual.

**The CSE side for `pb`:** ACME needs gRPB-M2M's patch (`patches/acme-protobuf.patch` plus the
codec as `acmecse/etc/ProtoCodec.py` and `onem2m_pb2.py`, and `protobuf>=7.35.1`). The patch
applies cleanly to ACME 2026.5.3. ACME also needs its MQTT binding on (`[mqtt] enable=true`,
the broker's address and port, and no `--no-mqtt`).

**Checked against the DigitalOcean IN-CSE** (2026-11-18, `ONEM2M_TRANSPORT=mqtt`,
`ONEM2M_SERIALIZATION=pb`, broker on port 1884): all 21 on-demand checks passed. The Pi
provisioned, uploaded and answered requests over MQTT/Protobuf, while the app side read the
same resources over HTTPS/JSON.

**Size:** one reading upload is 394 B as JSON and 354 B as Protobuf (11% smaller). gRPB-M2M's
Protobuf schema models AE, container, contentInstance and subscription, but not
timeSeriesInstance, so a reading's content still travels as compact JSON inside. Larger
savings would need the schema to cover `m2m:tsi`.

## Limits of the proof of concept

- **One basin per Pi.** Basins in a cycle share a timestamp, and the TimeSeries allows one
  instance per `dgt`. `onem2m_sync.py` refuses to start with more than one basin.
- **The app only reads, plus on-demand requests.** Its one write is posting a request into
  `requests`. There are no commands, subscriptions or polling channels yet, and requests wait up
  to ~3 s for the Pi's next check.
- **Plain HTTP on the LAN** with bearer tokens. Tokens are not bound to originators, so any
  valid token holder could claim another originator; fine on a lab network, not on the internet.
- `camera.py` reads the level sensor on GPIO 11 while `config.py` says GPIO 17; unchanged here.

## Moving to EC2 later

Run ACME on EC2 as an **IN-CSE** (`cse-in`, CSE-ID `/id-in`) behind HTTPS. Setup scripts for
this (`setup-acme-ec2.sh`, kept outside this repo in the `acme_ec2` folder) install ACME,
Caddy for HTTPS, and tokens using the same scheme as here. Then connect each Pi one of two ways.

**Option A, recommended: the Pi sends straight to EC2.** The Pi stops running its own CSE, and
the AE registers with the IN-CSE. No code changes; in `pi/.env`:

```bash
ONEM2M_CSE_URL=https://<ec2 domain>
ONEM2M_CSE_RN=cse-in
ONEM2M_TOKEN=<token from the EC2 CSE>
```

then `sudo systemctl mask --now acme-cse` (mask, because `hydro-onem2m.service` has
`Wants=acme-cse` and would start it again) and `sudo systemctl restart hydro-onem2m`.
Checked against an IN-CSE: the AE provisions and uploads, and the app's queries read it back.

**Option B: the Pi keeps its CSE and registers it with EC2.** In `acme/acme.ini`, set the
`[basic.config] registrar*` values to the EC2 CSE, `[cse.registrar] address=https://<domain>`
and `enableRemoteCSE=true`. Keep `CAdmin` in `allowedAEOriginators` (see above). Add
`ACME_CSE_REGISTRAR_SECURITY_HTTPBEARERTOKEN=<token>` to `acme/.env`. Before several Pis
register, give each a unique `cseID`/`cseName` matching the IN-CSE's
`allowedCSROriginators` (`/id-hydropi*`), and set the same name as `ONEM2M_CSE_RN` in `pi/.env`.
Registration was checked over HTTP. The readings still live on the Pi, though: to read them
through EC2, the IN-CSE has to reach a Pi behind NAT (announcements or a polling channel),
which isn't set up or tested yet.

Either way, point the app at the EC2 address (HTTPS) and, once nothing uses plain HTTP, turn
off cleartext traffic in its build.
