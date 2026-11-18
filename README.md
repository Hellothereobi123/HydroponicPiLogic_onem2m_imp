# HydroponicPiLogic (oneM2M)

Raspberry Pi software for the HydroOS hydroponic system: it reads the sensors every 30 seconds,
stores every reading locally, controls the fans, and publishes the data so the HydroOS app can
show it. This version adds a **oneM2M** backend: the Pi runs an
[ACME](https://acmecse.net) oneM2M CSE and publishes its readings there, as a proof of concept
before a cloud CSE on Amazon EC2.

Each Pi uses **one** backend, chosen in `pi/.env`:

| `HYDRO_BACKEND` | Where readings go | App |
|---|---|---|
| `onem2m` | ACME CSE running on the Pi (`acme/`, `pi/onem2m/`) | `homegrow_frontend_onem2m` (reads the CSE, read-only) |
| `supabase` (default) | Supabase table `sensor_readings` (`pi/sync.py`) | `homegrow_frontend` |

A oneM2M Pi never writes to Supabase. Bluetooth (for when there's no network) works with either.

## How it fits together

```
sensors ─► main.py ─► SQLite hydro.db ─┬─► onem2m_sync.py (AE) ──► ACME CSE :8080 ◄── app over LAN (read-only)
                                       ├─► sync.py ──► Supabase           (supabase backend only)
                                       └─► ble_server.py ──Bluetooth──► app nearby
camera.py ─► vision metrics ─► CSE "vision" container  (or Supabase)
```

- **SQLite is the source of truth.** Every cycle is saved locally first. Each backend keeps its
  own "sent" flag, so an outage or restart loses nothing; uploads resume where they stopped.
- **The Pi is both CSE and AE.** `acme-cse.service` runs the CSE (an MN-CSE, `cse-hydropi`);
  `hydro-onem2m.service` registers the Pi as an AE named `HydroPi-<8 hex digits>` (a hash of the
  board's serial number) and uploads each cycle as a timeSeries instance.
- Every backend sends the same record (`config.remote_record()`), so the app parses data from
  the CSE, Supabase and Bluetooth with one function.

oneM2M resources on the CSE:

```
cse-hydropi
└─ HydroPi-<hash>     AE          labels type:hydropi, system:<name>
   ├─ acp             ACP         the Pi: full access; the app (CHydroOS-app): read + discover
   ├─ readings        timeSeries  one instance per 30 s cycle, about 2 days kept
   └─ vision          container   camera metrics, one instance per capture
```

## Repository layout

| Path | What it is |
|---|---|
| `pi/main_real.py` | **Run with the real sensors.** The sensor loop, plus the oneM2M uploader in the same process (`--no-uploader` to leave that to `hydro-onem2m.service`) |
| `pi/main_dummy.py` | **Run with simulated sensors** (no hardware, on a Pi or a PC). Same loop and uploader, with its own database (`hydro_dummy.db`) and device name (`HydroPi-<hash>-sim`); seeds 3 days of history; never uploads to Supabase |
| `pi/main.py` | Entry point of `hydro-sensors.service`: `main_real.py --no-uploader` |
| `pi/loop.py` | The sensor loop both use: read sensors, validity checks, fan control, save to SQLite, Supabase sync in supabase mode |
| `pi/sensors/dummy.py` | The simulated sensors: same names and units, plausible values with a daily light cycle |
| `pi/config.py` | All settings: pins, intervals, ranges, device name, backend choice, oneM2M settings |
| `pi/db.py` | SQLite schema, migrations and the upload queues |
| `pi/sensors/` | Sensor drivers (ADS1115 pH/EC, SCD4x CO₂, VEML7700 light, DS18B20 water temp, Edenic EC, water level) |
| `pi/validity.py`, `pi/control.py` | Reading checks and fan control |
| `pi/onem2m/` | oneM2M client, provisioning, uploader and token tool |
| `pi/onem2m_sync.py` | Entry point for the oneM2M uploader service |
| `pi/sync.py`, `pi/supabase_schema.sql` | Supabase backend |
| `pi/ble_server.py`, `pi/ble_protocol.py` | Bluetooth server for the app |
| `pi/camera.py` | Plant camera metrics (canopy area, green %, wilting) |
| `pi/export_readings.py` | Export readings from SQLite to CSV |
| `pi/systemd/` | Service files: `hydro-sensors`, `hydro-ble`, `acme-cse`, `hydro-onem2m` |
| `acme/` | ACME CSE config (`acme.ini`) and templates for its secret and token file |
| `docs/onem2m.md` | oneM2M setup in detail, ACME behaviour the code relies on, moving to EC2 |
| `docs/local-trial.md` | Running the CSE, uploader and app on one Windows PC, no Pi |
| `docs/ble-protocol.md` | Bluetooth protocol between the Pi and the app |
| `PI_CONTROL_SPEC.md` | Hardware, sensing, control and safety spec |
| `PROGRESS.md` | Dated change log |

Diagnostic scripts in `pi/`: `read_sensors.py`, `read_adc.py`, `diagnose_adc.py`,
`calibrate_ph.py`, `list_edenic_devices.py`, `seed_dummy_data.py`.

## Setup on a Pi (oneM2M)

The service files expect the repo at `/home/pi/HydroponicPiLogic` and user `pi`. Clone it to that
path, or edit `User`, `WorkingDirectory` and `ExecStart` in `pi/systemd/*.service`.

```bash
git clone git@github.com:Hellothereobi123/HydroponicPiLogic_onem2m.git /home/pi/HydroponicPiLogic
cd /home/pi/HydroponicPiLogic/pi
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Then follow [docs/onem2m.md](docs/onem2m.md) to:

1. install ACME in its own virtualenv (`/home/pi/acme-venv`),
2. set the CSE secret in `acme/.env`,
3. create a token for the Pi and one for the app (`python3 -m onem2m.make_token`) and put their
   hashes in `acme/certs/http_token_auth.txt`,
4. set `HYDRO_BACKEND=onem2m` and `ONEM2M_TOKEN` in `pi/.env`,
5. install and start the services:

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hydro-sensors hydro-ble acme-cse hydro-onem2m
```

Check it with `journalctl -u hydro-onem2m -f`: it prints `provisioned` and then
`uploaded N cycle(s)`. The sensor loop prints the Pi's name (`HydroPi-...`) at startup.

In the app, open **⚙ oneM2M settings** and enter `http://<Pi's LAN IP>:8080` and the app's token.
The phone must be on the same network as the Pi.

For the Supabase backend instead, leave `HYDRO_BACKEND=supabase`, fill in `SUPABASE_URL` and
`SUPABASE_KEY`, and skip ACME, `acme-cse` and `hydro-onem2m`.

## Running it: real or dummy data

Two entry points run the same loop (`pi/loop.py`), from the `pi/` folder:

```bash
python3 main_real.py      # real sensors
python3 main_dummy.py     # simulated sensors: test without hardware, on a Pi or a PC
```

Each, every 30 s, reads every sensor, checks it, drives the fans and saves the cycle. With
`HYDRO_BACKEND=onem2m` it also runs the oneM2M uploader in the same process: uploads, plus
answering the app's 24 h / 7 days requests. So one command does everything. Settings come from
`pi/.env` as usual (`ONEM2M_CSE_URL`, `ONEM2M_TRANSPORT`, …).

| | `main_real.py` | `main_dummy.py` |
|---|---|---|
| Sensors | The real ones (`sensors/`) | Simulated (`sensors/dummy.py`) |
| Database | `hydro.db` | `hydro_dummy.db` |
| Device / system name | `HydroPi-<hash>` | `HydroPi-<hash>-sim`, a separate system in the app |
| Supabase | Synced every 5 min (supabase backend) | Never: dummy data stays local |
| History | What the Pi has recorded | 3 days seeded on first run (`--seed-days`, `0` for none); the last 2 h are uploaded (`--upload-hours`) |

`--no-uploader` on either leaves the oneM2M uploads to a separate `hydro-onem2m.service`.
That's how the installed services run it: `hydro-sensors.service` starts `main.py`, which is
`main_real.py --no-uploader`. To run everything as one service instead, point
`hydro-sensors.service` at `main_real.py` and don't enable `hydro-onem2m`.

Dummy and real data never mix, so a Pi can run `main_dummy.py` for a demo and switch back to
`main_real.py` without cleanup. Delete `hydro_dummy.db` to start the dummy history again.

## Testing without a Pi

**Step by step on Windows:** [docs/local-trial.md](docs/local-trial.md). It runs the CSE, the
Pi's uploader with dummy readings, and the app's web build on one PC.

ACME runs on Windows, macOS and Linux (`pip install acmecse`). Run it from `acme/` with this
repo's `acme.ini`, then point the web build of `homegrow_frontend_onem2m` at it; dev web builds
default to `http://localhost:8080`. Start ACME with stdin redirected, or it exits when run in
the background:

```bash
cd acme
acmecse --config acme.ini --base-directory . --headless --no-mqtt --no-coap --no-ws < /dev/null
```

## History

The CSE keeps about two days of readings; the full history stays in `pi/hydro.db`. Export it
with:

```bash
cd pi && python3 export_readings.py --since 2026-11-01 --out readings.csv
```

Back up `hydro.db` or the CSV off the Pi regularly: with the oneM2M backend there's no cloud copy.

## Current limits

- One basin per Pi with the oneM2M backend.
- The app is read-only: no commands or subscriptions yet.
- Plain HTTP on the LAN with bearer tokens, fine for a lab network, not for the internet.
- The CSE runs on the Pi; the plan is an IN-CSE on Amazon EC2 (see "Moving to EC2 later" in
  [docs/onem2m.md](docs/onem2m.md)).
- `camera.py` reads the water level sensor on GPIO 11 while `config.py` says GPIO 17.
- Not yet run end to end on a real Pi; tested on Windows against ACME 2026.5.3.
