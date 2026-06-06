# matter-weather-sensor

Turn a **free weather API into Matter sensors**.

Point it at a latitude/longitude and it exposes the weather there as ordinary
Matter sensors to [matter_webcontrol](https://github.com/dongnh/matter_webcontrol):

| Sensor | Source | Matter representation |
|--------|--------|-----------------------|
| **Temperature** | Open-Meteo (ECMWF), optional METAR blend | TemperatureMeasurement |
| **Humidity** | Open-Meteo | RelativeHumidityMeasurement |
| **Pressure** | Open-Meteo `pressure_msl`, optional METAR QNH | PressureMeasurement |
| **Rain** | RainViewer radar → METAR → Open-Meteo | BooleanState (contact) or Occupancy |
| **Brightness** | Open-Meteo `shortwave_radiation` → lux | IlluminanceMeasurement |

From there they flow into `/api/sensors`, `/api/climate`, Apple Home, and anything
built on top like [light_programmer](https://github.com/dongnh/light_programmer) —
e.g. "if it's raining, …" or "if it's over 33 °C outside, …".

It's the weather sibling of
[matter-appletv-presence](https://github.com/dongnh/matter-appletv-presence) and
[matter-mac-presence](https://github.com/dongnh/matter-mac-presence): same
logical-bridge / SSE shape, different signal source — here the source is a public
weather API rather than a device on the LAN.

## How it works

One small service runs on an always-on host with outbound internet (the home
server). On an interval it polls the weather API and serves the result; the
matter_webcontrol bridge polls *it* and presents Matter sensors.

```
 weather API            home server (the service)            matter_webcontrol      Apple Home / light_programmer
 ┌──────────┐  https   ┌──────────────────────────┐ /api/bridge ┌──────────────┐  ┌────────────────────────────┐
 │ Open-Meteo│ ◄──────►│ matter-weather-sensor      │ ◄─ip,port─ │ polls this   │─►│ temp / humidity / pressure │
 │ (+ METAR) │  poll   │ serve (logical bridge)     │  poll/SSE   │ bridge,      │  │ rain / brightness, /climate│
 └──────────┘          │ maps reading -> Matter raw │ ──────────►│ exposes them │  └────────────────────────────┘
                       └──────────────────────────┘             └──────────────┘
```

## Sensors

Each entry in `sensors` is one Matter device. Give each its own `id` and a
`fields` list, so a "Rain" sensor and a "Brightness" sensor show up as separate
accessories (or combine fields into one device — your call).

### Temperature / humidity / pressure — and why ECMWF

Measured live against the Noi Bai (VVNB) station observation for Hanoi, the
global models differ a lot:

| Model (Open-Meteo)   | Temp error | Humidity |
|----------------------|-----------:|---------:|
| **ECMWF** (`ecmwf_ifs025`) | **−0.7 °C** | within a few % |
| ICON (`icon_global`) | +0.3 °C | close |
| GFS (`gfs_global`)   | +1.3 °C | too dry |
| JMA (`jma_gsm`)      | +2.2 °C | far too dry |

So the default model is **ECMWF**. Optionally set a `metar_station` (e.g.
`"VVNB"`): the station's **real hourly observation** then overrides the model for
`metar_fields` (default temperature + pressure) — point-truth blended into the
smoother model output. Humidity is left to the model at your exact coordinates.

### Rain

Matter has no precipitation cluster, so rain surfaces as a **binary** "is it
raining now". Because model precipitation is the *weakest* signal for Hanoi's
local convective showers (the ~25 km ECMWF grid mistimes and smears them), rain
is resolved from `rain_sources` in **priority order** — the first source with a
definite answer wins:

1. **`rainviewer`** — real **weather radar** sampled at your exact coordinate via
   [RainViewer](https://www.rainviewer.com)'s free, key-less tiles (Vietnam has
   10 radars in the mosaic). The most accurate "is it raining *here, now*"; ~5–10
   min latency. Falls through if the tile/API is unavailable.
2. **`metar`** — the configured `metar_station`'s **present-weather** report
   (`RA`/`SHRA`/`TSRA` = raining). A real observation, but at the airport and
   hourly. Needs `metar_station` set.
3. **`model`** — Open-Meteo `precipitation` ≥ `rain_threshold_mm` (default `0.1`).
   Always available, so it's the final backstop.

> How it reads radar: RainViewer's free tier serves only raster tiles, so the
> service downloads the radar tile covering your point and reads the pixel there
> (decoding the PNG with the Python stdlib — no extra dependency). A fully opaque
> pixel = a real echo; the faint semi-transparent "trace" layer is ignored.

Choose how the result appears with `rain_state`:

- `"contact"` (default) → a **Contact / BooleanState** sensor; value `1` = raining.
- `"occupancy"` → an **Occupancy** sensor; handy if you want light_programmer's
  occupancy gating to react to rain.

`/api/health` shows which source decided (`rain_source`) and the raw METAR
present-weather (`wx`), so you can see *why* it says rain or dry.

### Brightness (illuminance)

Outdoor brightness comes from Open-Meteo `shortwave_radiation` (W/m²) ×
`lux_per_wm2` (default `120`, the luminous efficacy of daylight) → lux, encoded
with the Matter illuminance log scale. Apple Home shows it as a Light Sensor in
lux; a bright midday is ~80–100k lux, overcast a few thousand, night ~0.

## Set it up

Install (only needs `aiohttp`):

```bash
pip install -e .
```

Copy `bridge.sample.json` to `bridge.json` and set your coordinates. Verify the
readings before serving (prints human values + the raw Matter integers):

```bash
matter-weather-sensor test --config bridge.json
# dev_weather_hanoi    (Hanoi Weather):    33.3C  68%RH  1004hPa
#   matter raw: {'temperature': 3330, 'humidity': 6800, 'pressure': 1004}
# dev_rain_hanoi       (Hanoi Rain):       dry[rainviewer]
#   matter raw: {'contact': 0}
# dev_brightness_hanoi (Hanoi Brightness): 88320lux
#   matter raw: {'illuminance': 49462}
```

Run the service:

```bash
matter-weather-sensor serve --config bridge.json
```

### Register with matter_webcontrol

Once (matter_webcontrol caches it in `bridge_cache.json`):

```bash
curl 'http://<matter-host>:8080/api/bridge?ip=<this-host>&port=8093'
```

The sensors then appear in `/api/sensors`, `/api/devices` and `/api/climate`.

### Run it under launchd (macOS home server)

See [`deploy/home.weather.sensor.plist`](deploy/home.weather.sensor.plist) — edit
the user/paths, copy to `~/Library/LaunchAgents/`, then `bootstrap` + `kickstart`
as noted in the file.

## Config reference

| key | meaning |
|-----|---------|
| `host` / `port` | where this service listens (default `0.0.0.0:8093`). |
| `poll_interval` | seconds between weather refreshes (default `600`). |
| `sensors[].latitude/longitude` | the point to read. |
| `sensors[].fields` | subset of `temperature`, `humidity`, `pressure`, `rain`, `illuminance`. |
| `sensors[].provider` | `open-meteo` (default) or `metar` (station obs; temp/humidity/pressure only). |
| `sensors[].model` | Open-Meteo model id; default `ecmwf_ifs025`. |
| `sensors[].metar_station` / `metar_fields` | blend a real station observation (default temperature, pressure). |
| `sensors[].rain_sources` | priority list for rain, default `["rainviewer","metar","model"]`. |
| `sensors[].rain_threshold_mm` | mm of precipitation that counts as "raining" for the `model` source (default `0.1`). |
| `sensors[].rain_state` | `contact` (default) or `occupancy`. |
| `sensors[].lux_per_wm2` | radiation→lux factor for brightness (default `120`). |
| `rainviewer_zoom` / `rainviewer_color` / `rainviewer_window` / `rainviewer_alpha_min` | radar tile sampling tunables (top-level; defaults `7` / `2` / `1` / `250`). |

## Endpoints

| endpoint | purpose |
|----------|---------|
| `GET /api/devices` | device list with raw Matter `states` (what matter_webcontrol polls). |
| `GET /api/sensor?id=` | single sensor's current states. |
| `GET /api/subscribe?id=` | SSE stream of state changes. |
| `GET /api/health` | human-readable last readings, for debugging. |

## Notes

This reports **outdoor** weather at a coordinate, not a room. Great for
automations and Home display; don't use it as the room sensor that drives your
AC — keep a real indoor climate sensor for that.

Dependencies: `aiohttp` only (the RainViewer PNG tiles are decoded with the
Python stdlib). Data: [Open-Meteo](https://open-meteo.com) (CC-BY, no key),
[aviationweather.gov](https://aviationweather.gov) METAR (US NWS, public domain),
and [RainViewer](https://www.rainviewer.com) radar (free, attribution).
