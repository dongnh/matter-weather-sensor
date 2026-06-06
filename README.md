# matter-weather-sensor

Turn a **free weather API into Matter sensors**.

Point it at a latitude/longitude and it exposes the weather there as ordinary
Matter sensors to [matter_webcontrol](https://github.com/dongnh/matter_webcontrol):

| Sensor | Source | Matter representation |
|--------|--------|-----------------------|
| **Temperature** | Open-Meteo (ECMWF), optional METAR blend | TemperatureMeasurement |
| **Humidity** | Open-Meteo | RelativeHumidityMeasurement |
| **Pressure** | Open-Meteo `pressure_msl`, optional METAR QNH | PressureMeasurement |
| **Rain** | Open-Meteo `precipitation` ≥ threshold | BooleanState (contact) or Occupancy |
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
raining now": Open-Meteo `precipitation` (mm) ≥ `rain_threshold_mm` (default
`0.1`). Choose how it appears with `rain_state`:

- `"contact"` (default) → a **Contact / BooleanState** sensor; value `1` = raining.
- `"occupancy"` → an **Occupancy** sensor; handy if you want light_programmer's
  occupancy gating to react to rain.

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
# dev_rain_hanoi       (Hanoi Rain):       dry(0.0mm)
#   matter raw: {'contact': 0}
# dev_brightness_hanoi (Hanoi Brightness): 88440lux
#   matter raw: {'illuminance': 49467}
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
| `sensors[].rain_threshold_mm` | mm of precipitation that counts as "raining" (default `0.1`). |
| `sensors[].rain_state` | `contact` (default) or `occupancy`. |
| `sensors[].lux_per_wm2` | radiation→lux factor for brightness (default `120`). |

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

Dependencies: `aiohttp`. Data: [Open-Meteo](https://open-meteo.com) (CC-BY, no
key) and [aviationweather.gov](https://aviationweather.gov) METAR (US NWS, public
domain).
