# CLAUDE.md: working on UK SitRep, or building one for another country

This file briefs Claude Code (and humans) on how this project is built, the rules it follows,
and how to adapt it to a different country. Read it before changing code.

UK SitRep is a single-page live map backed by a small Python service. The service fetches
public data feeds, caches them, and serves them as JSON to the page. Everything country-specific
lives in a handful of constants and feed functions, so a port is mostly "swap the feeds".

## Architecture

```
browser (uksitrep/static/index.html: Leaflet, no build step)
   │  GET api/<feed>        (relative URLs, so it works under /k/<token>/ and behind proxies)
   ▼
aiohttp server (uksitrep/server.py)
   │  auth middleware (ACCESS_TOKEN), routes, security headers
   ▼
Situation (uksitrep/feeds.py) + mixins (more_feeds.py, fire_services.py)
   │  one async method per feed, each wrapped in a TTL Cache with per-key locks
   ▼
fixed, allow-listed upstream URLs (never an open proxy)
```

| File | What's in it |
|---|---|
| `uksitrep/server.py` | Routes, token auth (`?token=`, `/k/<token>/`, Bearer), `/healthz`, startup tasks |
| `uksitrep/config.py` | `Config.from_env()`: every environment variable |
| `uksitrep/feeds.py` | `Cache`, `Situation`, core feeds (aircraft, AIS relay, weather, grid, roads, news, sewage, tides, webcams…) and the `handlers` map |
| `uksitrep/more_feeds.py` | `MoreFeeds` mixin: rivers, lightning relay, air quality, bathing water, ISS (sgp4), FIRMS, TfL, fuel, crime |
| `uksitrep/fire_services.py` | `FireServices` mixin: fire & rescue feeds, text-to-place geocoding restricted to a bounding box |
| `uksitrep/lookups.py` | Aircraft type names and ICAO-address-to-country tables, downloaded at startup (GPL, not bundled) |
| `uksitrep/places.py`, `hems_sites.py` | Gazetteer for pinning news to places; hospital/base sites for air-ambulance "on scene" inference |
| `uksitrep/data/` | Small bundled seeds (lifeboat stations, an OSM webcam snapshot) |
| `uksitrep/static/index.html` | The whole front end: settings, layers, renderers, refresh loop, views |
| `config/cameras.json` | Hand-curated webcams |
| `tools/smoke/` | jsdom smoke test (see Testing) |

## Anatomy of a feed (the checklist for adding one)

Server side:
1. A method on `Situation` or a mixin that returns a dict and uses the cache:
   ```python
   async def rivers(self) -> dict:
       async def fetch() -> dict:
           d = await self._json(URL, params={...})          # _json/_text add UA + timeout
           ...normalise to small, flat records with lat/lon...
           return {"updated": datetime.now(timezone.utc).isoformat(), "gauges": out}
       return await self.cache.get("rivers", 600, fetch)  # TTL in seconds
   ```
   Errors raised inside `fetch` are caught by the cache: viewers get the last good value or
   `{"error": ...}`. Return `{"error": "no_key", ...}` when an optional key isn't set.
2. Add it to `Situation.handlers` (or special-case it in `server.feed` if it takes query
   parameters, like `tides`, `crime`, `iss`, `fuel`).
3. New settings go in `Config` / `from_env()`, `docker-compose.yml` (`environment:` block),
   `.env.example`, the README configuration table, and `/healthz` features if it's a key.

Front end (`index.html`):
4. A layer key in `DEFAULTS.layers` (a Leaflet layer group `G[key]` is made for each).
5. A row in `LAYER_UI` (group, label, legend colour, optional nested children).
6. A renderer `renderX(d)` that keeps `lastX`, clears `G.x`, draws markers, sets `counts.x`.
7. An entry in `refresh` and `INTERVAL`; a chip name in `renderChips`; a count in `countFor`.
8. Zoom-gated or bbox-driven layers re-render on `moveend` (see webcams, crime, fuel).
9. Hook it into `afterToggle()` if toggling needs a redraw, and add it to `VIEWS` where it fits.

Docs: README layer table, `NOTICE.md` (source, use, licence), version in `__init__.py`.

## Porting to another country

Work through these in order. Keep the generic machinery; replace the data.

**1. Geography and defaults**
- `feeds.py`: `BBOX` (S, W, N, E), `UK_TZ`, `ADSB_POINTS` (≤250 nm circles covering the country).
- `index.html`: `HOME()` (centre and zoom), `BASEMAPS` if you want local styles.
- `more_feeds.py`: `BLITZ_BOX`, `AIR_POINTS`, `FIRMS_BOX`.
- `feeds.py`: `CITY_WEATHER`, `SEA_POINTS` (offshore points, one per sea area).

**2. Global feeds that work anywhere (keep these)**
adsb.lol (aircraft), aisstream.io (ships, free key), Open-Meteo (weather, marine, air quality),
RainViewer (radar tiles), NASA FIRMS (fires, free key) and GIBS (imagery), CelesTrak + sgp4 (ISS),
Blitzortung (lightning; coverage varies by region), NOAA SWPC (space weather), OpenStreetMap
Overpass (webcams and other tagged features), tar1090 lookups. Re-check each one's terms and
coverage for your country.

**3. Country-specific feeds to replace** (UK source → what to look for)

