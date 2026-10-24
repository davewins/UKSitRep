"""aiohttp web server: serves the map page and the /api/<feed> JSON endpoints."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

from . import USER_AGENT, __version__, lookups
from .config import Config
from .feeds import Situation
from .more_feeds import OSM_POINTS

_LOGGER = logging.getLogger("uksitrep")
STATIC = Path(__file__).parent / "static"
ICONS = STATIC / "icons"
# Icons are fetched by browsers without our token, so they skip auth
PUBLIC_PATHS = {"/healthz", "/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
                "/apple-touch-icon-120x120.png", "/apple-touch-icon-120x120-precomposed.png"}
CFG = web.AppKey("cfg", Config)
SIT = web.AppKey("situation", Situation)
LOOKUPS = web.AppKey("lookups", dict)


@web.middleware
async def compress_middleware(request: web.Request, handler):
    """gzip the JSON feeds (the aircraft feed alone is ~250 KB every 10 s) for clients that ask for it."""
    resp = await handler(request)
    if (type(resp) is web.Response and resp.body is not None and len(resp.body) > 1024
            and (resp.content_type or "").startswith(("application/json", "text/html"))
            and "gzip" in request.headers.get("Accept-Encoding", "")):
        resp.enable_compression()
    return resp


@web.middleware
async def auth_middleware(request: web.Request, handler):
    cfg = request.app[CFG]
    token = cfg.access_token
    path = request.path
    if path.startswith("/k/"):  # /k/<token>/... form: survives clients that drop query strings
        path = "/" + path.split("/", 3)[3] if path.count("/") >= 3 else "/"
    if token and path not in PUBLIC_PATHS and not path.startswith("/icons/"):
        given = (request.match_info.get("tok") or request.query.get("token")
                 or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                 or request.cookies.get("sitrep_token", ""))
        # any home's token works; it also picks the home the page opens on
        match = next((k for k in cfg.tokens() if given and hmac.compare_digest(given, k)), None)
        request["home"] = cfg.tokens().get(match) if match else None
        if match and (request.match_info.get("tok") or request.query.get("token")):
            request["set_cookie"] = match
        if not match:
            why = "no token in the request" if not given else f"token does not match ({len(given)} chars, starts {given[:4]})"
            _LOGGER.warning(
                "401 %s: %s (from %s, referer %s, dest %s, UA %s)", path, why,
                request.headers.get("X-Forwarded-For", request.remote), request.headers.get("Referer", "-"),
                request.headers.get("Sec-Fetch-Dest", "-"), request.headers.get("User-Agent", "?")[:80],
            )
            # no-store: a cached 401 would otherwise outlive a fixed URL or a container restart
            return web.Response(status=401, text=f"Unauthorised: open /?token=... or /k/<token>/\n\n({why})",
                                headers={"Cache-Control": "no-store"})
    resp = await handler(request)
    if request.get("set_cookie") and path in ("/", "/index.html"):
        # remembered so that an installed app which opens "/" with no token still gets in on this device
        resp.set_cookie("sitrep_token", request["set_cookie"], max_age=365 * 86400, httponly=True, samesite="Lax",
                        secure=request.headers.get("X-Forwarded-Proto", request.scheme) == "https")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    # send our origin (not full URL) so tile servers like OSM see a valid Referer
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp


async def icon_file(request: web.Request) -> web.StreamResponse:
    f = ICONS / request.match_info["name"]
    if f.parent != ICONS or not f.is_file():
        raise web.HTTPNotFound()
    if f.name == "site.webmanifest":
        # an installed web app opens start_url with no cookies or headers, so the token has to be inside it
        manifest = json.loads(await asyncio.to_thread(f.read_text))
        given = request.match_info.get("tok") or request.query.get("token", "")
        if given and any(hmac.compare_digest(given, k) for k in request.app[CFG].tokens()):
            # absolute and inside the scope: a relative "../" fell outside the manifest's own folder and was ignored
            manifest["start_url"] = f"/k/{given}/" if request.match_info.get("tok") else "/?" + urlencode({"token": given})
        else:
            manifest["start_url"] = "/"
        manifest["scope"] = "/"
        manifest["id"] = manifest["start_url"]
        return web.json_response(manifest, content_type="application/manifest+json", headers={"Cache-Control": "no-store"})
    return web.FileResponse(f, headers={"Cache-Control": "max-age=604800"})


async def token_root(request: web.Request) -> web.Response:
    # the page uses relative URLs, so it must be served from /k/<token>/ with the slash
    raise web.HTTPFound(f"/k/{request.match_info['tok']}/")


async def index(request: web.Request) -> web.Response:
    # read into memory so that it can be gzipped like the feeds (the page is ~240 KB)
    body = await asyncio.to_thread((STATIC / "index.html").read_bytes)
    return web.Response(body=body, content_type="text/html", charset="utf-8", headers={"Cache-Control": "no-cache"})


async def feed(request: web.Request) -> web.Response:
    sit = request.app[SIT]
    name = request.match_info["feed"]
    if name == "version":
        return web.json_response(await sit.version_info(__version__))
    if name == "vessels":
        return web.json_response(await sit.vessels(request.query.get("all") == "1"))
    if name == "vessel_info":
        q = request.query
        return web.json_response(await sit.vessel_info(q.get("mmsi", ""), q.get("imo", ""), q.get("name", "")),
                                 headers={"Cache-Control": "max-age=86400"})
    if name == "aircraft_info":
        return web.json_response(await sit.aircraft_info(request.query.get("hex", ""), request.query.get("callsign", "")),
                                 headers={"Cache-Control": "max-age=3600"})
    if name in ("fuel", "buses", "bus_stops", "chargers", *OSM_POINTS):
        try:
            bbox = tuple(float(x) for x in request.query["bbox"].split(","))
            if len(bbox) != 4:
                raise ValueError
        except (KeyError, ValueError):
            bbox = None
        fn = {"buses": sit.buses, "bus_stops": sit.bus_stops, "fuel": sit.fuel, "chargers": sit.chargers}.get(name) or partial(sit._osm_points, name)
        return web.json_response(await fn(bbox))
    if name == "reverse":
        try:
            lat, lon = float(request.query["lat"]), float(request.query["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
        except (KeyError, ValueError):
            return web.json_response({"error": "lat and lon required"}, status=400)
        return web.json_response(await sit.place_at(lat, lon), headers={"Cache-Control": "max-age=3600"})
    if name in ("iss", "crime"):
        try:
            lat, lon = float(request.query["lat"]), float(request.query["lon"])
        except (KeyError, ValueError):
            if name == "crime":
                return web.json_response({"error": "lat and lon required"}, status=400)
            lat = lon = None
        if name == "crime":
            return web.json_response(await sit.crime(lat, lon))
        return web.json_response(await sit.iss(lat, lon))
    if name == "bus_board":
        return web.json_response(await sit.bus_board(request.query.get("stop", "")))
    if name == "airport_board":
        return web.json_response(await sit.airport_board(request.query.get("icao", "")))
    if name == "rail_board":
        return web.json_response(await sit.rail_board(request.query.get("crs", "")))
    if name == "search":
        return web.json_response(await sit.search(request.query.get("q", "")), headers={"Cache-Control": "max-age=3600"})
    if name == "area":
        return web.json_response(await sit.area(request.query.get("id", "")), headers={"Cache-Control": "max-age=86400"})
    if name == "tides":
        return web.json_response(await sit.tides(request.query.get("station", "")))
    if name == "home_info":  # just where the home is: instant, so the page can open there
        cfg = request.app[CFG]
        h = cfg.home(request.query.get("home") or request.get("home"))
        if not h:
            return web.json_response({"error": "disabled: set HOME_LAT and HOME_LON"})
        return web.json_response({"home": {"key": h.key, "name": h.name, "lat": h.lat, "lon": h.lon}})
    if name == "home":
        return web.json_response(await sit.home_summary(request.query.get("home") or request.get("home")))
    handler = sit.handlers.get(name)
    if not handler:
        return web.json_response({"error": "unknown feed"}, status=404)
    return web.json_response(await handler())


FAV_MAX, FAV_MAX_BYTES = 300, 256 * 1024


def _fav_store(request: web.Request) -> Path:
    """One favourites list per home (picked by the token), shared by every browser using it."""
    cfg = request.app[CFG]
    h = cfg.home(request.get("home"))
    return cfg.data_dir / ("favourites.json" if not h or h is cfg.homes[0] else f"favourites-{h.key}.json")


def _clean_favs(items) -> list[dict]:
    out, seen = [], set()
    for f in items if isinstance(items, list) else []:
        try:
            k, i, lat, lon = str(f["k"])[:20], str(f["id"])[:200], float(f["lat"]), float(f["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)) or (k, i) in seen:
            continue
        seen.add((k, i))
        out.append({"k": k, "id": i, "name": str(f.get("name") or "")[:200], "lat": lat, "lon": lon,
                    "o": f["o"] if isinstance(f.get("o"), dict) else {}})
    return out[:FAV_MAX]


async def favourites(request: web.Request) -> web.Response:
    store = _fav_store(request)
    if request.method == "POST":
        if (request.content_length or 0) > FAV_MAX_BYTES:
            return web.json_response({"error": "too big"}, status=413)
        try:
            items = _clean_favs((await request.json()).get("items"))
        except (ValueError, AttributeError):
            return web.json_response({"error": "bad json"}, status=400)
        body = json.dumps({"items": items}, separators=(",", ":"))
        if len(body) > FAV_MAX_BYTES:
            return web.json_response({"error": "too big"}, status=413)
        tmp = store.with_suffix(".tmp")
        await asyncio.to_thread(lambda: (tmp.write_text(body), tmp.replace(store)))
        return web.json_response({"items": items})
    try:
        items = await asyncio.to_thread(lambda: json.loads(store.read_text()).get("items", []))
    except (OSError, ValueError):
        items = []
    return web.json_response({"items": items}, headers={"Cache-Control": "no-store"})


async def lookup(request: web.Request) -> web.Response:
    key = request.match_info["name"]
    return web.json_response(request.app[LOOKUPS].get(key, {}), headers={"Cache-Control": "max-age=86400"})


async def favicon(request: web.Request) -> web.FileResponse:
    # browsers ask for these fixed paths; PNG content is fine at the .ico URL
    name = "favicon-32.png" if request.path == "/favicon.ico" else "apple-touch-icon.png"
    return web.FileResponse(ICONS / name, headers={"Cache-Control": "max-age=604800", "Content-Type": "image/png"})


async def healthz(request: web.Request) -> web.Response:
    """Liveness plus which optional features are configured (never the values)."""
    cfg = request.app[CFG]
    sit = request.app[SIT]
    return web.json_response({
        "ok": True,
        "version": __version__,
        "open_meteo_calls_today": {"used": sit._om_calls if sit._om_day == datetime.now(timezone.utc).strftime("%Y-%m-%d") else 0,
                                   "budget": cfg.open_meteo_daily_calls},
        "features": {
            "access_token": bool(cfg.access_token),
            "ais": bool(cfg.ais_key),
            "national_highways": bool(cfg.nh_keys),
            "worldcam": cfg.enable_worldcam,
            "dsfrs": bool(cfg.dsfrs_stations),
            "bbc_local_regions": sorted(cfg.bbc_local_feeds),
            "background_adsb": cfg.enable_background_adsb,
            "lightning": cfg.enable_lightning,
            "seaglass": cfg.enable_seaglass,
            "firms": bool(cfg.firms_key),
            "fuel_finder": bool(cfg.fuel_client_id and cfg.fuel_client_secret),
            "homes": [h.name for h in cfg.homes],
            "rail_departures": bool(cfg.rail_ldb_key),
            "airport_boards": bool(cfg.aerodatabox_key),
            "buses": bool(cfg.bods_key),
            "bus_timetables": cfg.bus_timetable_regions,
            "ev_chargers": bool(cfg.ocm_key),
            "rail_disruptions": bool(cfg.rail_disruptions_key),
            "ha_notifications": [h.name for h in cfg.homes if h.webhook_url],
        },
    })


async def _startup(app: web.Application) -> None:
    cfg = app[CFG]
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
    app["session"] = session
    sit = Situation(cfg, session)
    app[SIT] = sit
    types, countries = await lookups.load(session, cfg.data_dir)
    app[LOOKUPS] = {"aircraft_types": types, "icao_countries": countries}
    if cfg.enable_background_adsb:
        sit.spawn(sit.adsb_loop())
    if cfg.enable_lightning:
        sit.lightning_relay.start()
    if cfg.bus_timetable_regions:
        sit.bustimes_maintain()  # first start downloads and indexes the timetables in the background
    for h in cfg.homes:
        if h.webhook_url:
            sit.spawn(sit.notify_loop(h))
    if cfg.fuel_client_id and cfg.fuel_client_secret:
        sit.spawn(sit.fuel())  # warm the cache: a cold fetch is ~30 batched requests
    _LOGGER.info(
        "UK SitRep %s on %s:%d (AIS %s, National Highways %s, WorldCam %s, DSFRS %s, auth %s)",
        __version__, cfg.host, cfg.port, "on" if cfg.ais_key else "off", "on" if cfg.nh_keys else "off",
        "on" if cfg.enable_worldcam else "off", "on" if cfg.dsfrs_stations else "off",
        "token" if cfg.access_token else "OPEN",
    )


async def _cleanup(app: web.Application) -> None:
    for task in list(app[SIT].tasks):
        task.cancel()
    await asyncio.gather(*app[SIT].tasks, return_exceptions=True)
    await app["session"].close()


def make_app(cfg: Config) -> web.Application:
    app = web.Application(middlewares=[compress_middleware, auth_middleware])
    app[CFG] = cfg
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/apple-touch-icon.png", favicon)
    app.router.add_get("/apple-touch-icon-precomposed.png", favicon)
    app.router.add_get("/k/{tok}", token_root)
    # every page route twice: /... with ?token=, and /k/<token>/... for clients that drop query strings
    for pre in ("", "/k/{tok}"):
        app.router.add_get(pre + "/", index)
        app.router.add_get(pre + "/index.html", index)
        app.router.add_get(pre + "/icons/{name}", icon_file)
        app.router.add_get(pre + "/api/favourites", favourites)
        app.router.add_post(pre + "/api/favourites", favourites)
        app.router.add_get(pre + "/api/{feed}", feed)
        app.router.add_get(pre + "/{name:aircraft_types|icao_countries}.json", lookup)
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    return app
