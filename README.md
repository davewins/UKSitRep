# UK SitRep

A live, full-screen UK situational-awareness map that brings aviation, maritime,
weather, infrastructure, emergency-service and news feeds together on one display.
It's built for an always-on screen, and the layout also works on a phone.

One small Python (aiohttp) service serves the map and a set of cached JSON feeds.
It runs as a ~70 MB distroless container with no shell, as a non-root user.

![UK SitRep showing aircraft, ships, lifeboat launches, sewage discharges, sea state, rain radar, lightning and the grid mix](docs/screenshot.webp)

## Layers

| Group | What's shown |
|---|---|
| Aviation | Live ADS-B aircraft with trails, labels, full type names, a photo and the scheduled route in the popup. Military aircraft get country flags, and police (NPAS), air ambulance and coastguard aircraft are picked out. Emergency squawks (7500/7600/7700) are flagged. Also: military airfields and army bases, and a guess at where military aircraft are heading |
| Emergency services | Air ambulance / coastguard helicopter "on scene" (inferred from landings away from bases and hospitals), RNLI lifeboat launches, and fire & rescue incidents from Avon, Buckinghamshire, Cheshire, Cleveland, Hampshire & IoW, Royal Berkshire and Shropshire, plus Devon & Somerset (optional), each with its own toggle |
| Maritime | Live AIS ships (Royal Navy, RNLI, coastguard, police and government vessels and distress beacons highlighted), tide times, and sea state for the Shipping Forecast areas |
| Weather | Met Office warnings, rain radar (animated), wind arrows, temperature grid, city weather, lightning |
| Infrastructure | Power cuts (UKPN, SSEN), electricity grid mix and demand, carbon intensity, interconnector flows to and from Europe and Ireland, National Highways closures and live incidents |
| Space & geology | BGS earthquakes, UK Meteor Network meteor tracks, NOAA Kp index, AuroraWatch UK, ISS position, ground track and visible passes from the map centre; when zoomed in, public allsky cameras (Allsky and indi-allsky) with their latest image, and observatories, radio telescopes and planetaria with links to their sites |
| Water | Environment Agency flood warnings; river gauges for England (EA) and Scotland (SEPA) (against their normal range, with rising/falling trend); live sewage storm overflow status from all 11 water companies (discharging now, and stopped within the last 48 hours); bathing water classifications and daily pollution-risk forecasts, cross-referenced with nearby sewage |
| Air & fire | Air quality and pollen (CAMS via Open-Meteo); NASA FIRMS satellite fire hotspots (optional key); NASA GIBS satellite image overlay |
| Transport & crime | Live forecourt fuel prices with brand logos (GOV.UK Fuel Finder, optional credentials) from zoom 11; TfL line disruption in the Info panel; police.uk street crime around the map centre (zoom 14+), falling back to the latest month a force has published |
| Tides | High and low water times, heights and a 30-hour curve for about 700 UK and Irish ports (Admiralty EasyTide), shown when zoomed in |
| Cameras | TfL JamCams, OpenStreetMap-tagged webcams, your own curated list, and the WorldCam directory (optional) |
| News | BBC UK, nations and local feeds, pinned to the places they mention, filtered by severity and age, plus a scrolling ticker |

## Views

The layer panel has a **View** menu that switches many layers at once: *Everything*,
*Military*, *Emergency*, *Transport & infrastructure*, *Weather & water*, *Coast & sea* and
*Space*. *My layers* puts back the set you had before picking a view. Each group heading has
its own checkbox, and *All on* / *All off* sit at the top.

Add `?view=<name>` to the URL to open in a view (`military`, `emergency`, `transport`,
`weather`, `coast`, `space`, `everything`), e.g. for a wall display:
`https://<host>/k/<ACCESS_TOKEN>/?kiosk&view=military`.

## Finding a place

