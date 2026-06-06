"""RainViewer radar — real precipitation at an exact point, free and key-less.

RainViewer (https://www.rainviewer.com) merges 1200+ national weather radars into
a global mosaic, refreshed ~every 10 min. The public weather-maps endpoint is
free and needs no key. There is no point-value JSON in the free tier, only raster
tiles, so we sample the radar tile pixel at the sensor's lat/lon.

Tile format note (verified against live tiles): low zooms (<=7) are served as
8-bit RGBA PNGs where actual precipitation echoes are drawn fully opaque
(alpha 255) over a faint, semi-transparent context layer (alpha < ~200, e.g. tan
"trace" pixels that are NOT rain). So "raining" = a fully opaque pixel near the
point. High zooms (>=8) switch to a paletted PNG whose alpha semantics differ;
we deliberately pin zoom 7 and accept ONLY RGBA8 — any other format makes
:func:`radar_raining` return ``None`` so the caller falls through to METAR.

This is the most accurate "is it raining here right now" source, but it is
radar: it can't tell "no rain" from "outside radar coverage" (both render
transparent), and it lags ~5-10 min.
"""

from __future__ import annotations

import logging
import math
import struct
import zlib

log = logging.getLogger(__name__)

RAINVIEWER_MAPS_URL = "https://api.rainviewer.com/public/weather-maps.json"
TILE_SIZE = 256


def _tile_pixel(lat: float, lon: float, z: int, size: int = TILE_SIZE):
    """Slippy-map tile x/y and the within-tile pixel for a coordinate."""
    n = 2 ** z
    xf = (lon + 180.0) / 360.0 * n
    yf = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    xt, yt = int(xf), int(yf)
    return xt, yt, int((xf - xt) * size), int((yf - yt) * size)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _decode_rgba8(data: bytes):
    """Decode a non-interlaced 8-bit RGBA PNG to (w, h, bytearray). Raises otherwise."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    i = 8
    idat = bytearray()
    w = h = None
    while i < len(data):
        ln = struct.unpack(">I", data[i:i + 4])[0]
        typ = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + ln]
        if typ == b"IHDR":
            w, h, bd, ct, _comp, _filt, inter = struct.unpack(">IIBBBBB", body)
            if not (bd == 8 and ct == 6 and inter == 0):
                raise ValueError(f"unsupported PNG (bitdepth={bd} colortype={ct} interlace={inter})")
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        i += 12 + ln
    if w is None:
        raise ValueError("no IHDR")
    raw = zlib.decompress(bytes(idat))
    stride = w * 4
    out = bytearray(w * h * 4)
    prev = bytearray(stride)
    pos = 0
    for row in range(h):
        ft = raw[pos]; pos += 1
        line = bytearray(raw[pos:pos + stride]); pos += stride
        if ft == 1:
            for x in range(4, stride):
                line[x] = (line[x] + line[x - 4]) & 255
        elif ft == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif ft == 3:
            for x in range(stride):
                a = line[x - 4] if x >= 4 else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 255
        elif ft == 4:
            for x in range(stride):
                a = line[x - 4] if x >= 4 else 0
                c = prev[x - 4] if x >= 4 else 0
                line[x] = (line[x] + _paeth(a, prev[x], c)) & 255
        elif ft != 0:
            raise ValueError(f"bad filter type {ft}")
        out[row * stride:(row + 1) * stride] = line
        prev = line
    return w, h, out


async def radar_raining(session, lat: float, lon: float, cfg) -> bool | None:
    """True/False if radar shows precipitation at the point, or None if unavailable.

    None means "couldn't decide from radar" (API/tile error, or a non-RGBA tile);
    the caller should fall through to the next rain source.
    """
    try:
        async with session.get(RAINVIEWER_MAPS_URL, timeout=cfg.request_timeout) as resp:
            resp.raise_for_status()
            maps = await resp.json()
        frames = (maps.get("radar") or {}).get("past") or []
        if not frames or not maps.get("host"):
            return None
        base = maps["host"] + frames[-1]["path"]
        z = cfg.rainviewer_zoom
        xt, yt, px, py = _tile_pixel(lat, lon, z)
        url = f"{base}/{TILE_SIZE}/{z}/{xt}/{yt}/{cfg.rainviewer_color}/0_0.png"
        async with session.get(url, timeout=cfg.request_timeout) as resp:
            resp.raise_for_status()
            png = await resp.read()
        w, h, rgba = _decode_rgba8(png)
        rad = cfg.rainviewer_window
        for dy in range(-rad, rad + 1):
            for dx in range(-rad, rad + 1):
                x = min(w - 1, max(0, px + dx))
                y = min(h - 1, max(0, py + dy))
                if rgba[(y * w + x) * 4 + 3] >= cfg.rainviewer_alpha_min:
                    return True
        return False
    except Exception as exc:  # noqa: BLE001 - radar is best-effort; degrade to next source
        log.warning("rainviewer radar check failed (%s); falling through", exc)
        return None
