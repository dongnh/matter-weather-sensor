"""Background poller: refresh every sensor's reading on a fixed interval.

One shared aiohttp session, one task. On each tick it fetches every configured
sensor (model + optional METAR blend) and pushes the result into WeatherState,
which fans the change out to any SSE subscribers. A failed fetch for one sensor
is logged and skipped — the last good reading keeps being served, so a transient
API hiccup never blanks the sensor.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from .config import BridgeConfig
from .providers import fetch_reading
from .state import WeatherState

log = logging.getLogger(__name__)


class WeatherPoller:
    def __init__(self, cfg: BridgeConfig, state: WeatherState):
        self.cfg = cfg
        self.state = state
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            while not self._stop.is_set():
                await self._poll_once(session)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.poll_interval)
                except asyncio.TimeoutError:
                    pass

    async def _poll_once(self, session: aiohttp.ClientSession) -> None:
        results = await asyncio.gather(
            *(self._fetch_one(session, s) for s in self.cfg.sensors),
            return_exceptions=True,
        )
        for sensor, res in zip(self.cfg.sensors, results):
            if isinstance(res, Exception):
                log.warning("[%s] fetch failed: %s", sensor.id, res)

    async def _fetch_one(self, session, sensor) -> None:
        reading = await fetch_reading(session, sensor, self.cfg)
        self.state.update(sensor.id, reading, sensor)
