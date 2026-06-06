# matter-weather-sensor

Free weather APIs, exposed as Matter sensors.

Point it at a latitude and longitude. Run it on the home server. The weather at that point shows up in Apple Home, in `matter_webcontrol`, and in anything built on top — as ordinary Matter temperature, humidity, pressure, rain, and illuminance sensors.

## Overview

One small service, one outbound internet connection, one config file. It polls public weather APIs on an interval and presents the result as a Matter logical bridge. The `matter_webcontrol` host polls the bridge and surfaces the readings on `/api/sensors`, `/api/climate`, and Apple Home. Consumers like `light_programmer` then gate automations on real outdoor conditions — raining, bright, hot — without a single hardware sensor outside.

It is the weather sibling of `matter-appletv-presence` and `matter-mac-presence`. Same logical-bridge shape, same SSE contract. The signal source is a public weather API instead of a device on the LAN.

## How it works

Each Matter sensor corresponds to a coordinate and a set of fields. Temperature, humidity, and pressure come from Open-Meteo, with ECMWF (`ecmwf_ifs025`) as the default model — it tracks the Noi Bai (VVNB) observation in Hanoi within a degree, while GFS and JMA drift two degrees warm and several percent dry. A METAR station can be named to override the model for temperature and pressure with the real hourly observation; humidity stays on the model at the exact coordinate.

Rain is harder. Matter has no precipitation cluster, and 25 km model grids mistime convective showers. The service resolves rain in priority order and takes the first definite answer. Weather radar from RainViewer is sampled at the exact coordinate first — the most accurate "is it raining here, now" at five to ten minute latency. METAR present-weather is the fallback when radar is unavailable. Open-Meteo precipitation is the final backstop. The radar tile is a PNG; the service decodes it with the Python standard library, with no extra dependency, and reads the colour of the pixel under the coordinate. That colour maps to dBZ and to a rain rate via the Marshall–Palmer relation, then buckets to light, moderate, heavy, or violent. The binary contact stays binary; the intensity, rate, and dBZ ride alongside as informational fields.

Brightness comes from Open-Meteo shortwave radiation, converted to lux through a configurable luminous efficacy and encoded with the Matter illuminance log scale. Apple Home displays it as a Light Sensor.

The rain channel can present as Matter's dedicated Rain Sensor (device type 0x0044, the default), as a Contact sensor, or as an Occupancy sensor — whichever shape the downstream consumer expects.

## Sensors

- Temperature — Open-Meteo ECMWF, optional METAR station blend.
- Humidity — Open-Meteo at the exact coordinate.
- Pressure — Open-Meteo MSL, optional METAR QNH blend.
- Rain — RainViewer radar, with METAR and model as fallbacks; intensity surfaced as light, moderate, heavy, or violent.
- Brightness — Open-Meteo shortwave radiation, mapped to lux.

## Installation

The only dependency is `aiohttp`. Install the package, copy `bridge.sample.json` to `bridge.json`, set the coordinates and field list, and verify the readings with the bundled `test` subcommand before going live — it prints human values next to the raw Matter integers. Then run the service. Register it with `matter_webcontrol` once; the host caches the bridge and the sensors appear on `/api/sensors`, `/api/devices`, and `/api/climate`. On macOS, the included `deploy/home.weather.sensor.plist` runs the service under launchd. The default listening port is 8093.

A short reference of every config key — poll interval, model id, METAR station and fields, rain sources and threshold, rain state shape, radar sampling tunables — lives in `bridge.sample.json` alongside the defaults.

## A note on scope

This reports outdoor weather at a point. It is the right input for automations and for Home display. It is not a room sensor, and it should not be the climate input that drives an air conditioner. Keep a real indoor sensor for that.

## Related projects

- [matter_webcontrol](https://github.com/dongnh/matter_webcontrol) — the Matter host this bridge registers with.
- [light_programmer](https://github.com/dongnh/light_programmer) — schedule engine that gates rain overrides on the `rain` sensor exposed here.
- [matter-appletv-presence](https://github.com/dongnh/matter-appletv-presence) and [matter-mac-presence](https://github.com/dongnh/matter-mac-presence) — sibling logical bridges for presence.

Data from [Open-Meteo](https://open-meteo.com) (CC-BY), [aviationweather.gov](https://aviationweather.gov) METAR (US NWS, public domain), and [RainViewer](https://www.rainviewer.com) radar (free, attribution).
