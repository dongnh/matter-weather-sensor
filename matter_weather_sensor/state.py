"""In-memory weather state with SSE subscriber fan-out.

Stores, per sensor id, the latest :class:`Reading` plus the Matter ``states``
dict derived from it. All mutations happen on the event-loop thread (the poller
task + aiohttp handlers), so no locking is needed.

Matter scaling — matter_webcontrol stores sensor states as the RAW Matter
attribute integers and divides on read (see its core.py / matter_bridge.py
SENSOR_CLUSTERS). To match the physical sensors we report the same raw units:

  - temperature : 0.01 C    -> round(temp_c * 100)               (cluster 1026)
  - humidity    : 0.01 %    -> round(rh_pct * 100)               (cluster 1029)
  - pressure    : 0.1 kPa   -> round(p_hpa)  (10 x kPa == hPa)   (cluster 1027)
  - rain        : boolean   -> 1 if precip_mm >= threshold       (contact 69 /
                               occupancy 1030; Matter has no precip cluster)
  - illuminance : log scale -> round(10000*log10(lux)+1)         (cluster 1024;
                               lux = radiation_wm2 * lux_per_wm2)
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

from .config import SensorConfig
from .providers import Reading

log = logging.getLogger(__name__)


def to_matter_states(reading: Reading, sensor: SensorConfig) -> dict[str, int]:
    """Convert a reading to the raw-integer Matter states for the sensor's fields."""
    fields = sensor.fields
    states: dict[str, int] = {}
    if "temperature" in fields and reading.temperature_c is not None:
        states["temperature"] = int(round(reading.temperature_c * 100))
    if "humidity" in fields and reading.humidity_pct is not None:
        states["humidity"] = int(round(reading.humidity_pct * 100))
    if "pressure" in fields and reading.pressure_hpa is not None:
        states["pressure"] = int(round(reading.pressure_hpa))
    if "rain" in fields and reading.precipitation_mm is not None:
        raining = 1 if reading.precipitation_mm >= sensor.rain_threshold_mm else 0
        states[sensor.rain_state] = raining  # "contact" (default) or "occupancy"
    if "illuminance" in fields and reading.radiation_wm2 is not None:
        lux = max(1.0, reading.radiation_wm2 * sensor.lux_per_wm2)
        states["illuminance"] = max(1, int(round(10000 * math.log10(lux) + 1)))
    return states


class WeatherState:
    def __init__(self, ids: list[str]):
        self._states: dict[str, dict[str, int]] = {i: {} for i in ids}
        self._readings: dict[str, Reading] = {}
        self._last: dict[str, float] = {}
        self._subs: dict[str, set[asyncio.Queue]] = {}

    # -- reads ---------------------------------------------------------------
    def states(self, dev_id: str) -> dict[str, int]:
        return dict(self._states.get(dev_id, {}))

    def reading(self, dev_id: str) -> Reading | None:
        return self._readings.get(dev_id)

    def last_update(self, dev_id: str) -> float | None:
        return self._last.get(dev_id)

    # -- mutations (loop thread only) ---------------------------------------
    def update(self, dev_id: str, reading: Reading, sensor: SensorConfig,
               ts: float | None = None) -> None:
        """Record a fresh reading and fan out to subscribers if states changed."""
        ts = ts if ts is not None else time.time()
        self._last[dev_id] = ts
        self._readings[dev_id] = reading
        states = to_matter_states(reading, sensor)
        if states and states != self._states.get(dev_id):
            self._states[dev_id] = states
            log.info("%s -> %s (%s)", dev_id, states, reading.source)
            self._fanout(dev_id, states, ts)

    def _fanout(self, dev_id: str, states: dict[str, int], ts: float) -> None:
        for q in self._subs.get(dev_id, ()):
            q.put_nowait((states, ts))

    # -- subscriptions -------------------------------------------------------
    def subscribe(self, dev_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(dev_id, set()).add(q)
        return q

    def unsubscribe(self, dev_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(dev_id)
        if subs:
            subs.discard(q)
