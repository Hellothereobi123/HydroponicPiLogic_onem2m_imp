"""Entry point of hydro-sensors.service: the real sensor loop on its own.

Kept so existing installs keep working. It's main_real.py with --no-uploader: on a oneM2M Pi the
uploads are done by hydro-onem2m.service (onem2m_sync.py). To run everything in one process,
use main_real.py; to test without hardware, main_dummy.py. The loop itself is in loop.py.
"""

from __future__ import annotations

import main_real
from main_real import build_sensors  # noqa: F401  (read_sensors.py imports it from here)


def main() -> None:
    main_real.main(["--no-uploader"])


if __name__ == "__main__":
    main()
