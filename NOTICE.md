# Data sources & attribution

UK SitRep's own code is MIT-licensed. It pulls data from these sources at runtime.
Each is subject to its own terms.

| Source | Used for | Terms |
|---|---|---|
| [airplanes.live](https://airplanes.live/api-guide/) (default; `ADSB_WIDE_API` can point elsewhere) | UK-wide aircraft positions | airplanes.live API terms: non-commercial use, 1 request/s, no uptime guarantee. TODO: confirm the full terms and any attribution wording at the link |
| Your own receivers (`ADSB_LOCAL_FEEDS`) | Real-time aircraft positions | Your data |
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
| [OpenStreetMap](https://www.openstreetmap.org/copyright) | Lifeboat station positions (`uksitrep/data/rnli_stations.json`), webcams (seed copy in `uksitrep/data/osm_webcams.json`), observatories and telescopes (seed copy in `uksitrep/data/observatories.json`), cash machines (`amenity=atm`), post boxes (`amenity=post_box`), defibrillators (`emergency=defibrillator`), life rings and lifeguard posts (`emergency=life_ring`, `emergency=lifeguard`), hospitals (`amenity=hospital`) and public toilets (`amenity=toilets`), each downloaded weekly for the UK only (ISO 3166-1 GB boundary) from Overpass into `DATA_DIR` | © OpenStreetMap contributors, ODbL |
| [postcodes.io](https://postcodes.io) / OS Open Names | Geocoding, place search and the nearest postcode to a dropped pin | OGL |
| [TfL Unified API](https://api.tfl.gov.uk) | JamCams | Powered by TfL Open Data |
| [tar1090](https://github.com/wiedehopf/tar1090), [tar1090-db](https://github.com/wiedehopf/tar1090-db) | Aircraft type names, ICAO country ranges (downloaded at runtime, not bundled) | GPL |
| [flagcdn.com](https://flagcdn.com) | Flag images | Public domain flags |
| [Esri](https://www.esri.com) basemaps, [OpenStreetMap tiles](https://operations.osmfoundation.org/policies/tiles/) | Base maps | Esri terms; OSM tile usage policy (light personal use) |
| Water company storm overflow feeds via [Stream](https://www.streamwaterdata.co.uk) (Anglian, Northumbrian, Severn Trent, Southern, South West, Thames, United Utilities, Wessex, Yorkshire), Dŵr Cymru Welsh Water, Scottish Water | Live sewage discharges | © the respective water company, CC BY 4.0 / OGL |
| [Environment Agency](https://environment.data.gov.uk/flood-monitoring/doc/reference) river gauges, [bathing water quality](https://environment.data.gov.uk/bwq/) | River levels, beaches | OGL v3 |
| [Blitzortung.org](https://www.blitzortung.org) | Lightning strikes | Private, non-commercial use; volunteer network. Off by default (`ENABLE_LIGHTNING`): data access is for registered station owners |
| [Open-Meteo air quality](https://open-meteo.com/en/docs/air-quality-api) (Copernicus CAMS) | Air quality & pollen | CC BY 4.0 |
| [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov) *(optional key)*, [NASA GIBS](https://www.earthdata.nasa.gov/engage/open-data-services-software/earthdata-developer-portal/gibs-api) | Fire hotspots, satellite imagery | NASA open data; please acknowledge LANCE FIRMS / GIBS |
| [Allsky camera map](https://www.thomasjacquin.com/allsky-map/) (Thomas Jacquin's Allsky project) | Locations, links and latest images of allsky cameras whose owners chose to show them on the map; images are loaded from the owners' own sites | Owners' content; links and images shown as published, not stored |
| [allsky-map.com](https://allsky-map.com) ([TN-1/allsky-map](https://github.com/TN-1/allsky-map), for [indi-allsky](https://github.com/aaronwmorris/indi-allsky)) | indi-allsky camera locations, online status and latest image | Owners' content, served by allsky-map.com; not stored |
| [Jodrell Bank webcams](https://www.jodrellbank.manchester.ac.uk/about/webcams/) (University of Manchester) | Live snapshot images of the Lovell, Mark II and 7m telescopes, shown in the Jodrell Bank observatory popup and linked back to their page. No terms stated; hotlinked, not stored. |
| [Google Fonts](https://fonts.google.com) (see "Styles (themes)" below for the list) | Fonts for the optional styles, loaded only when one is picked | SIL Open Font License / Apache 2.0 |
| [Rail Data Marketplace](https://raildata.org.uk) (Rail Delivery Group): Live Departure Board (Darwin) and National Rail Disruptions | Departure boards, delays, cancellations, disruption bulletins | Rail Data Marketplace licence terms for each product (free); requires your own keys |
| [Bus Open Data Service](https://data.bus-data.dft.gov.uk) (DfT) | Live bus positions and bus disruptions | OGL v3; requires your own key |
| [NESO Carbon Intensity API](https://carbonintensity.org.uk) | Regional carbon intensity, mix and forecast | CC BY 4.0 |
| [NESO Data Portal](https://www.neso.energy/data-portal/gis-boundaries-gb-dno-license-areas): GB DNO licence areas | Region outlines (simplified extract in `uksitrep/data/energy_regions.json`) | NESO Open Data Licence |
| [ONS Open Geography Portal](https://geoportal.statistics.gov.uk): countries, regions, counties and unitary authorities, local authority districts, national parks (BUC) | Area search and outlines (simplified extract in `uksitrep/data/areas.json`) | OGL v3; contains OS data © Crown copyright and database right |
| [Sheffield Solar PV_Live](https://www.solar.sheffield.ac.uk/pvlive/) | Regional solar generation estimates | Sheffield Solar terms (free, with attribution) |
| [Octopus Energy API](https://developer.octopus.energy) | Agile half-hourly prices by region | Octopus public API |
| [Elexon BMRS / Insights](https://bmrs.elexon.co.uk) | Physical notifications per generating unit; unit reference data | Elexon BMRS data licence (attribution) |
| [OSUKED Power Station Dictionary](https://github.com/OSUKED/Power-Station-Dictionary) | Generator positions and unit ids (extract in `uksitrep/data/power_plants.json`) | MIT |
| [Open Charge Map](https://openchargemap.org) | EV chargers | CC BY 4.0 (most data); requires your own key |
| [Traveline National Operator Codes](https://www.travelinedata.org.uk/traveline-open-data/transport-operations/) | Bus operator names (extract in `uksitrep/data/bus_operators.json`) | Traveline open data terms |
| [NaPTAN](https://www.data.gov.uk/dataset/ff93ffc1-6656-47d8-9155-85ea0b8f2251/national-public-transport-access-nodes-naptan) (DfT) | Station positions and codes (extract in `uksitrep/data/rail_stations.json`) | OGL v3 |
| [CelesTrak](https://celestrak.org) + [python-sgp4](https://pypi.org/project/sgp4/) | ISS orbit | CelesTrak terms; MIT |
| [TfL Unified API](https://api.tfl.gov.uk) | Line status | Powered by TfL Open Data |
| [GOV.UK Fuel Finder](https://www.developer.fuel-finder.service.gov.uk) *(optional credentials)* | Fuel prices | Motor Fuel Price (Open Data) Regulations 2025; Fuel Finder terms |
| [adsbdb](https://www.adsbdb.com), [Planespotters.net](https://www.planespotters.net/photo/api), airport-data.com | Aircraft routes and photos (fetched when a popup opens) | adsbdb terms; photos © their photographers, credited and linked |
| [Wikidata](https://www.wikidata.org) and [Wikimedia Commons](https://commons.wikimedia.org) | Ship photos, found by IMO or MMSI number (fetched when a popup opens) | Wikidata CC0; each photo under its own licence (CC BY-SA, CC0 and so on), credited with author and licence and linked to its Commons page |
| [SEPA](https://timeseriesdoc.sepa.org.uk) KiWIS | Scottish river levels | OGL v3 |
| [Elexon BMRS FUELINST](https://bmrs.elexon.co.uk) | Interconnector flows | Elexon licence |
| [data.police.uk](https://data.police.uk) | Street-level crime | OGL v3 |
| [Admiralty EasyTide](https://easytide.admiralty.co.uk) (UKHO) | Tide times. Fetched per port when a popup is opened, cached 6 h | Crown copyright. Not for navigation |
| [seaglasshunting.com](https://seaglasshunting.com) *(optional, `ENABLE_SEAGLASS`)* | Weekly sea glass hunting Top 25 (ShoreSense score) and beach positions | Their content (© Seaglass Hunting), read from public pages; linked and credited, not stored beyond the latest copy. Check their terms before enabling |
| worldcam.eu *(optional)* | Webcam directory | Scraped. Check their terms before enabling |
| Avon, Buckinghamshire, Cheshire, Cleveland, Hampshire & IoW, Royal Berkshire and Shropshire fire & rescue services | Incident reports (their public RSS / website JSON), geocoded with postcodes.io | Each service's website terms |
| Devon & Somerset FRS *(optional)* | Incident pages | Scraped. Check their terms before enabling |

## Styles (themes)

The Layers → Style options are original homages to familiar visual genres. They contain no logos, artwork, music, dialogue,
character or product names from any film, TV show or game, and use only generic names and original wording. Fonts come from
[Google Fonts](https://fonts.google.com) under the SIL Open Font License or Apache licence (Share Tech Mono, VT323, Orbitron, Exo 2,
Oxanium, Antonio, Chakra Petch, Rajdhani, Cinzel, Jost, Audiowide, Poiret One, Josefin Sans, Inter, Michroma, Titillium Web, News Cycle,
Stardos Stencil, IM Fell English, MedievalSharp, Quicksand, Special Elite, Playfair Display SC, Baloo 2). Any resemblance to a
particular work is stylistic only. The short style keys in URLs (for example `?theme=matrix`) are internal identifiers kept for
backward compatibility. All trademarks belong to their owners; no affiliation or endorsement is implied.
