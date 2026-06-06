"""Command-line entry point: serve | test.

  serve — run the logical-bridge REST/SSE server (what launchd runs).
  test  — fetch each configured sensor once and print the human-readable reading
          alongside the raw Matter integers it would expose. A quick check that
          the API, coordinates, model and METAR blend all work before serving.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from .config import BridgeConfig
from .state import to_matter_states


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cmd_serve(args) -> int:
    from .server import run as run_server

    cfg = BridgeConfig.load(args.config)
    asyncio.run(run_server(cfg))
    return 0


async def _test(args) -> int:
    import aiohttp

    from .providers import fetch_reading

    cfg = BridgeConfig.load(args.config)
    rc = 0
    async with aiohttp.ClientSession() as session:
        for sensor in cfg.sensors:
            try:
                r = await fetch_reading(session, sensor, cfg)
            except Exception as exc:  # noqa: BLE001
                print(f"{sensor.id} ({sensor.name}): FETCH FAILED - {exc}")
                rc = 1
                continue
            raw = to_matter_states(r, sensor)
            human = []
            if r.temperature_c is not None:
                human.append(f"{r.temperature_c:.1f}C")
            if r.humidity_pct is not None:
                human.append(f"{r.humidity_pct:.0f}%RH")
            if r.pressure_hpa is not None:
                human.append(f"{r.pressure_hpa:.0f}hPa")
            if "rain" in sensor.fields and r.raining is not None:
                tag = "RAIN" if r.raining else "dry"
                bits = f"{tag}[{r.rain_source}]"
                if r.raining and r.rain_intensity and r.rain_intensity != "none":
                    bits += f" {r.rain_intensity}"
                    if r.rain_rate_mm_h is not None:
                        bits += f" ~{r.rain_rate_mm_h}mm/h"
                    if r.rain_dbz is not None:
                        bits += f" {r.rain_dbz:.0f}dBZ"
                human.append(bits)
            if "illuminance" in sensor.fields and r.radiation_wm2 is not None:
                human.append(f"{r.radiation_wm2 * sensor.lux_per_wm2:.0f}lux")
            print(f"{sensor.id} ({sensor.name}): {'  '.join(human)}")
            print(f"  source:     {r.source}  @ {r.observed_at}")
            print(f"  matter raw: {raw}")
    return rc


def cmd_test(args) -> int:
    return asyncio.run(_test(args))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="matter-weather-sensor")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="Run the logical-bridge REST/SSE server")
    sp.add_argument("--config", required=True, help="Path to bridge.json")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("test", help="Fetch each sensor once and print the reading")
    sp.add_argument("--config", required=True, help="Path to bridge.json")
    sp.set_defaults(func=cmd_test)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
