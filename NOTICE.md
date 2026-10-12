# Data sources & attribution

UK SitRep's own code is MIT-licensed. It pulls data from these sources at runtime.
Each is subject to its own terms.

| Source | Used for | Terms |
|---|---|---|
| [adsb.lol](https://adsb.lol) | Aircraft positions | ODbL 1.0 |
| [aisstream.io](https://aisstream.io) | Ship positions (needs your key) | aisstream.io terms |
| [Met Office](https://www.metoffice.gov.uk) RSS | Weather warnings | Crown copyright |
| [Environment Agency](https://environment.data.gov.uk/flood-monitoring/doc/reference) | Flood warnings | OGL v3 |
| [Open-Meteo](https://open-meteo.com) | Weather, wind, sea state | CC BY 4.0; free tier is non-commercial |
| [RainViewer](https://www.rainviewer.com/api.html) | Rain radar tiles | RainViewer terms |
| [Elexon BMRS](https://bmrs.elexon.co.uk), [Carbon Intensity API](https://carbonintensity.org.uk) | Grid data | Elexon licence / CC BY 4.0 |
| [UK Power Networks](https://ukpowernetworks.opendatasoft.com), SSEN PowerTrack | Power cuts | UKPN open data licence / SSEN |
| [National Highways](https://developer.data.nationalhighways.co.uk) | Road closures and incidents | National Highways API terms |
| [NOAA SWPC](https://www.swpc.noaa.gov), [AuroraWatch UK](https://aurorawatch.lancs.ac.uk) | Space weather | Public domain / CC BY-NC-SA |
| [British Geological Survey](https://earthquakes.bgs.ac.uk) | Earthquakes | BGS © UKRI |
| [UK Meteor Network](https://ukmeteornetwork.org/our-data-apis/) | Meteor trajectories | UKMON |
| [BBC News](https://www.bbc.co.uk/news) RSS | Headlines | Personal, non-commercial use |
| [RNLI](https://rnli.org) launches feed | Lifeboat launches | RNLI |
| [OpenStreetMap](https://www.openstreetmap.org/copyright) | Lifeboat station positions (`uksitrep/data/rnli_stations.json`), webcams (seed copy in `uksitrep/data/osm_webcams.json`) | © OpenStreetMap contributors, ODbL |
| [postcodes.io](https://postcodes.io) / OS Open Names | Geocoding | OGL |
| [TfL Unified API](https://api.tfl.gov.uk) | JamCams | Powered by TfL Open Data |
| [tar1090](https://github.com/wiedehopf/tar1090), [tar1090-db](https://github.com/wiedehopf/tar1090-db) | Aircraft type names, ICAO country ranges (downloaded at runtime, not bundled) | GPL |
| [flagcdn.com](https://flagcdn.com) | Flag images | Public domain flags |
| [Esri](https://www.esri.com) basemaps, [OpenStreetMap tiles](https://operations.osmfoundation.org/policies/tiles/) | Base maps | Esri terms; OSM tile usage policy (light personal use) |
| Water company storm overflow feeds via [Stream](https://www.streamwaterdata.co.uk) (Anglian, Northumbrian, Severn Trent, Southern, South West, Thames, United Utilities, Wessex, Yorkshire), Dŵr Cymru Welsh Water, Scottish Water | Live sewage discharges | © the respective water company, CC BY 4.0 / OGL |
| [Environment Agency](https://environment.data.gov.uk/flood-monitoring/doc/reference) river gauges, [bathing water quality](https://environment.data.gov.uk/bwq/) | River levels, beaches | OGL v3 |
| [Blitzortung.org](https://www.blitzortung.org) | Lightning strikes | Private, non-commercial use; volunteer network |
| [Open-Meteo air quality](https://open-meteo.com/en/docs/air-quality-api) (Copernicus CAMS) | Air quality & pollen | CC BY 4.0 |
| [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov) *(optional key)*, [NASA GIBS](https://www.earthdata.nasa.gov/engage/open-data-services-software/earthdata-developer-portal/gibs-api) | Fire hotspots, satellite imagery | NASA open data; please acknowledge LANCE FIRMS / GIBS |
| [CelesTrak](https://celestrak.org) + [python-sgp4](https://pypi.org/project/sgp4/) | ISS orbit | CelesTrak terms; MIT |
| [TfL Unified API](https://api.tfl.gov.uk) | Line status | Powered by TfL Open Data |
| [data.police.uk](https://data.police.uk) | Street-level crime | OGL v3 |
| [Admiralty EasyTide](https://easytide.admiralty.co.uk) (UKHO) | Tide times. Fetched per port when a popup is opened, cached 6 h | Crown copyright. Not for navigation |
| worldcam.eu *(optional)* | Webcam directory | Scraped. Check their terms before enabling |
| Devon & Somerset FRS *(optional)* | Incident pages | Scraped. Check their terms before enabling |
