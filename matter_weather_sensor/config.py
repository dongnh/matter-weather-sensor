"""Configuration loader for the weather sensor bridge.

bridge.json — the weather service. Run it on any always-on host with outbound
internet (the home server); it just polls a public weather API and serves the
result as Matter sensors.

Each entry in `sensors` becomes one logical device exposing the measurements in
its `fields`. Split them into separate devices (one for climate, one for rain,
one for brightness) or combine — your choice of `id`/`fields`.

    {
      "host": "0.0.0.0",
      "port": 8093,
      "poll_interval": 600,
      "sensors": [
        {
          "id": "dev_weather_hanoi",
          "name": "Hanoi Weather",
          "latitude": 21.0285,
          "longitude": 105.8542,
          "fields": ["temperature", "humidity", "pressure"],
          "model": "ecmwf_ifs025",
          "metar_station": "VVNB"
        },
        {
          "id": "dev_rain_hanoi", "name": "Hanoi Rain",
          "latitude": 21.0285, "longitude": 105.8542,
          "fields": ["rain"], "rain_threshold_mm": 0.1, "rain_state": "contact"
        },
        {
          "id": "dev_brightness_hanoi", "name": "Hanoi Brightness",
          "latitude": 21.0285, "longitude": 105.8542,
          "fields": ["illuminance"], "lux_per_wm2": 120
        }
      ]
    }

Fields (subset of these per sensor):
  - temperature / humidity / pressure -> Matter temp / humidity / pressure.
  - rain        -> binary "is it raining now", resolved by `rain_sources` in
                   priority order (first source with a definite answer wins):
                     "rainviewer" -> real radar at the exact point (best),
                     "metar"      -> station present-weather (needs metar_station),
                     "model"      -> Open-Meteo precipitation >= rain_threshold_mm.
                   Exposed as a Matter `rain_state` cluster: "contact" (default,
                   BooleanState) or "occupancy". Matter has no precipitation
                   cluster, so a binary contact/occupancy is how rain surfaces.
  - illuminance -> outdoor brightness. Open-Meteo `shortwave_radiation` (W/m^2)
                   x `lux_per_wm2` (default 120, daylight luminous efficacy) ->
                   lux, then the Matter log encoding.

Other keys:
  - provider     -> "open-meteo" (default) or "metar". "metar" only supports
                    temperature/humidity/pressure (a station has no radiation).
  - model        -> Open-Meteo model id, default "ecmwf_ifs025" (most accurate
                    global model for Hanoi/SE-Asia). "best_match" to auto-pick.
  - metar_station / metar_fields -> blend a real station observation over the
                    model for those fields (default temperature+pressure).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

VALID_FIELDS = ("temperature", "humidity", "pressure", "rain", "illuminance")
DEFAULT_FIELDS = ["temperature", "humidity", "pressure"]
# Fields a METAR station observation can supply (no radiation/precip amount).
METAR_CAPABLE = ("temperature", "humidity", "pressure")
RAIN_STATES = ("contact", "occupancy")
RAIN_SOURCES = ("rainviewer", "metar", "model")
DEFAULT_RAIN_SOURCES = ["rainviewer", "metar", "model"]


@dataclass
class SensorConfig:
    id: str
    name: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    fields: list[str] = field(default_factory=lambda: list(DEFAULT_FIELDS))
    provider: str = "open-meteo"
    model: str = "ecmwf_ifs025"
    metar_station: str = ""
    metar_fields: list[str] = field(default_factory=lambda: ["temperature", "pressure"])
    rain_threshold_mm: float = 0.1
    rain_state: str = "contact"
    rain_sources: list[str] = field(default_factory=lambda: list(DEFAULT_RAIN_SOURCES))
    lux_per_wm2: float = 120.0

    @classmethod
    def from_dict(cls, d: dict) -> "SensorConfig":
        dev_id = d.get("id")
        fields_ = [f for f in d.get("fields", DEFAULT_FIELDS) if f in VALID_FIELDS]
        if not fields_:
            raise ValueError(f"sensor {dev_id!r}: no valid fields "
                             f"(choose from {', '.join(VALID_FIELDS)})")
        provider = d.get("provider", "open-meteo")
        if provider not in ("open-meteo", "metar"):
            raise ValueError(f"sensor {dev_id!r}: unknown provider {provider!r}")
        metar_station = (d.get("metar_station") or "").strip().upper()
        if provider == "metar":
            if not metar_station:
                raise ValueError(f"sensor {dev_id!r}: provider 'metar' needs a metar_station")
            unsupported = [f for f in fields_ if f not in METAR_CAPABLE]
            if unsupported:
                raise ValueError(f"sensor {dev_id!r}: provider 'metar' cannot supply "
                                 f"{', '.join(unsupported)} (only {', '.join(METAR_CAPABLE)})")
        rain_state = d.get("rain_state", "contact")
        if rain_state not in RAIN_STATES:
            raise ValueError(f"sensor {dev_id!r}: rain_state must be one of {', '.join(RAIN_STATES)}")
        rain_sources = list(d.get("rain_sources", DEFAULT_RAIN_SOURCES))
        bad = [s for s in rain_sources if s not in RAIN_SOURCES]
        if bad:
            raise ValueError(f"sensor {dev_id!r}: unknown rain_sources {', '.join(bad)} "
                             f"(choose from {', '.join(RAIN_SOURCES)})")
        if not rain_sources:
            raise ValueError(f"sensor {dev_id!r}: rain_sources is empty")
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            latitude=float(d.get("latitude", 0.0)),
            longitude=float(d.get("longitude", 0.0)),
            fields=fields_,
            provider=provider,
            model=d.get("model", "ecmwf_ifs025"),
            metar_station=metar_station,
            metar_fields=[f for f in d.get("metar_fields", ["temperature", "pressure"])
                          if f in METAR_CAPABLE],
            rain_threshold_mm=float(d.get("rain_threshold_mm", 0.1)),
            rain_state=rain_state,
            rain_sources=rain_sources,
            lux_per_wm2=float(d.get("lux_per_wm2", 120.0)),
        )


@dataclass
class BridgeConfig:
    host: str = "0.0.0.0"
    port: int = 8093
    api_key: str | None = None
    poll_interval: float = 600.0   # seconds between weather refreshes
    request_timeout: float = 15.0  # per outbound HTTP fetch
    user_agent: str = "matter-weather-sensor/0.1 (+https://github.com/dongnh)"
    # RainViewer radar sampling (for the "rainviewer" rain source):
    rainviewer_zoom: int = 7        # pin a zoom served as RGBA8 (~1 km/px at z7)
    rainviewer_color: int = 2       # tile color scheme (2 = Universal Blue)
    rainviewer_window: int = 1      # half-size of the sampled pixel window (1 -> 3x3)
    rainviewer_alpha_min: int = 250  # opaque pixel = real echo; semi-transp = trace/context
    sensors: list[SensorConfig] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "BridgeConfig":
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        sensors = [SensorConfig.from_dict(s) for s in raw.get("sensors", [])]
        if not sensors:
            raise ValueError("bridge.json has no sensors")
        ids = [s.id for s in sensors]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate sensor ids in bridge.json")
        return cls(
            host=raw.get("host", "0.0.0.0"),
            port=int(raw.get("port", 8093)),
            api_key=raw.get("api_key"),
            poll_interval=float(raw.get("poll_interval", 600.0)),
            request_timeout=float(raw.get("request_timeout", 15.0)),
            user_agent=raw.get("user_agent", cls.user_agent),
            rainviewer_zoom=int(raw.get("rainviewer_zoom", 7)),
            rainviewer_color=int(raw.get("rainviewer_color", 2)),
            rainviewer_window=int(raw.get("rainviewer_window", 1)),
            rainviewer_alpha_min=int(raw.get("rainviewer_alpha_min", 250)),
            sensors=sensors,
        )
