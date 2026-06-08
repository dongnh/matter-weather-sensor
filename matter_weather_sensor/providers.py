"""Weather data sources.

Two providers, both free and key-less:

  - Open-Meteo (https://open-meteo.com) — model output at exact coordinates.
    Default model ``ecmwf_ifs025`` (ECMWF IFS); for Hanoi this tracks the Noi
    Bai station within ~0.7 C / a few % RH, while GFS/JMA run too hot and dry.
    Supplies every field: temperature, humidity, pressure (``pressure_msl``,
    sea-level / barometer), precipitation (mm, for rain) and shortwave radiation
    (W/m^2, for outdoor brightness).

  - METAR (https://aviationweather.gov) — the actual hourly observation from an
    airport station. Real measurement, but only temperature / dewpoint / QNH.
    Used to override the model for the fields that travel well over distance
    (temperature, pressure).

A :class:`Reading` carries whichever measurements a source could supply; missing
ones stay ``None`` and are simply not exposed as Matter states.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .config import SensorConfig

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
METAR_URL = "https://aviationweather.gov/api/data/metar"

# config field -> Open-Meteo `current` variable
_OM_VAR = {
    "temperature": "temperature_2m",
    "humidity": "relative_humidity_2m",
    "pressure": "pressure_msl",
    "rain": "precipitation",
    "illuminance": "shortwave_radiation",
}


@dataclass
class Reading:
    temperature_c: float | None = None
    humidity_pct: float | None = None
    pressure_hpa: float | None = None
    precipitation_mm: float | None = None
    radiation_wm2: float | None = None
    wx: str | None = None          # METAR present-weather string (e.g. "-RA", "TSRA")
    raining: bool | None = None    # resolved rain decision (None = undetermined)
    rain_source: str | None = None  # which source decided `raining`
    rain_intensity: str | None = None   # "none"/"light"/"moderate"/"heavy"/"violent"
    rain_rate_mm_h: float | None = None  # estimated rain rate (mm/h), if known
    rain_dbz: float | None = None        # radar reflectivity at the point (dBZ), if known
    source: str = ""
    observed_at: str | None = None

    def merge(self, other: "Reading", only: list[str]) -> "Reading":
        """Return a copy with `only` fields taken from `other` when present."""
        out = Reading(
            temperature_c=self.temperature_c, humidity_pct=self.humidity_pct,
            pressure_hpa=self.pressure_hpa, precipitation_mm=self.precipitation_mm,
            radiation_wm2=self.radiation_wm2, wx=self.wx,
            source=self.source, observed_at=self.observed_at,
        )
        applied = []
        if "temperature" in only and other.temperature_c is not None:
            out.temperature_c = other.temperature_c
            applied.append("temperature")
        if "pressure" in only and other.pressure_hpa is not None:
            out.pressure_hpa = other.pressure_hpa
            applied.append("pressure")
        if "humidity" in only and other.humidity_pct is not None:
            out.humidity_pct = other.humidity_pct
            applied.append("humidity")
        if applied:
            out.source = f"{self.source}+{other.source}({','.join(applied)})"
        return out


def relative_humidity(temp_c: float, dewpoint_c: float) -> float:
    """RH (%) from temperature and dewpoint via the Magnus formula."""
    a, b = 17.625, 243.04
    gamma_d = (a * dewpoint_c) / (b + dewpoint_c)
    gamma_t = (a * temp_c) / (b + temp_c)
    rh = 100.0 * math.exp(gamma_d - gamma_t)
    return max(0.0, min(100.0, rh))


async def fetch_open_meteo(session, sensor: SensorConfig, timeout: float) -> Reading:
    variables = [_OM_VAR[f] for f in sensor.fields]
    params = {
        "latitude": sensor.latitude,
        "longitude": sensor.longitude,
        "current": ",".join(variables),
        "timezone": "UTC",
    }
    if sensor.model and sensor.model != "best_match":
        params["models"] = sensor.model
    async with session.get(OPEN_METEO_URL, params=params, timeout=timeout) as resp:
        resp.raise_for_status()
        data = await resp.json()
    cur = data.get("current") or {}
    return Reading(
        temperature_c=_as_float(cur.get("temperature_2m")),
        humidity_pct=_as_float(cur.get("relative_humidity_2m")),
        pressure_hpa=_as_float(cur.get("pressure_msl")),
        precipitation_mm=_as_float(cur.get("precipitation")),
        radiation_wm2=_as_float(cur.get("shortwave_radiation")),
        source=f"open-meteo:{sensor.model}",
        observed_at=cur.get("time"),
    )


async def fetch_metar(session, station: str, timeout: float, user_agent: str) -> Reading:
    params = {"ids": station, "format": "json"}
    headers = {"User-Agent": user_agent}
    async with session.get(METAR_URL, params=params, headers=headers, timeout=timeout) as resp:
        resp.raise_for_status()
        data = await resp.json()
    if not data:
        raise RuntimeError(f"no METAR for station {station}")
    o = data[0]
    temp = _as_float(o.get("temp"))
    dewp = _as_float(o.get("dewp"))
    rh = relative_humidity(temp, dewp) if temp is not None and dewp is not None else None
    return Reading(
        temperature_c=temp,
        humidity_pct=rh,
        pressure_hpa=_as_float(o.get("altim")),  # QNH, hPa
        wx=o.get("wxString"),  # present weather, e.g. "-RA", "TSRA"; None when clear
        source=f"metar:{station}",
        observed_at=o.get("reportTime"),
    )


# METAR present-weather precipitation codes (drizzle, rain, snow, ice, hail, ...).
_PRECIP_CODES = ("DZ", "RA", "SN", "SG", "PL", "GR", "GS", "IC", "UP")


def present_weather_rain(wx: str | None) -> tuple[bool, str]:
    """(raining, intensity) from a METAR present-weather string.

    METAR encodes intensity as a prefix: "-" light, none moderate, "+" heavy.
    A valid METAR with no present-weather group (CAVOK / clear) has wx=None and
    means "not precipitating" — a real observation, hence a definite (False, "none").
    """
    if not wx or not any(code in wx for code in _PRECIP_CODES):
        return False, "none"
    if "+" in wx:
        return True, "heavy"
    if "-" in wx:
        return True, "light"
    return True, "moderate"


# Open-Meteo `current` covers a 900 s (15 min) step; scale its mm to an mm/h rate.
_MODEL_MM_TO_RATE = 3600.0 / 900.0


async def decide_rain(session, sensor: SensorConfig, cfg, model_precip: float | None,
                      obs: Reading | None) -> dict:
    """Resolve rain by trying sensor.rain_sources in priority order.

    Returns {raining, source, level, rate_mm_h, dbz}. The first source with a
    definite answer wins:
      - applewx    : Apple WeatherKit precipitationIntensity (mm/h) at the point —
                     the iOS Weather app's own realtime rate. None on auth/fetch
                     failure -> fall through.
      - rainviewer : real radar at the exact point (best); also yields intensity
                     (color -> dBZ -> mm/h). None if tile/API fails -> fall through.
      - metar      : station present-weather + intensity prefix; needs metar_station.
      - model      : Open-Meteo precipitation >= rain_threshold_mm (always available).
    """
    from .rainviewer import radar_rain, rate_to_level
    from .weatherkit import weatherkit_rain

    for src in sensor.rain_sources:
        if src == "applewx":
            res = await weatherkit_rain(session, sensor, cfg)
            if res is not None:
                return res
        elif src == "rainviewer":
            res = await radar_rain(session, sensor.latitude, sensor.longitude, cfg)
            if res is not None:
                return {"raining": res["raining"], "source": "rainviewer",
                        "level": res["level"], "rate_mm_h": res["rate_mm_h"],
                        "dbz": res["dbz"]}
        elif src == "metar":
            o = obs
            if o is None and sensor.metar_station:
                try:
                    o = await fetch_metar(session, sensor.metar_station,
                                          cfg.request_timeout, cfg.user_agent)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[%s] METAR %s rain check failed (%s)",
                                sensor.id, sensor.metar_station, exc)
                    o = None
            if o is not None:
                raining, level = present_weather_rain(o.wx)
                return {"raining": raining, "source": f"metar:{sensor.metar_station}",
                        "level": level, "rate_mm_h": None, "dbz": None}
        elif src == "model":
            if model_precip is not None:
                rate = model_precip * _MODEL_MM_TO_RATE
                raining = model_precip >= sensor.rain_threshold_mm
                return {"raining": raining, "source": "model",
                        "level": rate_to_level(rate) if raining else "none",
                        "rate_mm_h": round(rate, 1), "dbz": None}
    return {"raining": None, "source": "none", "level": None, "rate_mm_h": None, "dbz": None}


async def fetch_reading(session, sensor: SensorConfig, cfg) -> Reading:
    """Fetch a sensor's reading, applying the optional METAR blend/override."""
    if sensor.provider == "metar":
        return await fetch_metar(session, sensor.metar_station,
                                 cfg.request_timeout, cfg.user_agent)

    reading = await fetch_open_meteo(session, sensor, cfg.request_timeout)

    obs: Reading | None = None
    if sensor.metar_station:
        try:
            obs = await fetch_metar(session, sensor.metar_station,
                                    cfg.request_timeout, cfg.user_agent)
        except Exception as exc:  # noqa: BLE001 - METAR is best-effort enrichment
            log.warning("[%s] METAR %s fetch failed (%s)", sensor.id, sensor.metar_station, exc)
    if obs is not None:
        # Only override fields this sensor actually exposes.
        only = [f for f in sensor.metar_fields if f in sensor.fields]
        if only:
            reading = reading.merge(obs, only=only)

    if "rain" in sensor.fields:
        rr = await decide_rain(session, sensor, cfg,
                               model_precip=reading.precipitation_mm, obs=obs)
        reading.raining = rr["raining"]
        reading.rain_source = rr["source"]
        reading.rain_intensity = rr["level"]
        reading.rain_rate_mm_h = rr["rate_mm_h"]
        reading.rain_dbz = rr["dbz"]

    return reading


def _as_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
