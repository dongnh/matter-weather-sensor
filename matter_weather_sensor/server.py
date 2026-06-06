"""aiohttp logical-bridge server: free weather-API readings as matter_webcontrol sensors.

Endpoints (the subset matter_webcontrol's LogicalBridgeClient consumes for
sensors — same shape as matter-appletv-presence, but the states carry the
temperature/humidity/pressure measurements instead of occupancy):

  GET  /api/devices        -> [{id, endpoint_id, names, type, states:{temperature,...}}]
  GET  /api/sensor?id=     -> {id, ...states}            (poll fallback)
  GET  /api/subscribe?id=  -> text/event-stream of state changes (SSE)
  GET  /api/health         -> human-readable last readings, for eyeballing

States are RAW Matter integers (temperature/humidity x100, pressure in hPa) so
matter_webcontrol's core.py divides them back to real units exactly as it does
for physical sensors — the device shows up in /api/sensors and /api/climate.

Register with matter_webcontrol (once; cached in its bridge_cache.json):
  GET http://<matter>:8080/api/bridge?ip=<this-host>&port=8093
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging

from aiohttp import web

from .config import BridgeConfig
from .poller import WeatherPoller
from .state import WeatherState

log = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 15.0


class WeatherBridge:
    def __init__(self, cfg: BridgeConfig):
        self.cfg = cfg
        self.sensors = {s.id: s for s in cfg.sensors}
        self.state = WeatherState(list(self.sensors))
        self._poller: WeatherPoller | None = None

    # ---------------------------------------------------------------- routes
    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/devices", self.get_devices)
        app.router.add_get("/api/sensor", self.get_sensor)
        app.router.add_get("/api/subscribe", self.subscribe)
        app.router.add_get("/api/health", self.health)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    def _snapshot(self, dev_id: str) -> dict:
        sensor = self.sensors[dev_id]
        return {
            "id": dev_id,
            "endpoint_id": 1,
            "names": [sensor.name] if sensor.name else [],
            "type": "sensor",
            "states": self.state.states(dev_id),
        }

    async def get_devices(self, request: web.Request) -> web.Response:
        return web.json_response([self._snapshot(i) for i in self.sensors])

    async def get_sensor(self, request: web.Request) -> web.Response:
        dev_id = request.query.get("id")
        if not dev_id or dev_id not in self.sensors:
            raise web.HTTPNotFound(reason=f"unknown sensor {dev_id}")
        body = {"id": dev_id, **self.state.states(dev_id)}
        r = self.state.reading(dev_id)
        if r is not None and r.raining is not None:
            # Rain intensity is informational (Matter has no precip-rate cluster).
            body["rain_intensity"] = r.rain_intensity
            body["rain_rate_mm_h"] = r.rain_rate_mm_h
            body["rain_dbz"] = r.rain_dbz
        return web.json_response(body)

    async def subscribe(self, request: web.Request) -> web.StreamResponse:
        dev_id = request.query.get("id")
        if not dev_id or dev_id not in self.sensors:
            raise web.HTTPNotFound(reason=f"unknown sensor {dev_id}")

        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await resp.prepare(request)

        q = self.state.subscribe(dev_id)
        try:
            # Emit current state immediately so a fresh subscriber is in sync.
            await self._emit(resp, dev_id, self.state.states(dev_id))
            while True:
                try:
                    states, _ts = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    continue
                await self._emit(resp, dev_id, states)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.state.unsubscribe(dev_id, q)
        return resp

    @staticmethod
    async def _emit(resp: web.StreamResponse, dev_id: str, states: dict) -> None:
        iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        payload = json.dumps({"id": dev_id, "timestamp": iso, **states})
        await resp.write(f"data: {payload}\n\n".encode("utf-8"))

    async def health(self, request: web.Request) -> web.Response:
        out = {}
        for dev_id, sensor in self.sensors.items():
            r = self.state.reading(dev_id)
            out[dev_id] = {
                "name": sensor.name,
                "states_raw": self.state.states(dev_id),
                "reading": None if r is None else {
                    "temperature_c": r.temperature_c,
                    "humidity_pct": r.humidity_pct,
                    "pressure_hpa": r.pressure_hpa,
                    "precipitation_mm": r.precipitation_mm,
                    "radiation_wm2": r.radiation_wm2,
                    "raining": r.raining,
                    "rain_source": r.rain_source,
                    "rain_intensity": r.rain_intensity,
                    "rain_rate_mm_h": r.rain_rate_mm_h,
                    "rain_dbz": r.rain_dbz,
                    "wx": r.wx,
                    "source": r.source,
                    "observed_at": r.observed_at,
                },
                "last_update": self.state.last_update(dev_id),
            }
        return web.json_response({"ok": True, "sensors": out})

    # ---------------------------------------------------------- background
    async def _on_startup(self, app: web.Application) -> None:
        self._poller = WeatherPoller(self.cfg, self.state)
        app["poller_task"] = asyncio.create_task(self._poller.run())

    async def _on_cleanup(self, app: web.Application) -> None:
        if self._poller is not None:
            self._poller.stop()
        task = app.get("poller_task")
        if task is not None:
            task.cancel()


async def run(cfg: BridgeConfig) -> None:
    bridge = WeatherBridge(cfg)
    runner = web.AppRunner(bridge.app())
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port)
    await site.start()
    log.info("matter-weather-sensor listening on %s:%d (%d sensors, poll %.0fs)",
             cfg.host, cfg.port, len(cfg.sensors), cfg.poll_interval)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
