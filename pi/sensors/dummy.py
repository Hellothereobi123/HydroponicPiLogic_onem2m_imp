"""Simulated sensors for main_dummy.py: no hardware needed.

Same names and units as the real sensors (sensors/environment.py, sensors/master_basin.py), so
everything downstream (validity checks, fan control, SQLite, Supabase/BLE/oneM2M records) treats
them exactly like real ones. Values are plausible for a lettuce system and follow a daily cycle:
grow lights on for part of the day raise light, air temperature, CO2 use and water temperature,
and lower humidity. Small noise keeps every value moving, so the "stuck probe" check
(STUCK_READING_COUNT identical readings) never fires on them.

simulated_value() is also used to seed history (main_dummy.py --seed-days).
"""

from __future__ import annotations

import math
import random
import time

from config import MASTER_BASIN
from sensors.base import Reading, Sensor

_rng = random.Random()

# name -> unit, as on the real sensors
MASTER_SENSORS = {"ec": "mS/cm", "ph": "pH", "water_level": "bool"}
BASIN_SENSORS = {"temperature": "C", "water_temp": "C", "humidity": "pct", "co2": "ppm", "light": "lux"}


def _lights(ts: float) -> float:
    """0 at night, up to 1 at midday (UTC), like grow lights on a timer."""
    day = 2 * math.pi * ((ts % 86400) / 86400)
    return max(0.0, math.sin(day - 1.2))


def simulated_value(name: str, ts: float) -> float:
    """One plausible reading of `name` at time `ts` (Unix seconds)."""
    lights = _lights(ts)
    g = _rng.gauss
    if name == "ph":
        return round(6.1 + 0.15 * math.sin(ts / 9000) + g(0, 0.02), 2)
    if name == "ec":
        return round(1.65 + 0.1 * math.sin(ts / 20000) + g(0, 0.01), 2)
    if name == "temperature":
        return round(21.5 + 3.5 * lights + g(0, 0.15), 2)
    if name == "humidity":
        return round(62 - 8 * lights + g(0, 0.8), 2)
    if name == "light":
        # Dark: a few lux of noise, as a real sensor reads; lit: up to ~18,000 lux
        return round(18000 * lights + g(0, 150), 1) if lights > 0.02 else round(abs(g(0, 0.5)), 2)
    if name == "co2":
        return float(round(700 + 250 * lights + g(0, 12)))
    if name == "water_temp":
        return round(19.5 + 1.2 * lights + g(0, 0.05), 2)
    if name == "water_level":
        return 1.0                       # water at the low-level sensor: normal
    raise ValueError(f"no simulation for sensor {name!r}")


class DummySensor(Sensor):
    """A sensor that reads simulated_value(name, now)."""

    def __init__(self, name: str, unit: str, basin_id: str):
        super().__init__(basin_id)
        self.name, self.unit = name, unit

    def read(self) -> Reading:
        return Reading(self.basin_id, self.name, simulated_value(self.name, time.time()), self.unit)


def build_dummy_sensors(basin_ids: list[str]):
    """Same shape as main_real.build_sensors(): master-basin sensors, and per-basin sensors."""
    master = [DummySensor(name, unit, MASTER_BASIN) for name, unit in MASTER_SENSORS.items()]
    per_basin = {basin_id: [DummySensor(name, unit, basin_id) for name, unit in BASIN_SENSORS.items()]
                 for basin_id in basin_ids}
    return master, per_basin
