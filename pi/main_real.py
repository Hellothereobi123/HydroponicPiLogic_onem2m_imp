"""Run the HydroPi with its real sensors (the production entry point).

    python3 main_real.py                  # sensor loop; with HYDRO_BACKEND=onem2m also the uploader
    python3 main_real.py --no-uploader    # sensor loop only (hydro-onem2m.service uploads)

Every 30 s it reads the real sensors, checks each value, drives the fans and stores the cycle
in hydro.db (loop.py). Where the readings go is set by HYDRO_BACKEND in .env:
    supabase  this loop pushes them to Supabase every 5 minutes
    onem2m    the oneM2M uploader sends them to the CSE and answers the app's on-demand
              requests; by default in this same process, or as hydro-onem2m.service with
              --no-uploader (which is how main.py / hydro-sensors.service run it)
For testing without hardware, use main_dummy.py instead.
"""

from __future__ import annotations

import argparse

import loop
from config import BACKEND, BASIN_IDS
from sensors.environment import CO2Sensor, HumiditySensor, LightSensor, TemperatureSensor, WaterTempSensor
from sensors.master_basin import ECSensor, PHSensor, WaterLevelSensor


def build_sensors():
    """One list of master-basin sensors, plus one list per basin_id for the per-basin sensors."""
    master = [ECSensor(), PHSensor(), WaterLevelSensor()]

    per_basin = {}
    for basin_id in BASIN_IDS:
        per_basin[basin_id] = [
            TemperatureSensor(basin_id),
            WaterTempSensor(basin_id),
            HumiditySensor(basin_id),
            CO2Sensor(basin_id),
            LightSensor(basin_id),
        ]
    return master, per_basin


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="HydroPi with its real sensors")
    parser.add_argument("--no-uploader", action="store_true",
                        help="oneM2M: don't run the uploader here (hydro-onem2m.service does)")
    args = parser.parse_args(argv)

    master, per_basin = build_sensors()
    loop.run(master, per_basin, with_uploader=BACKEND == "onem2m" and not args.no_uploader)


if __name__ == "__main__":
    main()
