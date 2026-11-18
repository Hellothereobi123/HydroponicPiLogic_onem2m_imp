"""Entry point for hydro-onem2m.service: keep the oneM2M CSE up to date with the local database.

Only for HYDRO_BACKEND=onem2m. Run from the pi/ directory:  python3 onem2m_sync.py

The work happens in onem2m/uploader.py: run() provisions this Pi's resources on the CSE, then
uploads queued cycles from SQLite forever (see the overview in onem2m/__init__.py). CSE
problems are retried inside run(); it only stops on its own for a configuration error (more
than one basin). Anything unexpected crashes it, and the service's Restart=always starts it again.
"""

from config import BACKEND
from onem2m.uploader import run

if __name__ == "__main__":
    # The unit file ships with every copy of the repo, so it can get started on a Supabase Pi
    # by mistake: refuse rather than upload to a CSE nobody reads
    if BACKEND != "onem2m":
        raise SystemExit(f'HYDRO_BACKEND is "{BACKEND}"; set HYDRO_BACKEND=onem2m to use the CSE')
    run()
