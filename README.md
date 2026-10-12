# UK SitRep

A live, full-screen UK situational-awareness map that brings aviation, maritime,
weather, infrastructure, emergency-service and news feeds together on one display.
It's built for an always-on screen, and the layout also works on a phone.

One small Python (aiohttp) service serves the map and a set of cached JSON feeds.
It runs as a ~70 MB distroless container with no shell, as a non-root user.

## Layers

| Group | What's shown |
|---|---|
| Aviation | Live ADS-B aircraft with trails, labels and full type names. Military aircraft get country flags, and police (NPAS), air ambulance and coastguard aircraft are picked out. Emergency squawks (7500/7600/7700) are flagged. Also: military airfields and army bases, and a guess at where military aircraft are heading |
| Emergency services | Air ambulance / coastguard helicopter "on scene" (inferred from landings away from bases and hospitals), RNLI lifeboat launches, and Devon & Somerset FRS incidents (optional) |
| Maritime | Live AIS ships. Royal Navy, RNLI, coastguard, police and government vessels and distress beacons are highlighted |
| Weather & sea | Met Office warnings, Environment Agency floods, rain radar (animated), wind arrows, temperature grid, city weather, sea state for the Shipping Forecast areas |
| Infrastructure | Power cuts (UKPN, SSEN), electricity grid mix and demand, carbon intensity, National Highways closures and live incidents |
| Space & geology | BGS earthquakes, UK Meteor Network meteor tracks, NOAA Kp index, AuroraWatch UK |
| Tides | High and low water times, heights and a 30-hour curve for about 700 UK and Irish ports (Admiralty EasyTide), shown when zoomed in |
| Cameras | TfL JamCams, OpenStreetMap-tagged webcams, your own curated list, and the WorldCam directory (optional) |
| News | BBC UK, nations and local feeds, pinned to the places they mention, filtered by severity and age, plus a scrolling ticker |

## Quick start

```bash
cp .env.example .env        # add keys / ACCESS_TOKEN
docker compose up -d        # then open http://<host>:8080/?token=<ACCESS_TOKEN>
```

Or build it yourself: `docker build -t uksitrep .`

**Portainer:** create a stack from this repository (or paste `docker-compose.yml` into the web
editor) and add the settings under *Environment variables*. The compose file's `environment:`
block passes them into the container. Set `HOST_PORT` to publish on a port other than 8080.
Check `http://<host>:<port>/healthz`: its `features` list shows which settings reached the container.

Without Docker (Python 3.11+): `pip install -r requirements.txt && DATA_DIR=./data python -m uksitrep`

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `ACCESS_TOKEN` | *(none — open)* | Require the token on every request: `/?token=…`, `/k/<token>/` (path form, for apps that drop query strings, e.g. the HA iOS app), or `Authorization: Bearer …` |
| `AISSTREAM_API_KEY` | — | Live ships ([aisstream.io](https://aisstream.io), free). Connects only while someone is viewing |
| `NATIONAL_HIGHWAYS_KEY`, `NATIONAL_HIGHWAYS_KEY_SECONDARY` | — | Road closures and incidents ([developer portal](https://developer.data.nationalhighways.co.uk)) |
| `BBC_LOCAL_REGIONS` | — | Extra BBC England local feeds, e.g. `gloucestershire,devon` |
| `ENABLE_BACKGROUND_ADSB` | `true` | Poll ADS-B continuously so on-scene detection works with no viewer |
| `ENABLE_WORLDCAM` | `false` | Import the worldcam.eu directory (scraped; weekly refresh) |
| `ENABLE_DSFRS` | `false` | Devon & Somerset FRS incidents (scraped) |
| `DSFRS_STATION_IDS`, `DSFRS_AREAS` | Teignbridge + Torbay stations | Which stations and districts to show |
| `CAMERAS_FILE` | bundled `config/cameras.json` | Your curated webcams |
| `PORT`, `DATA_DIR`, `LOG_LEVEL` | `8080`, `/data`, `INFO` | |

## Embedding in Home Assistant

Add a dashboard in panel view containing a Webpage (iframe) card:

```yaml
type: iframe
url: https://<host>/k/<ACCESS_TOKEN>/
```

Add `&kiosk` to the URL to hide the layer panel on a wall display.

## Security notes

* The feeds are fixed, allow-listed sources, so the service can't be used as an open proxy.
* Set `ACCESS_TOKEN` if the service is reachable from outside your network: anyone viewing it uses your AIS key.
* API keys only ever come from environment variables. Never commit `.env`.

## Data sources and licences

See [NOTICE.md](NOTICE.md). Most sources are open data (OGL, CC-BY, ODbL). Some are
read from public web pages and are **off by default**; check their terms before enabling them.