Press **🔍** (or `/`) and type a town, village, postcode (`TQ14 8AA`), postcode district
(`GL20`) or coordinates (`51.5, -0.12`). Suggestions appear as you type; Enter or a click flies
the map there and drops a pin, and ✕ removes it. Place names come from postcodes.io (OS Open
Names, Great Britain only). Other keys: `P` panels, `F` full screen, `H` home view.

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
| `FIRMS_MAP_KEY` | *(none)* | NASA FIRMS key for satellite fire hotspots ([free](https://firms.modaps.eosdis.nasa.gov/api/map_key/)) |
| `FUEL_FINDER_CLIENT_ID`, `FUEL_FINDER_CLIENT_SECRET` | *(none)* | GOV.UK Fuel Finder API credentials for live forecourt prices ([register](https://www.developer.fuel-finder.service.gov.uk/access-latest-fuelprices)) |
| `HOME_LAT`, `HOME_LON`, `HOME_NAME`, `HOME_RADIUS_KM` | *(none)*, `Home`, `25` | Your home area: shows a "Near home" panel (rivers, floods, sewage, beaches, fire, roads, power cuts, lifeboats, lightning, tide, cheapest fuel, ISS, air quality) and a radius ring |
| `HA_WEBHOOK_URL` | *(none)* | Post new nearby events to a Home Assistant webhook (needs the home area), e.g. `http://<ha-ip>:8123/api/webhook/<id>` |
| `NOTIFY` | all | Which events to send: `warning,flood,river,sewage,fire,road,power,rnli,aircraft,hems,lightning,iss` |
| `ENABLE_LIGHTNING` | `true` | Live lightning from Blitzortung.org (private, non-commercial use) |
| `ENABLE_WORLDCAM` | `false` | Import the worldcam.eu directory (scraped; weekly refresh) |
| `FIRE_SERVICES` | all seven | Fire & rescue services read from their public feeds: `avon,bucks,cheshire,cleveland,hants,rbfrs,shropshire`. Remove any you don't want |
| `ENABLE_DSFRS` | `false` | Also show Devon & Somerset FRS incidents. These are read from their incidents web page, so this is off by default |
| `DSFRS_STATION_IDS` | the 10 Teignbridge + Torbay stations | Only used with `ENABLE_DSFRS`: which stations' incidents to fetch (DSFRS station filter ids, comma-separated) |
| `DSFRS_AREAS` | `Teignbridge,Torbay` | Only used with `ENABLE_DSFRS`: keep incidents in these districts (drops calls where those crews helped elsewhere) |
| `CAMERAS_FILE` | bundled `config/cameras.json` | Your curated webcams |
| `PORT`, `DATA_DIR`, `LOG_LEVEL` | `8080`, `/data`, `INFO` | |

## Home Assistant notifications

Set `HOME_LAT`/`HOME_LON` and `HA_WEBHOOK_URL`, then add an automation with a webhook trigger:

```yaml
automation:
  - alias: UK SitRep - nearby event notifications
    mode: queued
    trigger:
      - platform: webhook
        webhook_id: uksitrep-<random>      # the end of HA_WEBHOOK_URL
        allowed_methods: [POST]
        local_only: true
    action:
      - service: notify.mobile_app_<your_phone>
        data:
          title: "{{ trigger.json.title }}"
          message: "{{ trigger.json.message }}"
```

The service checks every 5 minutes and sends each new event once (it remembers what it sent in
`/data/notified.json`). On first start it records what's already happening instead of sending it.

## Embedding in Home Assistant

Add a dashboard in panel view containing a Webpage (iframe) card:

```yaml
type: iframe
url: https://<host>/k/<ACCESS_TOKEN>/
```

Add `?kiosk` to the URL (e.g. `https://<host>/k/<ACCESS_TOKEN>/?kiosk`) to hide the layer panel on a wall display.

## Security notes

* The feeds are fixed, allow-listed sources, so the service can't be used as an open proxy.
* Set `ACCESS_TOKEN` if the service is reachable from outside your network: anyone viewing it uses your AIS key.
* API keys only ever come from environment variables. Never commit `.env`.

## Building one for another country

See [CLAUDE.md](CLAUDE.md): it covers how the project is put together, which constants and
feeds are UK-specific, global feeds you can keep, and how to find and add local ones. It's written
so Claude Code can follow it too.

## Data sources and licences

See [NOTICE.md](NOTICE.md). Most sources are open data (OGL, CC-BY, ODbL). Some are
read from public web pages and are **off by default**; check their terms before enabling them.
