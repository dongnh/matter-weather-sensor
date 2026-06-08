"""Apple WeatherKit REST source for rain detection.

``currentWeather.precipitationIntensity`` (mm/h) is the same realtime precip rate
the iOS Weather app shows — far more responsive to actual rain over the point than
a forecast model's grid output. We use it as a rain source: raining when the
intensity clears ``applewx_threshold_mm_h``, with the rate mapped to the usual
light/moderate/heavy/violent words.

Requires an Apple Developer membership and a WeatherKit key:
  - a ``.p8`` private key (Developer portal -> Keys -> enable WeatherKit),
  - its 10-char Key ID,
  - your 10-char Team ID,
  - a registered Service ID (an identifier, e.g. ``com.example.weather``).

Each request carries a short-lived ES256 JWT (PyJWT). We sign it once and reuse it
until shortly before it expires rather than re-signing every poll.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

WEATHERKIT_URL = "https://weatherkit.apple.com/api/v1/weather"

# Cached bearer token: {"jwt": str|None, "exp": epoch}. Shared across sensors —
# one Apple account signs for every coordinate.
_token_cache: dict = {"jwt": None, "exp": 0.0}


def _signing_jwt(cfg) -> str:
    """Return a valid bearer JWT, signing a fresh one only when the cache is stale."""
    now = time.time()
    cached = _token_cache.get("jwt")
    if cached and now < _token_cache["exp"] - 60:
        return cached

    import jwt  # PyJWT[crypto]; imported lazily so the dep is only needed for applewx

    with open(cfg.weatherkit_key_path, "rb") as f:
        key = f.read()
    iat = int(now)
    exp = iat + int(cfg.weatherkit_token_ttl)
    token = jwt.encode(
        {
            "iss": cfg.weatherkit_team_id,
            "iat": iat,
            "exp": exp,
            "sub": cfg.weatherkit_service_id,
        },
        key,
        algorithm="ES256",
        headers={
            "kid": cfg.weatherkit_key_id,
            "id": f"{cfg.weatherkit_team_id}.{cfg.weatherkit_service_id}",
        },
    )
    _token_cache["jwt"], _token_cache["exp"] = token, exp
    return token


async def fetch_current_weather(session, lat: float, lon: float, cfg) -> dict:
    """GET the ``currentWeather`` dataset for a coordinate (raises on HTTP error)."""
    token = _signing_jwt(cfg)
    url = f"{WEATHERKIT_URL}/en/{lat}/{lon}"
    params = {"dataSets": "currentWeather", "timezone": "UTC"}
    headers = {"Authorization": f"Bearer {token}"}
    async with session.get(url, params=params, headers=headers,
                           timeout=cfg.request_timeout) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return data.get("currentWeather") or {}


async def weatherkit_rain(session, sensor, cfg) -> dict | None:
    """Resolve rain from WeatherKit's precipitationIntensity (mm/h).

    Returns the standard rain dict, or ``None`` if the request failed / the field
    was absent — so :func:`decide_rain` falls through to the next source.
    """
    try:
        cur = await fetch_current_weather(session, sensor.latitude, sensor.longitude, cfg)
    except Exception as exc:  # noqa: BLE001 - best-effort; fall through to next source
        log.warning("[%s] WeatherKit fetch failed (%s)", sensor.id, exc)
        return None

    rate = cur.get("precipitationIntensity")  # mm/h
    if rate is None:
        return None
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return None

    from .rainviewer import rate_to_level

    raining = rate >= sensor.applewx_threshold_mm_h
    return {
        "raining": raining,
        "source": "applewx",
        "level": rate_to_level(rate) if raining else "none",
        "rate_mm_h": round(rate, 1),
        "dbz": None,
    }
