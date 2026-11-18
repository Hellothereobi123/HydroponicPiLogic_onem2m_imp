# Local trial on Windows (no Pi)

Run the whole oneM2M setup on one Windows PC:

- the ACME CSE, from this repo's `acme/acme.ini`,
- the Pi's real uploader, fed with dummy readings,
- the HydroOS app's web build.

It uses the test tokens the app's web build already defaults to, so the app connects with no
settings. Use this to try changes before a Pi is set up. For a real Pi, follow
[onem2m.md](onem2m.md).

All commands are PowerShell. The paths assume the folders sit in
`C:\Users\tharu\capstone_react_native` (this repo and `homegrow_frontend_onem2m` side by side);
change the first part if yours differ. Everything created here is ignored by git: `acme/.env`,
the token file, `acme/data/`, `acme/logs/` and `pi/hydro.db`.

Needs Python 3.11 or newer (`python --version`) and Node.js for the app.

## One-time setup

### 1. Install ACME in its own virtualenv

In the folder that holds the repos, so one install serves every copy:

```powershell
cd C:\Users\tharu\capstone_react_native
python -m venv acme-venv
.\acme-venv\Scripts\pip install acmecse==2026.5.3
```

This takes a few minutes. The Pi code below also runs with this Python, because it has
`python-dotenv`, which `pi/config.py` needs.

### 2. Set the CSE secret

```powershell
cd C:\Users\tharu\capstone_react_native\HydroponicPiLogic_onem2m\acme
Copy-Item .env.example .env
Copy-Item certs\http_token_auth.txt.example certs\http_token_auth.txt
notepad .env
```

In Notepad, change the last line to `ACME_SECURITY_SECRET=test-secret-not-for-prod` and save.

### 3. Create the token hashes

ACME's token file holds each token's hash, not the token. Make the hashes for the two test
tokens:

```powershell
cd C:\Users\tharu\capstone_react_native\HydroponicPiLogic_onem2m\pi
python -m onem2m.make_token --secret test-secret-not-for-prod pi-test-token
python -m onem2m.make_token --secret test-secret-not-for-prod app-test-token
```

Each prints a `hash` line. Open the token file with `notepad ..\acme\certs\http_token_auth.txt`,
paste both hashes below the comments (one per line), and save. With this secret they are:

```
47abd964bc1b0e572b8ceb284648a60f28970e8be189aa17776a237528532a1d
2a492e8e03b7a27aaec1506a18aab491c44c61ea709e0edbc14b1cbce0b77451
```

## Each time: three terminals

### Terminal 1: the CSE

```powershell
cd C:\Users\tharu\capstone_react_native\HydroponicPiLogic_onem2m\acme
..\..\acme-venv\Scripts\acmecse.exe --config acme.ini --base-directory . --headless --no-mqtt --no-coap --no-ws
```

It prints a banner and then stays quiet. Leave it running; Ctrl+C stops it. To check it from
another terminal:

```powershell
curl.exe -s "http://127.0.0.1:8080/cse-hydropi?fu=1&ty=2&lbl=type:hydropi" -H "X-M2M-Origin: CHydroOS-app" -H "X-M2M-RI: 1" -H "X-M2M-RVI: 3" -H "Accept: application/json" -H "Authorization: Bearer app-test-token"
```

This is the app's own "find the Pis" query:

- `{"m2m:uril": []}` before the uploader has run, and the Pi's path after.
- An empty answer with HTTP 401 means the token file or secret is wrong.

Asking for the CSEBase itself (`/cse-hydropi` without the query) gives "no RETRIEVE privileges"
for the app. That's expected: the app may only discover and read the Pis' data.

### Terminal 2: dummy readings and the Pi's uploader

```powershell
cd C:\Users\tharu\capstone_react_native\HydroponicPiLogic_onem2m\pi
..\..\acme-venv\Scripts\python.exe seed_dummy_data.py
$env:HYDRO_BACKEND="onem2m"; $env:ONEM2M_TOKEN="pi-test-token"
..\..\acme-venv\Scripts\python.exe onem2m_sync.py
```

- `seed_dummy_data.py` adds 10 cycles to `pi/hydro.db`, ending now. It says "marked synced
  (will not upload)"; that's about Supabase only. The oneM2M uploader still sends them.
- `onem2m_sync.py` is the Pi's real uploader (`hydro-onem2m.service` on a Pi). It prints
  `provisioned cse-hydropi/HydroPi-...`, then `uploaded 10 cycle(s)`. Leave it running: it
  sends new rows every 10 seconds.
- The app only shows the last 70 minutes. For fresh readings, run `seed_dummy_data.py` again
  (another terminal, same folder); the running uploader picks them up.
- The two `$env:` settings last only for this terminal. To keep them, put
  `HYDRO_BACKEND=onem2m` and `ONEM2M_TOKEN=pi-test-token` in `pi/.env` instead (copy
  `pi/.env.example`).

### Terminal 3: the app

```powershell
cd C:\Users\tharu\capstone_react_native\homegrow_frontend_onem2m
npx expo start --web --port 8083
```

Open http://localhost:8083 and sign in with `admin` / `admin`. The web build defaults to
`http://localhost:8080` with `app-test-token`. Settings saved earlier in that browser override
those defaults: if it can't connect, open **⚙ oneM2M settings** and check the address and token.

## From another device on the network

On a phone, tablet or second computer on the same Wi-Fi, set **⚙ oneM2M settings** to:

- **CSE address:** `http://<this PC's IPv4>:8080` (from `ipconfig`)
- **Token:** `app-test-token`

When Windows asks, allow `acmecse.exe` through the firewall on **private** networks. The network
must be set to Private in Windows; on Public, the firewall blocks it.

## Starting over

Stop terminal 1 and delete `acme\data` to empty the CSE. Delete `pi\hydro.db` to empty the
local readings. The uploader provisions everything again on its next start.

## What this does and doesn't cover

This exercises the same code and config a Pi uses:

- this repo's `acme.ini`,
- the token scheme,
- provisioning, uploads and retries,
- the app's queries.

What it doesn't cover: the real sensors, the systemd services, and the Pi's network.

The test secret and tokens are public (they're in this file). Never use them on a CSE that's
reachable from the internet.