| UK feed | Look for |
|---|---|
| Met Office warnings RSS (`MET_REGIONS`) | National weather service warnings, ideally CAP/XML. In Europe, MeteoAlarm publishes every member's warnings as CAP feeds; in the US, `api.weather.gov` alerts |
| Environment Agency floods and river gauges | National hydrology / flood agency APIs (gauges with "typical range" metadata are ideal) |
| BGS earthquakes | National geological survey, or USGS / EMSC global feeds filtered to your bbox |
| Elexon / Carbon Intensity (grid) | Transmission operator open data; ENTSO-E Transparency (Europe, free token) |
| UKPN / SSEN power cuts | Distribution network operators' outage maps (often ArcGIS or JSON behind the map) |
| National Highways DATEX II | Road authority's DATEX II / open traffic feed |
| Water company storm overflows | Water utilities' live discharge maps (often ArcGIS FeatureServers) |
| EA bathing water | EU countries report bathing water to the EEA; many have national APIs |
| Admiralty EasyTide | National hydrographic office tide predictions |
| BBC RSS (`BBC_FEEDS`, `NEWS_HIGH/MED`) | A national broadcaster's RSS; rewrite the keyword regexes and `places.py` gazetteer for your language |
| postcodes.io (geocoding) | A national gazetteer API, or Nominatim (keep to 1 request/second and cache hard) |
| police.uk, TfL, Fuel Finder, RNLI, fire services | Each has a local equivalent, or none: drop the layer rather than fake it |

**4. Aviation and military details**
- `AIRCRAFT_RULES`, `SAR_REG` (callsign/registration patterns for police, air ambulance, SAR).
- `AIRFIELDS`, `ARMY` in `index.html`; `HEMS_SITES` (hospitals and bases, used to avoid false
  "on scene" alerts); `TYPE_OVERRIDES` / `HELI_TYPES` in `lookups.py`.
- Emergency squawks are international; keep them.

**5. Rename**: package name, page title, icons (`static/icons/`), image name in the workflow and
compose file, README/NOTICE.

## Finding feeds: techniques that worked

- **ArcGIS Online search**: many agencies publish live data as FeatureServers. Search
  `https://www.arcgis.com/sharing/rest/search?q=<words>&f=json`, then query a layer with
  `/0/query?where=1=1&outFields=*&outSR=4326&f=json`. Page with `resultOffset`; respect
  `maxRecordCount`; filter server-side with `where` (e.g. only active or recent records).
- **WordPress sites**: `<site>/wp-json/wp/v2/types` lists custom post types (e.g. `incident`);
  `/wp-json/wp/v2/<rest_base>?per_page=40&_embed=wp:term` returns structured posts with taxonomy names.
- **RSS**: look for `<link rel="alternate" type="application/rss+xml">` in a page's head.
- **National open-data portals** (CKAN): `/api/3/action/package_search?q=...`.
- **A site's own JS bundle** can reveal its data endpoint. If that endpoint is a private
  backend (e.g. an app's Firebase), don't use it: find the open data it's built on instead
  (SAS's sewage alerts → the water companies' own feeds).
- **Check freshness**: some feeds look fine but stopped updating months ago. Look at the newest
  record's date before building on it.

## Rules this project follows

- **No open proxy.** Upstream URLs are fixed in code. User input only selects among them or fills
  validated parameters (ids matched by regex, lat/lon range-checked, bbox parsed as floats).
- **Be a polite client.** Cache every feed (minutes to days). Identify with a User-Agent.
  Back off on 429 (see the adsb.lol handling) and honour documented limits (Fuel Finder: one
  request at a time). Fetch large datasets once on the server and send viewers only their bbox.
- **Never make viewers wait on a slow upstream.** Serve a saved copy and refresh in the
  background (OSM webcams, WorldCam, river stations, fuel forecourts).
- **Keys come only from environment variables.** Never commit them; `.env` is gitignored.
- **Scraped or unofficial sources are off by default** and flagged in `NOTICE.md` (WorldCam, DSFRS).
  Prefer official open data; record every source and licence in `NOTICE.md`.
- **Say when data is old or approximate.** Label fallbacks (crime "older data"), approximate
  locations ("placed at the station"), and model data ("not for navigation").
- **Write for an always-on screen and a phone.** No layer should flood the map at country zoom:
  gate dense layers by zoom or show only the noteworthy subset (e.g. raised river gauges).

## Running and testing

```bash
pip install -r requirements.txt
ENABLE_BACKGROUND_ADSB=false ENABLE_LIGHTNING=false DATA_DIR=./data python -m uksitrep   # :8080
python3 -c "s=open('uksitrep/static/index.html').read(); open('page.js','w').write(s[s.rindex('<script>')+8:s.rindex('</script>')])" && node --check page.js
cd tools/smoke && npm install && node smoke.js http://localhost:8080/ 50.55,-3.5,11
```

- **Switch background polling off for local runs.** A forgotten dev server polling adsb.lol
  every few seconds gets your whole IP rate-limited.
- `smoke.js` loads the real page in jsdom, waits for the feeds, optionally zooms to a point,
  and prints each feed's status, marker counts per layer, and any page errors (exit 1 on failure).
- Test feed methods directly by constructing `Situation(Config.from_env(), session)` in a script.
- Check `/healthz` on a deployment: `features` shows which settings reached the container.

## Deployment notes and gotchas

- The image is distroless Python **3.11**: no 3.12-only syntax (e.g. nested same-quote f-strings).
- CI (`.github/workflows/docker.yml`) builds amd64 + arm64 and pushes to GHCR on every push to main.
- Portainer stacks: variables only reach the container through the compose `environment:` block.
- Basemaps: CARTO now needs a key; OSM's tile servers block requests without a Referer, which is
  why the server sends `Referrer-Policy: strict-origin-when-cross-origin`.
- Embedding in iframes (Home Assistant): Safari blocks third-party cookies and some apps drop
  query strings, hence the token in the path (`/k/<token>/`) and no cookies. The proxy in front
  must not send `X-Frame-Options: SAMEORIGIN`.
- Leaflet uses the canvas renderer (`preferCanvas`), so circle markers are cheap; divIcon markers
  are DOM elements: keep their count down (zoom-gate them).
