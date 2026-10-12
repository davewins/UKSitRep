"""aiohttp web server: serves the map page and the /api/<feed> JSON endpoints."""

from __future__ import annotations

import asyncio
import hmac
import logging
from pathlib import Path

import aiohttp
from aiohttp import web

from . import __version__, lookups
from .config import Config
from .feeds import Situation

_LOGGER = logging.getLogger("uksitrep")
STATIC = Path(__file__).parent / "static"
ICONS = STATIC / "icons"
# Icons are fetched by browsers without our token, so they skip auth
PUBLIC_PATHS = {"/healthz", "/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"}
CFG = web.AppKey("cfg", Config)
SIT = web.AppKey("situation", Situation)
LOOKUPS = web.AppKey("lookups", dict)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    token = request.app[CFG].access_token
    path = request.path
    if path.startswith("/k/"):  # /k/<token>/... form: survives clients that drop query strings
        path = "/" + path.split("/", 3)[3] if path.count("/") >= 3 else "/"
    if token and path not in PUBLIC_PATHS and not path.startswith("/icons/"):
        given = (request.match_info.get("tok") or request.query.get("token")
                 or request.headers.get("Authorization", "").removeprefix("Bearer ").strip())
        if not given or not hmac.compare_digest(given, token):
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
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    # send our origin (not full URL) so tile servers like OSM see a valid Referer
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp


async def icon_file(request: web.Request) -> web.StreamResponse:
    f = ICONS / request.match_info["name"]
    if f.parent != ICONS or not f.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(f, headers={"Cache-Control": "max-age=604800"})


async def token_root(request: web.Request) -> web.Response:
    # the page uses relative URLs, so it must be served from /k/<token>/ with the slash
    raise web.HTTPFound(f"/k/{request.match_info['tok']}/")


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def feed(request: web.Request) -> web.Response:
    sit = request.app[SIT]
    name = request.match_info["feed"]
    if name == "vessels":
        return web.json_response(await sit.vessels(request.query.get("all") == "1"))
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
    if name == "tides":
        return web.json_response(await sit.tides(request.query.get("station", "")))
    handler = sit.handlers.get(name)
    if not handler:
        return web.json_response({"error": "unknown feed"}, status=404)
    return web.json_response(await handler())


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
    return web.json_response({
        "ok": True,
        "version": __version__,
        "features": {
            "access_token": bool(cfg.access_token),
            "ais": bool(cfg.ais_key),
            "national_highways": bool(cfg.nh_keys),
            "worldcam": cfg.enable_worldcam,
            "dsfrs": bool(cfg.dsfrs_stations),
            "bbc_local_regions": sorted(cfg.bbc_local_feeds),
            "background_adsb": cfg.enable_background_adsb,
            "lightning": cfg.enable_lightning,
            "firms": bool(cfg.firms_key),
        },
    })


async def _startup(app: web.Application) -> None:
    cfg = app[CFG]
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    session = aiohttp.ClientSession(headers={"User-Agent": f"UKSitRep/{__version__}"})
    app["session"] = session
    sit = Situation(cfg, session)
    app[SIT] = sit
    types, countries = await lookups.load(session, cfg.data_dir)
    app[LOOKUPS] = {"aircraft_types": types, "icao_countries": countries}
    if cfg.enable_background_adsb:
        sit.spawn(sit.adsb_loop())
    if cfg.enable_lightning:
        sit.lightning_relay.start()
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
    app = web.Application(middlewares=[auth_middleware])
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
        app.router.add_get(pre + "/api/{feed}", feed)
        app.router.add_get(pre + "/{name:aircraft_types|icao_countries}.json", lookup)
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    return app
