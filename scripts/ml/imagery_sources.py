"""Higher-resolution imagery sources for saved scenes (imagery-sources-v1).

Three jobs in one module:
  inventory  every aerial or satellite source checked for Mooroolbark, with served zoom, native ground sample
             distance (provider metadata where published, measured otherwise), capture dates, licence, cost and
             whether a key is needed (`--list`, `PROVIDERS`). Key- or licence-gated providers are listed with how to
             get access and the price. They refuse to fetch until the named environment variable is set. The value is
             never printed or written: manifest URLs carry `${VAR}`, not the secret.
  measure    effective resolution of a square sample around a point (`--probe`): spectral octave ratio (power in the
             top octave of the sampling band against the octave below, with JPEG 8 px block lines masked), the mean
             edge spread function of the strongest edges on a metric grid (10-90 % width, MTF50 period, smallest
             resolvable bar at MTF10), gradient energy per square metre, and cross-zoom agreement (does zoom z add
             variance that the coarser zoom cannot predict). An upsampled tile keeps the coarser zoom's MTF in metres
             and has almost no top-octave power, however many pixels it has.
  fetch      a hi-res raster for a saved scene frame (`--fetch --scene ID --provider NAME [--zoom Z]`). Provider tiles
             at zoom Z are cached under data/imagery/<provider>/<z>/ with a sha256 manifest and provenance per tile in
             data/imagery/<provider>/manifests/z<Z>.json. They are reprojected onto the saved pixel grid at an
             integer multiple k (hi-res pixel q covers saved edge coordinates [q/k, (q+1)/k]); on tile scenes, whose
             frame sits on the z19 pixel grid, this is a lossless integer crop. The raster is then checked against the
             saved PNG with scene_local_stack.register_local on the k x k box average (per-window phase correlation,
             leave-one-out hold-out residual). An accepted field of at least MIN_APPLY_SHIFT_PX is applied, scaled by
             k; a rejected registration is reported and the raster is marked unaligned.

The paired measurement (`--paired`) runs structure_kinds.appearance_features (rib anisotropy, rib period),
structure_kinds_signals.oriented_ribs and scene_roof_diagnostics.edge_support on same-extent crops of the saved image
and of the hi-res rasters, for a list of (scene, object) structures. Outputs go under data/reviews/eyes/imagery-sources/.
Nothing here writes data/scenes, the eyes index or production tiles. A hi-res raster is a diagnostic view, not evidence,
until a separate decision makes it one (docs/roof-eyes.md, "Multi-date evidence").
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

from .tiles import WORLD_SPAN_M, lonlat_to_tile_xy

VERSION = 'imagery-sources-v1'
OUT = Path('data/reviews/eyes/imagery-sources')
CACHE = Path('data/imagery')
TILE_PX = 256
MAX_TILES = 2000
CONTROL_SHIFT_M = 1.          # paired edge-support control: the outline displaced by 1 m
MIN_APPLY_SHIFT_PX = .25       # saved px: below this an accepted field is recorded but not applied
CAMBRIDGE = {'lat': -37.79033, 'lon': 145.31259, 'label': '27 Cambridge Road, Mooroolbark'}
PIPELINE_UA = 'addr-ai-building-pipeline/1.0 (+https://github.com/benebsworth/addr)'
BROWSER_UA = 'GeoAU-addr/1.0 (+Mozilla/5.0 compatible; imagery source evaluation)'
ESRI_PLACEHOLDER_SHA256 = '9eafd300d61393184a4abc1d458564cfd1cd9b6f9c4e9c74687045c0a0e5b858'   # "Map data not yet available"
PRODUCTION_CLAHE_TILE_M = 1152*.23595/12   # structure_kinds.enhance: 12 x 12 tiles over a 1152 px, 0.236 m scene


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _now():
    return datetime.now(UTC).isoformat(timespec='seconds')


def _num(value, digits=4):
    if value is None:
        return None
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix+f'.tmp{os.getpid()}')
    tmp.write_text(json.dumps(value, indent=1, default=_plain))
    tmp.replace(path)


def _plain(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f'not JSON serialisable: {type(value).__name__}')


# --- provider registry ----------------------------------------------------------------------------------------

class AccessRequired(RuntimeError):
    """The provider needs a key or licence that is not configured; the message says how to get it and the price."""


class CacheIntegrityError(RuntimeError):
    """A cached tile no longer matches the sha256 its manifest recorded (offline mode refuses to refetch)."""


@dataclass(frozen=True)
class Provider:
    name: str
    title: str
    status: str                  # free-endpoint | licence-required | key-required | no-coverage | not-imagery
    template: str | None         # {z} {x} {y}, optional {release}, {secret} (the key_env value)
    served_max_zoom: int | None  # deepest zoom with a real tile at 27 Cambridge Road (checked 27 Sep 2026)
    native_gsd: str              # what the provider publishes or what was measured
    dates: str
    licence: str
    cost: str
    key_env: str | None = None
    how_to_get: str = ''
    recommended_zoom: int | None = None
    ext: str = 'jpg'
    user_agent: str = PIPELINE_UA
    placeholder_sha256: tuple = ()
    tested: bool = True          # False: the template is documented but was never exercised (no key here)
    notes: str = ''

    @property
    def fetchable(self):
        return self.template is not None

    def require_access(self):
        if not self.fetchable:
            raise AccessRequired(f'{self.name}: no tile endpoint to fetch ({self.status}). {self.how_to_get}'.strip())
        if self.key_env and not os.environ.get(self.key_env):
            raise AccessRequired(f'{self.name} needs {self.key_env} ({self.status}). How to get it: {self.how_to_get} '
                                 f'Cost: {self.cost}')

    def url(self, z, x, y, release=None, *, redact=False):
        """Tile URL; with redact=True the secret is replaced by ${KEY_ENV} (what manifests store)."""
        self.require_access() if not redact else None
        secret = f'${{{self.key_env}}}' if redact or not self.key_env else os.environ[self.key_env]
        return self.template.format(z=z, x=x, y=y, release=release, secret=secret)


_GOOGLE_TILES_API = ('Google Maps Platform Map Tiles API (2D tiles, mapType satellite): create a Cloud project with '
                     'billing, enable "Map Tiles API", create an API key, POST /v1/createSession then GET '
                     '/v1/2dtiles/{z}/{x}/{y}?session=&key=.')

PROVIDERS: dict[str, Provider] = {p.name: p for p in (
    Provider('google', 'Google satellite (maps tile endpoint mt1 lyrs=s, as the pipeline uses)', 'free-endpoint',
             'https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}', 22,
             'aerial mosaic, the same capture as the saved scenes. Measured on 200 m at 27 Cambridge: z19 (saved) is '
             'sampling-limited (edge 10-90 % 0.28 m); z20 edge 0.17 m, smallest bar 0.13 m; z21 edge 0.12 m, bar 0.10 m '
             'but its top octave (0.06-0.12 m periods) is nearly empty (ratio 0.05): effective about 0.10 m, not 0.059 m; '
             'z22 is interpolation. z21 tiles carry a faint "(c)2026 Google" watermark',
             'not exposed by the tile endpoint. Google Earth time-machine metadata at five Mooroolbark points '
             '(19 Oct 2026): z18-z20 = 2025-12-12, "Image © 2026 Airbus", epoch 367; z21 = 2023-10-18, epoch 1021. '
             'The z21 detail is an older, finer capture, not a newer one',
             'Google Maps/Earth terms: direct tile access outside the Maps APIs is not licensed; the licensed route is '
             'the Map Tiles API',
             '$0 as used (unofficial); licensed route: Map Tiles API 2D tiles, 100,000 free per month, then '
             'USD 0.60 per 1,000 (1-5 M: 0.48)', recommended_zoom=21,
             notes='This is the endpoint the saved scenes (z19) and structure_kinds_optical (z20) already use. '
                   'Dated 19 Oct 2026: z20 is the 2025-12-12 capture, z21 is 2023-10-18; choose z20 for currency and '
                   'z21 for the finer 2023 detail.'),
    Provider('google-maptiles-api', 'Google Map Tiles API (official 2D satellite tiles)', 'key-required', None, None,
             'same Google imagery as above (one mosaic)', 'as above', 'Google Maps Platform Terms; attribution required; '
             'caching limited to the API terms (tiles may not be stored beyond the cache headers)',
             '100,000 2D tiles free per month, then USD 0.60 per 1,000', key_env='GOOGLE_MAPS_API_KEY',
             how_to_get=_GOOGLE_TILES_API, tested=False,
             notes='Not implemented here (needs a session token); listed so the licensed route is visible.'),
    Provider('esri', 'Esri World Imagery (services.arcgisonline.com)', 'free-endpoint',
             'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', 20,
             'Vantor Vivid Advanced 0.34 m (z12-19, 2025-11-06); z20 is Vantor Vivid Premium SRC_RES 0.34 m '
             'resampled to 0.15 m (SAMP_RES), 2025-09-28; z21+ is a "Map data not yet available" placeholder',
             'z<=19 2025-11-06, z20 2025-09-28 (World_Imagery identify, Raster Basemaps 2026.R03)',
             'Esri terms: attribution "Source: Esri, Vantor, Earthstar Geographics"; commercial use via ArcGIS '
             'Location Platform', '$0 public endpoint; ArcGIS Location Platform 2,000,000 basemap tiles free per '
             'month, then USD 0.15 per 1,000', recommended_zoom=19, placeholder_sha256=(ESRI_PLACEHOLDER_SHA256,),
             notes='Different capture and ortho from the saved Google grid: at 27 Cambridge the z19 image sits 6.7 saved px '
                   '(1.6 m) off and only 40 of 121 windows register (hold-out 0.80 px); measured edge 0.41 m (z19), '
                   '0.24 m (z20).'),
    Provider('esri-wayback', 'Esri World Imagery Wayback (archived releases)', 'free-endpoint',
             'https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/MapServer/tile/{release}/{z}/{y}/{x}',
             20, 'satellite only here: 0.30-0.50 m sources (WV02, WV03, GE01, Vantor LG05/LG06); z20 releases are '
             '0.30-0.34 m resampled to 0.15 m', 'distinct local captures 2017-01-10, 2019-06-26, 2020-08-02, 2021-06-03, '
             '2022-02-13, 2022-08-13, 2023-06-18, 2023-12-20, 2025-09-28, 2025-11-06 (196 releases)',
             'Esri terms as World Imagery', '$0', recommended_zoom=19, user_agent=BROWSER_UA,
             placeholder_sha256=(ESRI_PLACEHOLDER_SHA256,),
             notes='Needs --release (imagery_dates.discover finds them); already used by scene_local_stack.'),
    Provider('vicmap-aerial', 'Vicmap Basemaps aerial (AERIAL_WM_256, DTP; CIP imagery)', 'licence-required',
             'https://base.maps.vic.gov.au/service?SERVICE=WMTS&VERSION=1.0.0&REQUEST=GetTile&LAYER=AERIAL_WM_256'
             '&STYLE=default&TILEMATRIXSET=EPSG:3857:256&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=image/png',
             20, 'CIP aerial mosaic, served to level 20 (0.118 m/px here); one reachability tile measured (probe.json)',
             'metro Melbourne updated annually, "typically at least two years old"; tile last-modified 2026-02-14',
             'WMTS capabilities: "Vicmap Basemaps is a licensed service ... An annual license fee is payable and is '
             'based on usage by the clients business" (page impressions)',
             'annual fee by quote (vicmap@transport.vic.gov.au); discounts for Vicmap licensees and CIP members',
             key_env='VICMAP_BASEMAPS_LICENCE', ext='png',
             how_to_get='Email vicmap@transport.vic.gov.au for a Vicmap Basemaps licence, then set VICMAP_BASEMAPS_LICENCE '
                        '(any value, e.g. the licence number) to record that a licence covers this use. The endpoint '
                        'answers without a key; the licence, not a key, is what is missing.',
             tested=False, notes='Reachable without a key (HTTP 200 on 27 Sep 2026); not fetched in bulk because the '
                                 'licence requires a fee. Probe 29 Sep 2026 (two tiles over 27 Cambridge Road, kept under '
                                 'data/references/imagery-probes/): z19 and z20 answer (tile last-modified 2026-01-10 and '
                                 '2026-02-14), z21 is refused (400), so 0.118 m/px nominal is the ceiling. On the shared z20 '
                                 'tile the Google mt1 tile is sharper (gradient p90 54.6 vs 39.0, Laplacian variance 2784 vs '
                                 '1455, top-octave energy ratio 1.27 vs 1.04): the basemap aerial is a softer, older '
                                 'resample of the CIP capture and gives no resolution gain over the pipeline tiles. MapShare '
                                 'Vic draws its basemap from this same WMTS (its Geocortex map config lists '
                                 'base.maps.vic.gov.au), so a browser extension over MapShare would extract the same licensed '
                                 'tiles. Whole Mooroolbark scan area (5.1 x 5.1 km): 7,396 tiles / 0.9 GB at z19, 28,900 / 3.9 GB '
                                 'at z20. Capabilities text: licensed service, annual fee by map page impressions; 1-3 month '
                                 'evaluation licence on request; the CIP source imagery (7.5-10 cm) is DTP IP sold through '
                                 'Data Service Providers.'),
    Provider('vic-cip-iws', 'Victorian Coordinated Imagery Program via Image Web Server (7.5-10 cm metro captures)',
             'licence-required', None, None, 'CIP metro captures (7.5-10 cm class, per project)',
             'per project on the Vicmap Imagery and Elevation Coverage map', 'DTP retains IP; IWS access is by '
             'invitation for DEECA staff and CIP purchase partners (Victorian and local government)',
             'government: CIP subscription or project partnership; private: buy through a Vicmap Data Service Provider '
             'or Value Added Retailer (quote)',
             how_to_get='Government bodies: coordinated.imagery@transport.vic.gov.au. Private sector: a DSP/VAR from '
                        'land.vic.gov.au "How to access spatial data".', tested=False),
    Provider('nearmap', 'Nearmap Tile API (vertical)', 'key-required',
             'https://api.nearmap.com/tiles/v3/Vert/{z}/{x}/{y}.jpg?apikey={secret}', None,
             '5.5-7.5 cm GSD vertical (HyperCamera3 captures down to 4.4 cm), refreshed up to 6 times a year',
             'multiple dated surveys per year; Coverage API lists survey ids and dates',
             'Nearmap Master Subscription Agreement; units purchased, not usage; storage limits apply',
             'no public price: subscriptions by quote, reported AUD 2,000-5,000+ per year for small business; '
             'resellers sell credits (Plex-Earth 2,500 credits for USD 250)', key_env='NEARMAP_API_KEY',
             how_to_get='Nearmap sales quote, then an API key from MyAccount.', tested=False),
    Provider('metromap', 'MetroMap (Aerometrex) WMS/WMTS', 'key-required', None, None,
             'high-resolution aerial (the plans page quotes no GSD; typically 5-10 cm urban)',
             'metro areas 4-6 captures per year, archive back decades',
             'MetroMap subscription terms; 12-month term',
             'Pro AUD 2,640 per year (1 user; WMS/WMTS optional extra); Team AUD 6,999 per year (5 users); '
             'Enterprise with API/WMTS by quote; 7-day trial without downloads',
             key_env='METROMAP_API_KEY', how_to_get='metromap.com.au/plans (Enterprise tier for API access).',
             tested=False),
    Provider('mapbox-satellite', 'Mapbox Satellite (raster tiles API)', 'key-required',
             'https://api.mapbox.com/v4/mapbox.satellite/{z}/{x}/{y}.jpg90?access_token={secret}', None,
             'unknown here without a token (global Maxar/Vantor-class mosaic plus local aerial in some cities)',
             'not published per tile', 'Mapbox ToS; attribution required',
             'Raster Tiles API 750,000 tiles free per month, then USD 0.25 per 1,000 (to 2 M)',
             key_env='MAPBOX_ACCESS_TOKEN', how_to_get='Create a Mapbox account and a public access token.',
             tested=False, notes='Unauthenticated request returns 401 "Not Authorized".'),
    Provider('azure-maps', 'Azure Maps imagery (microsoft.imagery; successor to Bing Maps aerial)', 'key-required',
             None, 19, 'imagery tiles served to zoom 19 only (0.236 m here): no gain over the saved scenes',
             'not published per tile', 'Azure Maps terms (Gen2)',
             '1,000 free transactions per month (1 transaction = 15 tiles), then by quote',
             key_env='AZURE_MAPS_KEY', how_to_get='Azure subscription, create a Maps account (Gen2), use its key.',
             tested=False, notes='Bing Maps Basic (free) keys stopped working on 30 June 2025; Enterprise ends 2028. '
                                 'The Bing metadata API answers NoCredentials without a key.'),
    Provider('maptiler-satellite', 'MapTiler Satellite', 'key-required',
             'https://api.maptiler.com/tiles/satellite-v2/{z}/{x}/{y}.jpg?key={secret}', None,
             'unknown here without a key', 'not published per tile', 'MapTiler terms; attribution required',
             'free plan (non-commercial, limited requests); paid plans from about USD 25 per month',
             key_env='MAPTILER_KEY', how_to_get='Create a MapTiler Cloud account and key.', tested=False),
    Provider('nsw-six-imagery', 'NSW Spatial Services imagery (maps.six.nsw.gov.au)', 'no-coverage', None, None,
             'about 10 cm urban NSW', 'NSW captures', 'CC BY 4.0', '$0',
             notes='Victoria is outside coverage: the tile at 27 Cambridge Road returns 404, identify returns Landsat.'),
    Provider('openaerialmap', 'OpenAerialMap', 'no-coverage', None, None, 'community uploads', '-', 'CC BY 4.0', '$0',
             notes='API search of the Mooroolbark bbox found 0 images.'),
    Provider('maxar-open-data', 'Maxar/Vantor Open Data (disaster events)', 'no-coverage', None, None, '0.3-0.5 m',
             'event-driven', 'CC BY-NC 4.0', '$0', notes='55 events, none in Australia.'),
    Provider('planet', 'Planet (PlanetScope 3 m, SkySat 0.5 m)', 'key-required', None, None,
             'PlanetScope 3 m; SkySat 0.5 m archive or tasking', 'daily (PlanetScope)', 'Planet licence', 'by quote',
             key_env='PL_API_KEY', how_to_get='Planet sales / Planet Insights Platform trial.', tested=False,
             notes='Coarser than the saved 0.236 m scenes; no gain for roofs.'),
    Provider('elvis', 'ELVIS (Geoscience Australia elevation portal)', 'not-imagery', None, None,
             'LiDAR point clouds and DEMs, not imagery', 'Greater Melbourne LiDAR 2017-18 among others',
             'per dataset (many CC BY 4.0; some Victorian sets restricted)', '$0 where open',
             notes='Relevant to roof heights, not to image resolution.'),
)}


# --- tile cache with sha256 manifests ---------------------------------------------------------------------------

def _decode(data):
    try:
        with Image.open(io.BytesIO(data)) as image:
            return np.asarray(image.convert('RGB'))
    except OSError:
        return None


def is_placeholder(provider, data, image):
    """A provider's 'no data here' tile: a known placeholder hash or an almost uniform grey tile."""
    if hashlib.sha256(data).hexdigest() in provider.placeholder_sha256:
        return True
    if image is None or not provider.placeholder_sha256:
        return False
    flat = image.reshape(-1, 3).astype(np.int16)
    mode = np.median(flat, axis=0)
    grey = float(mode.max()-mode.min()) <= 3
    return bool(grey and (np.abs(flat-mode).max(axis=1) <= 2).mean() >= .95)


class TileCache:
    """Provider tiles on disk with a per-zoom sha256 manifest.

    Files: data/imagery/<provider>[/<release>]/<z>/<x>_<y>.<ext> (the same layout imagery.ImageryFetcher uses, so its
    existing Google z20 tiles are adopted, hashed and recorded rather than refetched). Manifest records carry sha256,
    bytes, status (ok, missing, placeholder), HTTP status, content type, fetch time, the redacted URL and the source
    (fetched or adopted). A file whose bytes no longer match its record is refetched online and refused offline.
    """

    def __init__(self, provider, root=Path('.'), *, offline=False, session=None, release=None, workers=6,
                 timeout=20., retries=2):
        self.provider = provider if isinstance(provider, Provider) else PROVIDERS[provider]
        if self.provider.template and '{release}' in self.provider.template and not release:
            raise ValueError(f'{self.provider.name} needs a release id')
        if not offline:
            self.provider.require_access()
        self.root = Path(root)
        self.release = str(release) if release else None
        self.dir = self.root/CACHE/self.provider.name
        if self.release:
            self.dir = self.dir/self.release
        self.offline = offline
        self.session = session
        self.workers = workers
        self.timeout = timeout
        self.retries = retries
        self.lock = threading.Lock()
        self._manifests = {}
        self._dirty = set()
        self.stats = {'fetched': 0, 'cache_hits': 0, 'adopted': 0, 'missing': 0, 'placeholder': 0, 'errors': 0,
                      'refetched_corrupt': 0, 'offline_missing': 0}

    def tile_path(self, z, x, y):
        return self.dir/str(z)/f'{x}_{y}.{self.provider.ext}'

    def manifest_path(self, z):
        return self.dir/'manifests'/f'z{z}.json'

    def manifest(self, z):
        with self.lock:
            if z not in self._manifests:
                path = self.manifest_path(z)
                self._manifests[z] = json.loads(path.read_text()) if path.exists() else {
                    'version': VERSION, 'provider': self.provider.name, 'release': self.release, 'zoom': z, 'tiles': {}}
            return self._manifests[z]

    def _record(self, z, key, record):
        manifest = self.manifest(z)
        with self.lock:
            manifest['tiles'][key] = record
            self._dirty.add(z)

    def save(self):
        with self.lock:
            dirty, self._dirty = sorted(self._dirty), set()
            for z in dirty:
                manifest = self._manifests[z]
                manifest['updated_at'] = _now()
                _atomic_json(self.manifest_path(z), manifest)
        return dirty

    def _count(self, key):
        with self.lock:
            self.stats[key] += 1

    def record(self, z, x, y):
        return self.manifest(z)['tiles'].get(f'{x}_{y}')

    def tile(self, z, x, y):
        """RGB uint8 array of one tile, or None when the provider has no data there (or offline and uncached)."""
        key, path = f'{x}_{y}', self.tile_path(z, x, y)
        record = self.record(z, x, y)
        if path.exists():
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if record and record.get('sha256') and record['sha256'] != digest:
                if self.offline:
                    raise CacheIntegrityError(f'{path} does not match its manifest sha256')
                self._count('refetched_corrupt')
            else:
                image = _decode(data)
                if not record:
                    record = {'sha256': digest, 'bytes': len(data), 'status': 'ok', 'http_status': None,
                              'content_type': None, 'fetched_at': None, 'url': self.provider.url(z, x, y, self.release, redact=True)
                              if self.provider.template else None,
                              'source': 'adopted: already in the cache (imagery.ImageryFetcher layout), hashed on first use',
                              'adopted_at': _now()}
                    self._record(z, key, record)
                    self._count('adopted')
                else:
                    self._count('cache_hits')
                if image is None or image.shape[:2] != (TILE_PX, TILE_PX):
                    if self.offline:
                        raise CacheIntegrityError(f'{path} is not a {TILE_PX} px image')
                else:
                    return image
        elif record and record.get('status') in ('missing', 'placeholder'):
            self._count(record['status'])
            return None
        if self.offline:
            self._count('offline_missing')
            return None
        return self._fetch(z, x, y)

    def _fetch(self, z, x, y):
        key, path = f'{x}_{y}', self.tile_path(z, x, y)
        url = self.provider.url(z, x, y, self.release)
        redacted = self.provider.url(z, x, y, self.release, redact=True)
        session = self.session or requests
        error = None
        for attempt in range(self.retries+1):
            try:
                response = session.get(url, headers={'User-Agent': self.provider.user_agent}, timeout=self.timeout)
            except requests.RequestException as exc:
                error = f'{type(exc).__name__}'
                time.sleep(.5*(attempt+1))
                continue
            status = response.status_code
            content_type = (response.headers or {}).get('Content-Type') or (response.headers or {}).get('content-type')
            base = {'http_status': status, 'content_type': content_type, 'fetched_at': _now(), 'url': redacted,
                    'source': 'fetched'}
            if status in (400, 404):
                self._record(z, key, {**base, 'status': 'missing', 'sha256': None, 'bytes': 0})
                self._count('missing')
                return None
            if status != 200:
                error = f'HTTP {status}'
                time.sleep(.5*(attempt+1))
                continue
            data = response.content
            image = _decode(data)
            digest = hashlib.sha256(data).hexdigest()
            if is_placeholder(self.provider, data, image):
                self._record(z, key, {**base, 'status': 'placeholder', 'sha256': digest, 'bytes': len(data)})
                self._count('placeholder')
                return None
            if image is None or image.shape[:2] != (TILE_PX, TILE_PX):
                error = 'not a 256 px image'
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix+f'.tmp{threading.get_ident()}')
            tmp.write_bytes(data)
            tmp.replace(path)
            self._record(z, key, {**base, 'status': 'ok', 'sha256': digest, 'bytes': len(data)})
            self._count('fetched')
            return image
        self._count('errors')
        with self.lock:
            self.stats.setdefault('last_error', error)
        return None


def mosaic(cache, z, gx0, gy0, gx1, gy1):
    """(RGB, valid, tiles) for the global zoom-z pixel box [gx0, gx1) x [gy0, gy1); tiles = [[x, y, sha256|status]]."""
    gx0, gy0, gx1, gy1 = (int(v) for v in (gx0, gy0, gx1, gy1))
    if gx1 <= gx0 or gy1 <= gy0:
        raise ValueError('empty pixel box')
    tx0, ty0, tx1, ty1 = gx0//TILE_PX, gy0//TILE_PX, (gx1-1)//TILE_PX, (gy1-1)//TILE_PX
    coords = [(x, y) for y in range(ty0, ty1+1) for x in range(tx0, tx1+1)]
    if len(coords) > MAX_TILES:
        raise ValueError(f'{len(coords)} tiles requested; the limit is {MAX_TILES}')
    with ThreadPoolExecutor(max_workers=cache.workers) as pool:
        images = list(pool.map(lambda c: cache.tile(z, *c), coords))
    height, width = (ty1-ty0+1)*TILE_PX, (tx1-tx0+1)*TILE_PX
    canvas = np.zeros((height, width, 3), np.uint8)
    valid = np.zeros((height, width), bool)
    tiles = []
    for (x, y), image in zip(coords, images, strict=True):
        record = cache.record(z, x, y) or {}
        tiles.append([x, y, record.get('sha256') if image is not None else (record.get('status') or 'unavailable')])
        if image is None:
            continue
        oy, ox = (y-ty0)*TILE_PX, (x-tx0)*TILE_PX
        canvas[oy:oy+TILE_PX, ox:ox+TILE_PX] = image
        valid[oy:oy+TILE_PX, ox:ox+TILE_PX] = True
    ox, oy = gx0-tx0*TILE_PX, gy0-ty0*TILE_PX
    return (np.ascontiguousarray(canvas[oy:oy+gy1-gy0, ox:ox+gx1-gx0]),
            np.ascontiguousarray(valid[oy:oy+gy1-gy0, ox:ox+gx1-gx0]), tiles)


# --- reprojection onto the saved grid --------------------------------------------------------------------------

def frame_mapping(scene):
    """Saved pixel edge coordinate p -> Web Mercator world fraction wx0 + p*sx (region.bounds are Mercator-square)."""
    width, height = scene['image_width'], scene['image_height']
    west, south, east, north = scene['region']['bounds']
    wx0, wy0 = lonlat_to_tile_xy(west, north, 0)
    wx1, wy1 = lonlat_to_tile_xy(east, south, 0)
    return {'wx0': wx0, 'wy0': wy0, 'sx': (wx1-wx0)/width, 'sy': (wy1-wy0)/height, 'width': width, 'height': height}


def zoom_ratio(scene, zoom):
    """(rx, ry): zoom-level pixels per saved pixel."""
    m = frame_mapping(scene)
    n = TILE_PX*2**zoom
    return m['sx']*n, m['sy']*n


def choose_k(scene, zoom):
    rx, ry = zoom_ratio(scene, zoom)
    return max(1, int(round((rx+ry)/2)))


def zoom_mpp(lat, zoom):
    return WORLD_SPAN_M*math.cos(math.radians(lat))/(TILE_PX*2**zoom)


def lonlat_to_saved_px(scene, lon, lat):
    """Continuous saved edge coordinates of a lon/lat (pixel p covers [p, p+1])."""
    m = frame_mapping(scene)
    wx, wy = lonlat_to_tile_xy(lon, lat, 0)
    return (wx-m['wx0'])/m['sx'], (wy-m['wy0'])/m['sy']


def hires_sample_coords(scene, zoom, k, box):
    """Zoom-level pixel indices (centre convention) sampled by every hi-res column and row of `box` at factor k.

    Hi-res pixel a (0-based inside the box) has its centre at saved edge coordinate x0 + (a + 0.5)/k; the zoom pixel
    whose centre sits there has index G - 0.5, with G the global zoom edge coordinate."""
    m = frame_mapping(scene)
    n = TILE_PX*2**zoom
    x0, y0, x1, y1 = box
    a = np.arange((x1-x0)*k, dtype=np.float64)
    b = np.arange((y1-y0)*k, dtype=np.float64)
    gx = (m['wx0']+(x0+(a+.5)/k)*m['sx'])*n-.5
    gy = (m['wy0']+(y0+(b+.5)/k)*m['sy'])*n-.5
    return gx, gy


def reproject(scene, cache, zoom, *, k=None, box=None):
    """Provider tiles at `zoom` resampled onto the saved grid at factor k for the saved pixel box (x0, y0, x1, y1).

    Integer crop when the zoom grid coincides with the k-subdivided saved grid (tile scenes); bicubic remap otherwise.
    Returns {'image', 'valid', 'k', 'zoom', 'box', 'method', 'ratio', 'tiles', 'mosaic_origin'}."""
    width, height = scene['image_width'], scene['image_height']
    box = tuple(int(v) for v in (box or (0, 0, width, height)))
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError(f'box {box} is outside the {width} x {height} saved frame')
    k = int(k or choose_k(scene, zoom))
    rx, ry = zoom_ratio(scene, zoom)
    gx, gy = hires_sample_coords(scene, zoom, k, box)
    integer = (abs(rx-k) < 1e-6 and abs(ry-k) < 1e-6 and abs(gx[0]-round(gx[0])) < 1e-3
               and abs(gy[0]-round(gy[0])) < 1e-3)
    margin = 0 if integer else 3
    ix0, iy0 = int(math.floor(gx.min()))-margin, int(math.floor(gy.min()))-margin
    ix1, iy1 = int(math.ceil(gx.max()))+1+margin, int(math.ceil(gy.max()))+1+margin
    canvas, valid, tiles = mosaic(cache, zoom, ix0, iy0, ix1, iy1)
    out_w, out_h = len(gx), len(gy)
    if integer:
        cx, cy = int(round(gx[0]))-ix0, int(round(gy[0]))-iy0
        image = canvas[cy:cy+out_h, cx:cx+out_w]
        valid_out = valid[cy:cy+out_h, cx:cx+out_w]
        method = 'integer-crop (zoom grid coincides with the k-subdivided saved grid; no resampling)'
    else:
        map_x = np.broadcast_to((gx-ix0).astype(np.float32)[None, :], (out_h, out_w)).copy()
        map_y = np.broadcast_to((gy-iy0).astype(np.float32)[:, None], (out_h, out_w)).copy()
        image = cv2.remap(canvas, map_x, map_y, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        valid_out = cv2.remap(valid.astype(np.uint8), map_x, map_y, cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT,
                              borderValue=0).astype(bool)
        valid_out = cv2.erode(valid_out.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool) & valid_out
        method = f'bicubic remap (zoom px per saved px {rx:.4f} vs k={k})'
    return {'image': np.ascontiguousarray(image), 'valid': np.ascontiguousarray(valid_out), 'k': k, 'zoom': zoom,
            'box': list(box), 'method': method, 'ratio': [_num(rx, 6), _num(ry, 6)], 'tiles': tiles,
            'mosaic_origin': [ix0, iy0], 'integer_crop': bool(integer)}


def derived_frame(scene, box, k):
    """Scene fields for a crop at factor k: saved index coordinate p maps to crop index k*(p - x0 + 0.5) - 0.5.

    Rings are pixel indices (structure_kinds rasterises them with fillPoly(round(pts))), so the hi-res index of a
    saved index is k*p + (k-1)/2 - k*x0; meters_to_pixel and pixel_to_meters are rewritten to that mapping."""
    x0, y0, x1, y1 = box
    f = np.asarray(scene['meters_to_pixel'], dtype=float)
    g = f*k
    g[2] = g[2]+(k-1)/2-np.array([k*x0, k*y0], dtype=float)
    aug = np.array([[g[0, 0], g[0, 1], 0.], [g[1, 0], g[1, 1], 0.], [g[2, 0], g[2, 1], 1.]])
    inv = np.linalg.inv(aug)
    return {**scene, 'meters_to_pixel': g.tolist(), 'pixel_to_meters': inv[:, :2].tolist(),
            'image_width': (x1-x0)*k, 'image_height': (y1-y0)*k}


def ring_to_crop(ring, box, k):
    ring = np.asarray(ring, dtype=float)
    return k*(ring-[box[0], box[1]]+.5)-.5


# --- effective resolution measures -------------------------------------------------------------------------------

def to_gray(image):
    image = np.asarray(image)
    if image.ndim == 3:
        image = cv2.cvtColor(np.ascontiguousarray(image, np.uint8), cv2.COLOR_RGB2GRAY)
    return image.astype(np.float32)


def spectral_octaves(gray, block=8):
    """Power in the top octave of the sampling band (0.25-0.5 cycles/px) relative to the octave below (0.125-0.25).

    Hann-windowed periodogram; the axes and JPEG block lines (multiples of 1/block cycles/px along either axis, +-1.5
    bins) are masked so block artefacts do not pass for detail; each band's mean density is scaled by its full
    annulus area. A natural 1/f^2 image gives a ratio near 1 less its MTF loss; a tile upsampled 2x from the next
    coarser zoom has almost no power in the top octave (ratio well below 0.1)."""
    g = np.asarray(gray, np.float64)
    g = g-g.mean()
    h, w = g.shape
    window = np.outer(np.hanning(h), np.hanning(w))
    power = np.abs(np.fft.fftshift(np.fft.fft2(g*window)))**2
    fy = np.fft.fftshift(np.fft.fftfreq(h))[:, None]
    fx = np.fft.fftshift(np.fft.fftfreq(w))[None, :]
    radius = np.hypot(fx, fy)
    mask = np.ones((h, w), bool)
    for m in range(0, block//2+1):
        for sign in (1, -1):
            mask &= np.abs(fx-sign*m/block) > 1.5/w
            mask &= np.abs(fy-sign*m/block) > 1.5/h
    bands = {'top': (.25, .5), 'next': (.125, .25), 'third': (.0625, .125)}
    totals = {}
    for name, (lo, hi) in bands.items():
        ring = (radius >= lo) & (radius < hi if name != 'top' else radius <= hi)
        kept = ring & mask
        totals[name] = float(power[kept].mean())*float(ring.sum()) if kept.any() else None
    ratio = totals['top']/totals['next'] if totals['top'] and totals['next'] else None
    ratio2 = totals['next']/totals['third'] if totals['next'] and totals['third'] else None
    return {'top_octave_ratio': _num(ratio), 'next_octave_ratio': _num(ratio2),
            'band_power': {k: _num(v, 1) for k, v in totals.items()},
            'bands_cycles_per_px': bands, 'masked': f'axes and JPEG {block} px block lines (+-1.5 bins)'}


def edge_spread(gray, mpp, *, span_m=1.0, top_fraction=.03, max_edges=3000, min_contrast=30., seed=0):
    """Mean edge spread function of the strongest edges on a metric grid and the MTF derived from it.

    Edge pixels: top `top_fraction` of Sobel magnitude after a Gaussian of 0.12 m (direction only). Each profile is
    sampled along the gradient over +-span_m at mpp/4 steps, normalised by its end plateaus (contrast >= min_contrast
    grey levels), re-centred on its own 50 % crossing and averaged. Width is 10-90 % of the mean ESF; the MTF is the
    windowed FFT of its derivative. The smallest resolvable bar is half the period where the MTF falls to 0.1; when
    the MTF stays above 0.1 up to the sampling Nyquist the result is sampling-limited (bar = one pixel)."""
    from scipy.ndimage import map_coordinates
    g = np.asarray(gray, np.float32)
    sigma = max(.5, .12/mpp)
    smooth = cv2.GaussianBlur(g, (0, 0), sigma)
    gx = cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = np.hypot(gx, gy)
    border = int(math.ceil(span_m/mpp))+2
    inner = np.zeros_like(magnitude, bool)
    if g.shape[0] > 2*border and g.shape[1] > 2*border:
        inner[border:-border, border:-border] = True
    if not inner.any():
        return {'edges_used': 0, 'reason': 'sample too small for the profile span'}
    threshold = np.quantile(magnitude[inner], 1-top_fraction)
    ys, xs = np.nonzero(inner & (magnitude >= threshold) & (magnitude > 0))
    rng = np.random.default_rng(seed)
    if len(xs) > max_edges:
        pick = rng.choice(len(xs), max_edges, replace=False)
        ys, xs = ys[pick], xs[pick]
    step_m = mpp/4
    t = np.arange(-span_m, span_m+step_m/2, step_m)
    nx, ny = gx[ys, xs]/magnitude[ys, xs], gy[ys, xs]/magnitude[ys, xs]
    px = xs[:, None]+nx[:, None]*(t[None, :]/mpp)
    py = ys[:, None]+ny[:, None]*(t[None, :]/mpp)
    profiles = map_coordinates(g, [py.ravel(), px.ravel()], order=1, mode='nearest').reshape(px.shape)
    end = max(3, len(t)//5)
    lo = np.median(profiles[:, :end], axis=1)
    hi = np.median(profiles[:, -end:], axis=1)
    contrast = hi-lo
    keep = contrast >= min_contrast
    grid = np.arange(-.75*span_m, .75*span_m+step_m/2, step_m)
    aligned = []
    for profile, low, c in zip(profiles[keep], lo[keep], contrast[keep], strict=True):
        e = (profile-low)/c
        centre = np.abs(t) <= .5*span_m
        if e[centre].min() < -.35 or e[centre].max() > 1.35:
            continue
        crossings = np.nonzero((e[:-1] < .5) & (e[1:] >= .5))[0]
        if not len(crossings):
            continue
        i = crossings[np.argmin(np.abs(t[crossings]))]
        t50 = t[i]+(0.5-e[i])/(e[i+1]-e[i])*(t[i+1]-t[i])
        aligned.append(np.interp(grid+t50, t, e))
    if len(aligned) < 20:
        return {'edges_used': len(aligned), 'reason': 'fewer than 20 usable edge profiles'}
    esf = np.mean(aligned, axis=0)
    width = _crossing_width(grid, esf)
    lsf = np.gradient(esf, step_m)*np.hanning(len(esf))
    spectrum = np.abs(np.fft.rfft(lsf))
    freqs = np.fft.rfftfreq(len(lsf), d=step_m)
    mtf = spectrum/spectrum[0] if spectrum[0] > 0 else spectrum
    nyquist = 1/(2*mpp)
    f50, f10 = _first_below(freqs, mtf, .5), _first_below(freqs, mtf, .1)
    limited = f10 is None or f10 > nyquist
    bar = mpp if limited else 1/(2*f10)
    return {'edges_used': len(aligned), 'edges_candidates': int(len(xs)), 'span_m': span_m,
            'edge_width_10_90_m': _num(width), 'mtf50_cycles_per_m': _num(f50), 'mtf50_period_m': _num(1/f50) if f50 else None,
            'mtf10_cycles_per_m': _num(f10), 'nyquist_cycles_per_m': _num(nyquist),
            'smallest_resolvable_bar_m': _num(bar), 'sampling_limited': bool(limited),
            'mtf_at_nyquist': _num(float(np.interp(nyquist, freqs, mtf)))}


def _crossing_width(grid, esf):
    def cross(level):
        idx = np.nonzero((esf[:-1] < level) & (esf[1:] >= level))[0]
        if not len(idx):
            return None
        i = idx[np.argmin(np.abs(grid[idx]))]
        return grid[i]+(level-esf[i])/(esf[i+1]-esf[i])*(grid[i+1]-grid[i])
    a, b = cross(.1), cross(.9)
    return None if a is None or b is None else b-a


def _first_below(freqs, values, level):
    idx = np.nonzero(values < level)[0]
    if not len(idx) or idx[0] == 0:
        return None
    i = idx[0]
    return float(freqs[i-1]+(values[i-1]-level)/(values[i-1]-values[i])*(freqs[i]-freqs[i-1]))


def gradient_energy(gray, mpp):
    """Mean squared Sobel gradient in grey levels per metre (and per pixel)."""
    g = np.asarray(gray, np.float32)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)/8
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)/8
    per_px = float(np.mean(gx*gx+gy*gy))
    return {'per_px2': _num(per_px, 3), 'per_m2': _num(per_px/mpp**2, 1)}


def cross_zoom(gray_hi, gray_lo):
    """Same area at two zooms (gray_hi is an integer factor finer): same-capture NCC and unexplained detail.

    own_detail = var(hi - up(down(hi)))/var(hi): the hi image's own top-octave share. unexplained =
    var(hi - up(lo))/var(hi): what the coarser zoom cannot predict. An upsampled hi has own_detail near zero."""
    hi = np.asarray(gray_hi, np.float32)
    lo = np.asarray(gray_lo, np.float32)
    factor = hi.shape[0]/lo.shape[0]
    if abs(factor-round(factor)) > 1e-6 or abs(hi.shape[1]/lo.shape[1]-factor) > 1e-6:
        raise ValueError('cross_zoom needs an integer scale factor between the two samples')
    size = (lo.shape[1], lo.shape[0])
    down = cv2.resize(hi, size, interpolation=cv2.INTER_AREA)
    up_lo = cv2.resize(lo, (hi.shape[1], hi.shape[0]), interpolation=cv2.INTER_CUBIC)
    up_down = cv2.resize(down, (hi.shape[1], hi.shape[0]), interpolation=cv2.INTER_CUBIC)
    var = float(hi.var()) or 1.
    return {'factor': int(round(factor)), 'ncc_down_vs_lo': _num(np.corrcoef(down.ravel(), lo.ravel())[0, 1]),
            'own_detail_fraction': _num(float((hi-up_down).var())/var), 'unexplained_fraction': _num(float((hi-up_lo).var())/var)}


def measure_resolution(image, mpp):
    gray = to_gray(image)
    return {'meters_per_pixel': _num(mpp, 5), 'shape': list(gray.shape), 'spectral': spectral_octaves(gray),
            'edges': edge_spread(gray, mpp), 'gradient_energy': gradient_energy(gray, mpp)}


# --- registration against the saved PNG --------------------------------------------------------------------------

def box_average(hires, k, size):
    return cv2.resize(np.ascontiguousarray(hires), size, interpolation=cv2.INTER_AREA) if k > 1 else np.asarray(hires)


def global_phase(saved_rgb, capture_rgb):
    """Whole-frame phase correlation of the scene_local_stack matching signal: (dx, dy, response) in saved px."""
    from .scene_local_stack import matching_signal
    a, b = matching_signal(saved_rgb), matching_signal(capture_rgb)
    hann = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(a, b, hann)
    return {'dx_px': _num(dx), 'dy_px': _num(dy), 'shift_px': _num(math.hypot(dx, dy)), 'response': _num(response)}


def check_registration(saved_rgb, hires, k, saved_mpp, **knobs):
    """register_local on the k x k box average of the hi-res raster against the saved PNG (same grid)."""
    from . import scene_local_stack as sls
    saved_rgb = np.ascontiguousarray(saved_rgb, np.uint8)
    height, width = saved_rgb.shape[:2]
    down = box_average(hires, k, (width, height))
    result = {'global': global_phase(saved_rgb, down),
              'ncc_gray': _num(np.corrcoef(to_gray(saved_rgb).ravel(), to_gray(down).ravel())[0, 1]),
              'saved_mpp': _num(saved_mpp, 5), 'k': k}
    window = min(knobs.pop('window_px', sls.DEFAULTS['window_px']), min(width, height))
    stride = knobs.pop('stride_px', max(16, window//2))
    try:
        local = sls.register_local(saved_rgb, down, window_px=int(window), stride_px=int(stride), **knobs)
    except ValueError as error:
        result['local'] = {'accepted': False, 'status': 'error', 'reason': str(error)}
        return result, None
    reg = dict(local['registration'])
    for key in ('holdout_residual_px', 'median_shift_px', 'p95_shift_px', 'max_shift_px'):
        value = reg.get(key)
        reg[key.replace('_px', '_m')] = _num(value*saved_mpp) if value is not None else None
        reg[key.replace('_px', '_hires_px')] = _num(value*k) if value is not None else None
    result['local'] = reg
    return result, local


def apply_field(hires, local, k):
    """Warp the hi-res raster by the accepted per-window field (saved px, scaled by k); NaN cells take the median."""
    dx, dy = local['dx'], local['dy']
    finite = np.isfinite(dx) & np.isfinite(dy)
    mx = float(np.median(dx[finite])) if finite.any() else 0.
    my = float(np.median(dy[finite])) if finite.any() else 0.
    dx, dy = np.where(finite, dx, mx).astype(np.float32), np.where(finite, dy, my).astype(np.float32)
    h, w = hires.shape[:2]
    dx_hi = cv2.resize(dx, (w, h), interpolation=cv2.INTER_LINEAR)*k
    dy_hi = cv2.resize(dy, (w, h), interpolation=cv2.INTER_LINEAR)*k
    xs, ys = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    return cv2.remap(hires, xs+dx_hi, ys+dy_hi, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


# --- scene fetch -------------------------------------------------------------------------------------------------

def load_scene(root, scene_id):
    from . import structure_kinds as sk
    scene = sk.load_scene(root, scene_id)
    image = sk.load_image(root, scene)
    if image is None:
        raise ValueError(f'{scene_id}: saved PNG missing or the wrong size')
    return scene, image


def saved_mpp(scene):
    frame = np.asarray(scene['pixel_to_meters'], dtype=float)
    return float(math.sqrt(abs(np.linalg.det(frame[:2, :2]))))


def _png(folder, name, array):
    folder.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.ascontiguousarray(array)).save(folder/name, format='PNG')
    return {'path': name, 'sha256': hashlib.sha256((folder/name).read_bytes()).hexdigest()}


def output_folder(root, scene_id, provider, zoom, k, release=None, box=None):
    name = f'{provider}{"-"+str(release) if release else ""}-z{zoom}-k{k}'
    if box is not None:
        name += '-box' + '_'.join(str(int(v)) for v in box)
    return Path(root)/OUT/'scenes'/scene_id/name


def fetch_scene(scene_id, provider, *, zoom=None, k=None, root=Path('.'), offline=False, session=None, release=None,
                box=None, write=True):
    """Registered hi-res raster for a saved scene frame; returns (report, image)."""
    root = Path(root)
    p = PROVIDERS[provider]
    zoom = int(zoom or p.recommended_zoom or p.served_max_zoom)
    started = time.monotonic()
    scene, saved = load_scene(root, scene_id)
    cache = TileCache(p, root, offline=offline, session=session, release=release)
    rep = reproject(scene, cache, zoom, k=k, box=box)
    kk = rep['k']
    x0, y0, x1, y1 = rep['box']
    mpp = saved_mpp(scene)
    valid_fraction = float(rep['valid'].mean())
    local = None
    if valid_fraction >= .5:
        registration, local = check_registration(saved[y0:y1, x0:x1], rep['image'], kk, mpp)
    else:
        registration = {'local': {'accepted': False, 'status': 'rejected',
                                  'reason': f'only {valid_fraction:.2f} of the frame has provider tiles'}}
    image = rep['image']
    accepted = bool(local and local['registration']['accepted'])
    alignment = {'applied': False, 'min_apply_shift_px': MIN_APPLY_SHIFT_PX}
    if accepted:
        median = local['registration'].get('median_shift_px') or 0.
        if median >= MIN_APPLY_SHIFT_PX:
            image = apply_field(image, local, kk)
            alignment.update(applied=True, reason=f'accepted field applied (median shift {median:g} saved px, scaled by k={kk})')
        else:
            alignment['reason'] = f'accepted; median shift {median:g} saved px is below {MIN_APPLY_SHIFT_PX} px: already on the saved grid'
    else:
        alignment['reason'] = 'registration not accepted: raster written unaligned, do not overlay saved outlines on it'
    cache.save()
    report = {'version': VERSION, 'code_sha256': code_sha256(), 'computed_at': _now(), 'scene_id': scene_id,
              'saved_image_sha256': scene.get('image_sha256'), 'saved_meters_per_pixel': _num(mpp, 6),
              'provider': p.name, 'release': release, 'provider_title': p.title, 'licence': p.licence, 'cost': p.cost,
              'native_gsd': p.native_gsd, 'dates': p.dates, 'zoom': zoom,
              'zoom_meters_per_pixel_at_centroid': _num(zoom_mpp(scene['centroid']['lat'], zoom), 6),
              'k': kk, 'hires_meters_per_pixel': _num(mpp/kk, 6), 'box_saved_px': rep['box'],
              'hires_shape': list(image.shape[:2]), 'method': rep['method'], 'integer_crop': rep['integer_crop'],
              'zoom_px_per_saved_px': rep['ratio'], 'mosaic_origin_zoom_px': rep['mosaic_origin'],
              'mapping': 'hi-res pixel q covers saved edge coordinates [q/k, (q+1)/k]; saved ring index p -> hi-res '
                         'index k*(p - x0 + 0.5) - 0.5',
              'valid_fraction': _num(valid_fraction), 'registration': registration, 'aligned': accepted,
              'alignment': alignment, 'tiles': rep['tiles'], 'tile_manifest': str(cache.manifest_path(zoom).relative_to(root)),
              'cache': dict(cache.stats), 'elapsed_seconds': round(time.monotonic()-started, 2),
              'note': 'Diagnostic view registered onto the saved grid; not an evidence image. The saved PNG stays the '
                      'evidence gate.'}
    if write:
        folder = output_folder(root, scene_id, p.name, zoom, kk, release, box)
        report['assets'] = {'hires.png': _png(folder, 'hires.png', image)}
        if valid_fraction < 1:
            report['assets']['valid.png'] = _png(folder, 'valid.png', rep['valid'].astype(np.uint8)*255)
        report['folder'] = str(folder.relative_to(root))
        _atomic_json(folder/'report.json', report)
    return report, image


# --- probe: effective resolution around a point ------------------------------------------------------------------

def esri_metadata(lon, lat, session=None):
    """World Imagery identify at a point: per-layer source, resolution, date and map levels."""
    url = 'https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/identify'
    params = {'f': 'json', 'geometry': f'{lon},{lat}', 'geometryType': 'esriGeometryPoint', 'sr': 4326, 'layers': 'all',
              'tolerance': 1, 'mapExtent': f'{lon-.01},{lat-.01},{lon+.01},{lat+.01}', 'imageDisplay': '800,800,96',
              'returnGeometry': 'false'}
    response = (session or requests).get(url, params=params, timeout=20, headers={'User-Agent': BROWSER_UA})
    response.raise_for_status()
    out = []
    for r in response.json().get('results', []):
        a = r.get('attributes', {})
        out.append({'layer': r.get('layerName'), 'source': a.get('SOURCE') or a.get('NICE_DESC'),
                    'product': a.get('SOURCE_INFO') or a.get('NICE_NAME'),
                    'src_res_m': a.get('RESOLUTION (M)') or a.get('SRC_RES'), 'samp_res_m': a.get('SAMP_RES'),
                    'date': a.get('SRC_DATE2'), 'min_level': a.get('MinMapLevel'), 'max_level': a.get('MaxMapLevel'),
                    'release': a.get('ReleaseName')})
    return out


def _centre(gray, half, factor, half_c):
    lo, hi = (half-half_c)*factor, (half+half_c)*factor
    return gray[lo:hi, lo:hi]


def _label(image, lines, size=15):
    canvas = Image.fromarray(image) if isinstance(image, np.ndarray) else image
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=size)
    y = 4
    for line in lines:
        box = draw.textbbox((6, y), line, font=font)
        draw.rectangle((box[0]-3, box[1]-2, box[2]+3, box[3]+2), fill=(0, 0, 0))
        draw.text((6, y), line, fill=(255, 255, 255), font=font)
        y = box[3]+5
    return canvas


def _panel(crop, size):
    h, w = crop.shape[:2]
    interp = cv2.INTER_AREA if w > size else cv2.INTER_LANCZOS4
    return cv2.resize(np.ascontiguousarray(crop), (size, size), interpolation=interp)


DEFAULT_PROBE = (('google', 19), ('google', 20), ('google', 21), ('google', 22), ('esri', 19), ('esri', 20))


def probe(root=Path('.'), *, lat=CAMBRIDGE['lat'], lon=CAMBRIDGE['lon'], size_m=200., specs=DEFAULT_PROBE,
          offline=False, session=None, small_zoom=22, small_size_m=64., sheet_m=24., write=True, log=None):
    """Fetch a size_m square around (lat, lon) per (provider, zoom) and measure its effective resolution.

    The square is aligned to the z19 pixel grid so every zoom covers exactly the same ground; zooms >= small_zoom use a
    small_size_m square (they are 4x the tiles of the zoom below). Cross-zoom rows compare each zoom with the next
    coarser zoom of the same provider on their common centre square."""
    root = Path(root)
    gx, gy = lonlat_to_tile_xy(lon, lat, 19)
    cx, cy = int(round(gx*TILE_PX)), int(round(gy*TILE_PX))
    mpp19 = zoom_mpp(lat, 19)
    rows, grays, crops = [], {}, []
    for name, zoom in specs:
        provider = PROVIDERS[name]
        cache = TileCache(provider, root, offline=offline, session=session)
        size = small_size_m if zoom >= small_zoom else size_m
        half = int(round(size/mpp19/2))
        factor = 2**(zoom-19)
        box = ((cx-half)*factor, (cy-half)*factor, (cx+half)*factor, (cy+half)*factor)
        started = time.monotonic()
        image, valid, tiles = mosaic(cache, zoom, *box)
        cache.save()
        mpp = zoom_mpp(lat, zoom)
        row = {'provider': name, 'zoom': zoom, 'size_m': _num(2*half*mpp19, 2), 'meters_per_pixel': _num(mpp, 5),
               'tiles': len(tiles), 'tiles_ok': sum(1 for t in tiles if t[2] and len(str(t[2])) == 64),
               'tiles_placeholder': sum(1 for t in tiles if t[2] == 'placeholder'),
               'tiles_missing': sum(1 for t in tiles if t[2] in ('missing', 'unavailable')),
               'coverage': _num(valid.mean()), 'cache': dict(cache.stats), 'resolution': None}
        if valid.mean() >= .98:
            row['resolution'] = measure_resolution(image, mpp)
            grays[(name, zoom)] = (to_gray(image), half, factor)
            half_c = int(round(sheet_m/mpp19/2))
            crops.append((f'{name} z{zoom}', f'{mpp:.3f} m/px', _centre(image, half, factor, half_c)))
        else:
            row['reason'] = 'provider has no imagery at this zoom here (placeholder or missing tiles)'
        row['seconds'] = round(time.monotonic()-started, 1)
        rows.append(row)
        if log:
            log(f'{name} z{zoom}: {row["tiles"]} tiles, coverage {row["coverage"]}, {row["seconds"]} s')
    for row in rows:
        lower = (row['provider'], row['zoom']-1)
        here = (row['provider'], row['zoom'])
        if here in grays and lower in grays:
            (g_hi, h_hi, f_hi), (g_lo, h_lo, f_lo) = grays[here], grays[lower]
            half_c = min(h_hi, h_lo)
            row['cross_zoom_vs_lower'] = {'lower_zoom': row['zoom']-1, 'common_size_m': _num(2*half_c*mpp19, 2),
                                          **cross_zoom(_centre(g_hi, h_hi, f_hi, half_c), _centre(g_lo, h_lo, f_lo, half_c))}
    metadata = None
    if not offline:
        try:
            metadata = esri_metadata(lon, lat, session=session)
        except (requests.RequestException, ValueError) as error:
            metadata = {'error': f'{type(error).__name__}: {error}'[:200]}
    report = {'version': VERSION, 'code_sha256': code_sha256(), 'computed_at': _now(),
              'point': {'lat': lat, 'lon': lon}, 'z19_centre_px': [cx, cy], 'size_m': size_m,
              'small_zoom_size_m': small_size_m, 'rows': rows, 'esri_world_imagery_metadata': metadata,
              'method': {'spectral': 'top_octave_ratio = power 0.25-0.5 cycles/px over 0.125-0.25 (axes and JPEG block '
                                     'lines masked); ~1 scale-invariant, <0.1 upsampled',
                         'edges': 'mean ESF of the top 3 % gradient edges on a metric grid; smallest resolvable bar = '
                                  '1/(2 f) where the MTF falls to 0.1 (one pixel when sampling-limited)',
                         'cross_zoom': 'own_detail_fraction = var(hi - up(down(hi)))/var(hi); unexplained_fraction = '
                                       'var(hi - up(lower zoom))/var(hi)'}}
    if write:
        folder = root/OUT/'probe'
        _atomic_json(folder/'probe.json', report)
        if crops:
            size = 360
            panels = [np.asarray(_label(_panel(c, size), [title, sub])) for title, sub, c in crops]
            columns = 4
            rows_img = []
            for i in range(0, len(panels), columns):
                chunk = panels[i:i+columns]
                chunk += [np.full_like(panels[0], 255)]*(columns-len(chunk))
                rows_img.append(np.hstack(chunk))
            sheet = _label(np.vstack(rows_img), [])
            sheet.save(folder/'probe-sheet.png')
            report['sheet'] = str((folder/'probe-sheet.png').relative_to(root))
            _atomic_json(folder/'probe.json', report)
    return report


# --- contact sheet -------------------------------------------------------------------------------------------------

def _target_centre(scene, target):
    if target.get('object'):
        obj = next((o for o in scene['objects'] if o.get('id') == target['object']), None)
        if obj is None:
            raise ValueError(f"{target['object']} is not in {scene['id']}")
        ring = np.asarray(obj['ring'], dtype=float)
        return (ring[:, 0].min()+ring[:, 0].max())/2+.5, (ring[:, 1].min()+ring[:, 1].max())/2+.5, obj
    lon, lat = target['lonlat']
    x, y = lonlat_to_saved_px(scene, lon, lat)
    return x, y, None


def contact_sheet(root=Path('.'), targets=(), *, arms=(('saved', None), ('google', 20), ('google', 21), ('esri', 20)),
                  size_m=24., panel=440, offline=False, session=None, out=None):
    """One row per target: the saved image, then each (provider, zoom) arm on the same ground, same panel size."""
    root = Path(root)
    rows, facts = [], []
    for target in targets:
        scene, saved = load_scene(root, target['scene'])
        mpp = saved_mpp(scene)
        cx, cy, obj = _target_centre(scene, target)
        half = size_m/mpp/2
        box = (max(0, int(math.floor(cx-half))), max(0, int(math.floor(cy-half))),
               min(scene['image_width'], int(math.ceil(cx+half))), min(scene['image_height'], int(math.ceil(cy+half))))
        x0, y0, x1, y1 = box
        panels, row_facts = [], {'scene': scene['id'], 'object': target.get('object'), 'box_saved_px': list(box),
                                 'label': target.get('label'), 'arms': []}
        for name, zoom in arms:
            if name == 'saved':
                crop, lines = saved[y0:y1, x0:x1], ['saved scene PNG', f'{mpp:.3f} m/px (Google z19)']
                row_facts['arms'].append({'arm': 'saved', 'meters_per_pixel': _num(mpp)})
            else:
                cache = TileCache(name, root, offline=offline, session=session)
                rep = reproject(scene, cache, zoom, box=box)
                cache.save()
                crop, k = rep['image'], rep['k']
                reg = global_phase(saved[y0:y1, x0:x1], box_average(crop, k, (x1-x0, y1-y0)))
                lines = [f'{name} z{zoom} (k={k})', f'{mpp/k:.3f} m/px; shift {reg["shift_px"]:.2f} saved px',
                         f'valid {rep["valid"].mean():.2f}']
                row_facts['arms'].append({'arm': f'{name} z{zoom}', 'k': k, 'meters_per_pixel': _num(mpp/k),
                                          'phase_shift_saved_px': reg['shift_px'], 'response': reg['response'],
                                          'valid_fraction': _num(rep['valid'].mean()), 'integer_crop': rep['integer_crop']})
            panels.append(np.asarray(_label(_panel(crop, panel), lines)))
        title = target.get('label') or (f"{scene['id']} / {target.get('object')}" if target.get('object') else scene['id'])
        strip = np.hstack(panels)
        header = np.full((30, strip.shape[1], 3), 255, np.uint8)
        header = np.asarray(_label(header, [f'{title}: {size_m:g} m square'], size=16))
        rows.append(np.vstack([header, strip]))
        facts.append(row_facts)
    sheet = Image.fromarray(np.vstack(rows))
    out = Path(out) if out else root/OUT/'contact-sheet.png'
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    _atomic_json(out.with_suffix('.json'), {'version': VERSION, 'computed_at': _now(), 'size_m': size_m,
                                            'sheet': out.name, 'sha256': hashlib.sha256(out.read_bytes()).hexdigest(),
                                            'rows': facts})
    return out, facts


# --- paired structure measurement -----------------------------------------------------------------------------------

def enhance_metric(rgb, px_m, tile_m=PRODUCTION_CLAHE_TILE_M, clip=3.0):
    """structure_kinds.enhance (CLAHE on LAB L, clip 3) with the tile grid set in metres, so a crop at any k gets the
    same tile size on the ground (the production 12 x 12 grid over a 272 m scene is 22.6 m per tile)."""
    data = np.ascontiguousarray(np.asarray(rgb, np.uint8))
    h, w = data.shape[:2]
    grid = (max(1, int(round(w*px_m/tile_m))), max(1, int(round(h*px_m/tile_m))))
    lab = cv2.cvtColor(data, cv2.COLOR_RGB2LAB)
    lab[:, :, 0] = cv2.createCLAHE(clipLimit=clip, tileGridSize=grid).apply(lab[:, :, 0])
    return np.ascontiguousarray(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB))


def measure_arm(crop, scene, box, k, shape, ring, geometry, others):
    """Appearance (rib anisotropy/period), oriented ribs and outline edge support on one crop at factor k."""
    from . import scene_roof_diagnostics as sd
    from . import structure_kinds as sk
    from . import structure_kinds_signals as sks
    frame = derived_frame(scene, box, k)
    px_m = sk.pixel_size_m(frame)
    app = sk.appearance_features(enhance_metric(crop, px_m), shape, frame, geometry, others)
    gray = to_gray(crop)
    inner = shape.buffer(-sks.PARAMS['erode_m'])
    inside = sk._raster(sk.to_pixels(shape if inner.is_empty else inner, frame), (0, 0), gray.shape)
    ribs = None
    if inside.sum() >= sks.PARAMS['min_pixels'] and geometry.get('long_axis_deg') is not None:
        ribs = sks.oriented_ribs(gray, inside, sk._pixel_axis_deg(geometry['long_axis_deg'], frame), px_m)
        for key in ('along', 'across'):
            period = ribs.get(f'period_{key}_m')
            ribs[f'lag_{key}_px'] = _num(period/px_m, 2) if period else None
    ring_px = ring_to_crop(ring, box, k)
    edges = sd.canny_edges(gray.astype(np.uint8))
    distance = sd.edge_distance(edges)
    native = sd.edge_support(ring_px, edges, distance=distance)
    metric = native if k == 1 else sd.edge_support(ring_px, edges, distance=distance/k)
    keep = ('median', 'weighted_mean', 'unsupported_fraction')
    vertex = [v for v in native['vertex_distance_px'] if v is not None]
    period = app.get('rib_period_m')
    # Controls for the metric tolerance: the share of the crop within tolerance of any edge (what a random curve
    # scores) and the same outline displaced by CONTROL_SHIFT_M in four directions (a wrong outline nearby).
    step = CONTROL_SHIFT_M/px_m
    displaced = [sd.edge_support(ring_px+[dx, dy], edges, distance=distance/k)['weighted_mean']
                 for dx, dy in ((step, 0), (-step, 0), (0, step), (0, -step))]
    displaced = [v for v in displaced if v is not None]
    displaced_native = [sd.edge_support(ring_px+[dx, dy], edges, distance=distance)['weighted_mean']
                        for dx, dy in ((step, 0), (-step, 0), (0, step), (0, -step))]
    displaced_native = [v for v in displaced_native if v is not None]
    return {'k': k, 'px_m': _num(px_m, 5), 'shape': list(gray.shape),
            'appearance': {'rib_anisotropy': app.get('rib_anisotropy'), 'rib_period_m': period,
                           'rib_period_lag_px': _num(period/px_m, 2) if period else None,
                           'rib_axis_alignment': app.get('rib_axis_alignment'),
                           'brightness_median': app.get('brightness_median'), 'interior_pixels': app.get('interior_pixels'),
                           'image': app.get('image')},
            'ribs_oriented': ribs,
            'edge_support': {'native': {key: native.get(key) for key in keep}, 'metric': {key: metric.get(key) for key in keep},
                             'native_tolerance_m': _num(sd.EDGE_TOLERANCE_PX*px_m), 'metric_tolerance_m': _num(sd.EDGE_TOLERANCE_PX*px_m*k),
                             'vertex_distance_median_m': _num(float(np.median(vertex))*px_m) if vertex else None,
                             'edge_pixel_fraction': _num(float((edges > 0).mean())),
                             'chance_metric': _num(float((distance/k <= sd.EDGE_TOLERANCE_PX).mean())),
                             'displaced_metric': _num(float(np.mean(displaced))) if displaced else None,
                             'chance_native': _num(float((distance <= sd.EDGE_TOLERANCE_PX).mean())),
                             'displaced_native': _num(float(np.mean(displaced_native))) if displaced_native else None,
                             'control_shift_m': CONTROL_SHIFT_M}}


def paired(root=Path('.'), sample=None, *, provider='google', zooms=(20, 21), offline=False, session=None, pad_m=4.,
           write=True, log=None, limit=None):
    """Structure measures on the saved image and on hi-res rasters for the (scene, object) pairs in `sample`."""
    from . import structure_kinds as sk
    root = Path(root)
    pairs = json.loads(Path(sample).read_text()) if not isinstance(sample, (list, tuple)) else list(sample)
    pairs = [tuple(p) for p in pairs][:limit] if limit else [tuple(p) for p in pairs]
    cache = TileCache(provider, root, offline=offline, session=session)
    by_scene = {}
    for sid, oid in pairs:
        by_scene.setdefault(sid, []).append(oid)
    rows = []
    started = time.monotonic()
    for i, (sid, oids) in enumerate(by_scene.items()):
        scene, image = load_scene(root, sid)
        shapes = sk.building_shapes(scene)
        objects = {o.get('id'): o for o in scene.get('objects', [])}
        enhanced_full = sk.enhance(image)
        width, height = scene['image_width'], scene['image_height']
        for oid in oids:
            row = {'scene_id': sid, 'object_id': oid}
            shape, obj = shapes.get(oid), objects.get(oid)
            if shape is None or obj is None:
                rows.append({**row, 'error': 'object is not a live building in the scene'})
                continue
            geometry = sk.geometry_features(shape, vertex_count=len(obj.get('ring') or []))
            others = [s for o, s in shapes.items() if o != oid]
            production = sk.appearance_features(enhanced_full, shape, scene, geometry, others)
            row['geometry'] = {key: geometry.get(key) for key in ('area_m2', 'rect_width_m', 'rect_length_m', 'long_axis_deg')}
            row['production'] = {key: production.get(key) for key in ('rib_anisotropy', 'rib_period_m', 'rib_axis_alignment')}
            b = sk.to_pixels(shape.buffer(pad_m), scene).bounds
            box = (max(0, int(math.floor(b[0]))), max(0, int(math.floor(b[1]))),
                   min(width, int(math.ceil(b[2]))+1), min(height, int(math.ceil(b[3]))+1))
            x0, y0, x1, y1 = box
            row['box_saved_px'] = list(box)
            arms = {'saved': measure_arm(image[y0:y1, x0:x1], scene, box, 1, shape, obj['ring'], geometry, others)}
            for zoom in zooms:
                rep = reproject(scene, cache, zoom, box=box)
                if rep['valid'].mean() < .9:
                    arms[f'z{zoom}'] = {'error': 'provider tiles unavailable', 'valid_fraction': _num(rep['valid'].mean())}
                    continue
                arm = measure_arm(rep['image'], scene, box, rep['k'], shape, obj['ring'], geometry, others)
                arm['registration'] = global_phase(image[y0:y1, x0:x1], box_average(rep['image'], rep['k'], (x1-x0, y1-y0)))
                arm['integer_crop'] = rep['integer_crop']
                arms[f'z{zoom}'] = arm
            row['arms'] = arms
            rows.append(row)
        cache.save()
        if log:
            log(f'[{i+1}/{len(by_scene)}] {sid}: {len(oids)} objects, {time.monotonic()-started:.0f} s, cache {cache.stats}')
    summary = summarise_paired(rows, zooms)
    summary.update({'version': VERSION, 'code_sha256': code_sha256(), 'computed_at': _now(), 'provider': provider,
                    'zooms': list(zooms), 'pad_m': pad_m, 'sample': str(sample) if not isinstance(sample, (list, tuple)) else None,
                    'cache': dict(cache.stats), 'elapsed_seconds': round(time.monotonic()-started, 1)})
    if write:
        stem = Path(sample).stem if not isinstance(sample, (list, tuple)) else 'pairs'
        folder = root/OUT/'paired'/f'{provider}-{stem}'
        _atomic_json(folder/'rows.json', rows)
        _atomic_json(folder/'summary.json', summary)
        summary['folder'] = str(folder.relative_to(root))
    return summary, rows


def _dist(values):
    values = [float(v) for v in values if v is not None]
    if not values:
        return {'n': 0}
    q = np.percentile(values, [25, 50, 75])
    return {'n': len(values), 'p25': _num(q[0]), 'median': _num(q[1]), 'p75': _num(q[2]),
            'min': _num(min(values)), 'max': _num(max(values))}


def _paired_test(a, b):
    """Median of b - a over pairs where both exist, sign counts and a Wilcoxon signed-rank p (two-sided)."""
    pairs = [(x, y) for x, y in zip(a, b, strict=True) if x is not None and y is not None]
    if not pairs:
        return {'n': 0}
    diffs = np.array([y-x for x, y in pairs], float)
    out = {'n': len(pairs), 'median_diff': _num(np.median(diffs)), 'higher': int((diffs > 1e-9).sum()),
           'lower': int((diffs < -1e-9).sum()), 'equal': int((np.abs(diffs) <= 1e-9).sum())}
    if (np.abs(diffs) > 1e-9).sum() >= 6:
        from scipy.stats import wilcoxon
        out['wilcoxon_p'] = _num(wilcoxon(diffs[np.abs(diffs) > 1e-9]).pvalue)
    return out


def _minus(a, b):
    return None if a is None or b is None else a-b


def summarise_paired(rows, zooms):
    ok = [r for r in rows if r.get('arms')]
    names = ['saved', *[f'z{z}' for z in zooms]]

    def get(row, arm, *path):
        value = (row.get('arms') or {}).get(arm)
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        return value

    arms = {}
    for name in names:
        present = [r for r in ok if isinstance(get(r, name), dict) and 'error' not in get(r, name)]
        period = [get(r, name, 'appearance', 'rib_period_m') for r in present]
        lag = [get(r, name, 'appearance', 'rib_period_lag_px') for r in present]
        along = [get(r, name, 'ribs_oriented', 'period_along_m') for r in present]
        across = [get(r, name, 'ribs_oriented', 'period_across_m') for r in present]
        lags_oriented = [v for r in present for v in (get(r, name, 'ribs_oriented', 'lag_along_px'),
                                                       get(r, name, 'ribs_oriented', 'lag_across_px')) if v is not None]
        px_m = get(present[0], name, 'px_m') if present else None
        arms[name] = {
            'n': len(present), 'meters_per_pixel': px_m,
            'rib_period_measured': sum(v is not None for v in period),
            'rib_period_m': _dist(period), 'rib_period_lag_px': _dist(lag),
            'rib_period_at_nyquist_lag_le_2_5px': sum(1 for v in lag if v is not None and v <= 2.5),
            'rib_anisotropy': _dist([get(r, name, 'appearance', 'rib_anisotropy') for r in present]),
            'oriented_along_measured': sum(v is not None for v in along),
            'oriented_across_measured': sum(v is not None for v in across),
            'oriented_either_measured': sum(1 for a, c in zip(along, across, strict=True) if a is not None or c is not None),
            'oriented_period_along_m': _dist(along), 'oriented_period_across_m': _dist(across),
            'oriented_lags_px': _dist(lags_oriented),
            'oriented_at_nyquist_lag_le_2_5px': sum(1 for v in lags_oriented if v <= 2.5),
            'edge_support_native_weighted_mean': _dist([get(r, name, 'edge_support', 'native', 'weighted_mean') for r in present]),
            'edge_support_metric_weighted_mean': _dist([get(r, name, 'edge_support', 'metric', 'weighted_mean') for r in present]),
            'edge_unsupported_fraction_metric': _dist([get(r, name, 'edge_support', 'metric', 'unsupported_fraction') for r in present]),
            'vertex_distance_median_m': _dist([get(r, name, 'edge_support', 'vertex_distance_median_m') for r in present]),
            'edge_pixel_fraction': _dist([get(r, name, 'edge_support', 'edge_pixel_fraction') for r in present]),
            'edge_chance_metric': _dist([get(r, name, 'edge_support', 'chance_metric') for r in present]),
            'edge_displaced_metric': _dist([get(r, name, 'edge_support', 'displaced_metric') for r in present]),
            'edge_lift_over_displaced': _dist([_minus(get(r, name, 'edge_support', 'metric', 'weighted_mean'),
                                                      get(r, name, 'edge_support', 'displaced_metric')) for r in present]),
            'edge_native_lift_over_displaced': _dist([_minus(get(r, name, 'edge_support', 'native', 'weighted_mean'),
                                                             get(r, name, 'edge_support', 'displaced_native')) for r in present]),
            'edge_native_chance': _dist([get(r, name, 'edge_support', 'chance_native') for r in present]),
            'edge_lift_over_chance': _dist([_minus(get(r, name, 'edge_support', 'metric', 'weighted_mean'),
                                                   get(r, name, 'edge_support', 'chance_metric')) for r in present]),
            'oriented_periods_le_0_6_m': sum(1 for r in present for key in ('period_along_m', 'period_across_m')
                                             if (get(r, name, 'ribs_oriented', key) or 99) <= .6),
            'appearance_periods_le_0_6_m': sum(1 for v in period if v is not None and v <= .6),
        }
        if name != 'saved':
            arms[name]['registration_shift_saved_px'] = _dist([get(r, name, 'registration', 'shift_px') for r in present])
            arms[name]['registration_response'] = _dist([get(r, name, 'registration', 'response') for r in present])
            arms[name]['integer_crops'] = sum(1 for r in present if get(r, name, 'integer_crop'))
    comparisons = {}
    for name in names[1:]:
        comparisons[f'{name}_vs_saved'] = {
            'edge_support_metric_weighted_mean': _paired_test(
                [get(r, 'saved', 'edge_support', 'metric', 'weighted_mean') for r in ok],
                [get(r, name, 'edge_support', 'metric', 'weighted_mean') for r in ok]),
            'edge_support_native_weighted_mean': _paired_test(
                [get(r, 'saved', 'edge_support', 'native', 'weighted_mean') for r in ok],
                [get(r, name, 'edge_support', 'native', 'weighted_mean') for r in ok]),
            'edge_lift_over_displaced': _paired_test(
                [_minus(get(r, 'saved', 'edge_support', 'metric', 'weighted_mean'), get(r, 'saved', 'edge_support', 'displaced_metric')) for r in ok],
                [_minus(get(r, name, 'edge_support', 'metric', 'weighted_mean'), get(r, name, 'edge_support', 'displaced_metric')) for r in ok]),
            'edge_native_lift_over_displaced': _paired_test(
                [_minus(get(r, 'saved', 'edge_support', 'native', 'weighted_mean'), get(r, 'saved', 'edge_support', 'displaced_native')) for r in ok],
                [_minus(get(r, name, 'edge_support', 'native', 'weighted_mean'), get(r, name, 'edge_support', 'displaced_native')) for r in ok]),
            'rib_anisotropy': _paired_test([get(r, 'saved', 'appearance', 'rib_anisotropy') for r in ok],
                                           [get(r, name, 'appearance', 'rib_anisotropy') for r in ok]),
            'vertex_distance_median_m': _paired_test([get(r, 'saved', 'edge_support', 'vertex_distance_median_m') for r in ok],
                                                     [get(r, name, 'edge_support', 'vertex_distance_median_m') for r in ok])}
    reproduction = {'rib_period_equal': sum(1 for r in ok if get(r, 'saved', 'appearance', 'rib_period_m') ==
                                            (r.get('production') or {}).get('rib_period_m')),
                    'rib_anisotropy_abs_diff': _dist([abs(get(r, 'saved', 'appearance', 'rib_anisotropy') -
                                                          r['production']['rib_anisotropy'])
                                                      for r in ok if get(r, 'saved', 'appearance', 'rib_anisotropy') is not None
                                                      and (r.get('production') or {}).get('rib_anisotropy') is not None]),
                    'production_rib_period_measured': sum(1 for r in ok if (r.get('production') or {}).get('rib_period_m')),
                    'note': 'saved-crop arm (CLAHE grid set in metres) against production appearance_features on the '
                            'full-frame CLAHE image'}
    return {'rows': len(rows), 'measured': len(ok), 'errors': len(rows)-len(ok), 'arms': arms,
            'comparisons': comparisons, 'reproduction_check': reproduction}


# --- CLI ---------------------------------------------------------------------------------------------------------------

def measured_rows(root=Path('.')):
    """{provider: [per-zoom effective-resolution numbers]} from the last probe, when one was written."""
    path = Path(root)/OUT/'probe'/'probe.json'
    if not path.exists():
        return {}
    out = {}
    for row in json.loads(path.read_text()).get('rows', []):
        res = row.get('resolution') or {}
        edges, spectral = res.get('edges') or {}, res.get('spectral') or {}
        out.setdefault(row['provider'], []).append({
            'zoom': row['zoom'], 'meters_per_pixel': row['meters_per_pixel'], 'coverage': row['coverage'],
            'edge_width_10_90_m': edges.get('edge_width_10_90_m'), 'smallest_resolvable_bar_m': edges.get('smallest_resolvable_bar_m'),
            'sampling_limited': edges.get('sampling_limited'), 'top_octave_ratio': spectral.get('top_octave_ratio'),
            'own_detail_fraction': (row.get('cross_zoom_vs_lower') or {}).get('own_detail_fraction')})
    return out


def inventory_rows(root=Path('.')):
    measured = measured_rows(root)
    return [{'provider': p.name, 'title': p.title, 'status': p.status, 'served_max_zoom_here': p.served_max_zoom,
             'native_gsd': p.native_gsd, 'dates': p.dates, 'licence': p.licence, 'cost': p.cost,
             'key_needed': p.key_env or (p.status == 'licence-required' and 'licence') or None,
             'key_present': bool(p.key_env and os.environ.get(p.key_env)), 'how_to_get': p.how_to_get or None,
             'fetchable': p.fetchable, 'tested': p.tested, 'recommended_zoom': p.recommended_zoom, 'notes': p.notes or None,
             'measured_here': measured.get(p.name)}
            for p in PROVIDERS.values()]


def _parse_specs(values):
    specs = []
    for value in values:
        name, _, zooms = value.partition(':')
        if name not in PROVIDERS or not zooms:
            raise ValueError(f'--specs expects PROVIDER:Z[,Z...], got {value!r}')
        specs.extend((name, int(z)) for z in zooms.split(','))
    return tuple(specs)


def _parse_targets(values):
    targets = []
    for value in values:
        if '@' in value:
            sid, _, point = value.partition('@')
            lat, lon = (float(v) for v in point.split(','))
            targets.append({'scene': sid, 'lonlat': (lon, lat)})
        else:
            sid, _, oid = value.partition(':')
            targets.append({'scene': sid, 'object': oid or None} if oid else {'scene': sid, 'lonlat': (CAMBRIDGE['lon'], CAMBRIDGE['lat'])})
    return targets


DEFAULT_TARGETS = ('mooroolbark-50011498-17-118442-80417@-37.79033,145.31259',
                   'mooroolbark-50011498-17-118440-80408:7fdd138f819cb06c')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Higher-resolution imagery sources: inventory, effective-resolution probe, '
                                                 'registered hi-res scene rasters, contact sheet, paired structure measures.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--list', action='store_true', help='Print the provider inventory.')
    mode.add_argument('--probe', action='store_true', help='Measure effective resolution around a point.')
    mode.add_argument('--fetch', action='store_true', help='Write a registered hi-res raster for --scene.')
    mode.add_argument('--contact-sheet', action='store_true', help='Saved vs z20 vs hi-res crops for --targets.')
    mode.add_argument('--paired', action='store_true', help='Structure measures on saved vs hi-res crops for --sample.')
    parser.add_argument('--root', default='.')
    parser.add_argument('--json', action='store_true', help='--list as JSON.')
    parser.add_argument('--offline', action='store_true', help='Cache only; no network requests.')
    parser.add_argument('--scene')
    parser.add_argument('--provider', default='google')
    parser.add_argument('--zoom', type=int)
    parser.add_argument('--zooms', type=int, nargs='+', default=[20, 21])
    parser.add_argument('--k', type=int)
    parser.add_argument('--release', help='Esri Wayback release id (esri-wayback only).')
    parser.add_argument('--box', type=int, nargs=4, metavar=('X0', 'Y0', 'X1', 'Y1'), help='Saved-pixel box (default whole frame).')
    parser.add_argument('--lat', type=float, default=CAMBRIDGE['lat'])
    parser.add_argument('--lon', type=float, default=CAMBRIDGE['lon'])
    parser.add_argument('--size-m', type=float, default=200.)
    parser.add_argument('--specs', nargs='+', help='Probe specs PROVIDER:Z[,Z...] (default google:19,20,21,22 esri:19,20).')
    parser.add_argument('--targets', nargs='+', help='SCENE:OBJECT or SCENE@LAT,LON (default: 27 Cambridge and 7fdd138f).')
    parser.add_argument('--sample', default='data/reviews/eyes/structure-kinds/fresh-sample-48-seed13.json')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)
    root = Path(args.root)
    log = None if args.quiet else (lambda text: print(text, file=sys.stderr, flush=True))
    try:
        if args.list:
            rows = inventory_rows(root)
            if args.json:
                print(json.dumps(rows, indent=1))
            else:
                for r in rows:
                    key = r['key_needed'] or 'no'
                    print(f"{r['provider']:<20} {r['status']:<17} zoom {str(r['served_max_zoom_here'] or '-'):<4} "
                          f"key {key:<24} {r['native_gsd'][:70]}")
                    print(f"{'':<20} cost: {r['cost'][:110]}")
            return 0
        if args.probe:
            specs = _parse_specs(args.specs) if args.specs else DEFAULT_PROBE
            report = probe(root, lat=args.lat, lon=args.lon, size_m=args.size_m, specs=specs, offline=args.offline, log=log)
            print(json.dumps({'rows': [{k: v for k, v in r.items() if k != 'cache'} for r in report['rows']],
                              'sheet': report.get('sheet')}, indent=1, default=_plain))
            return 0
        if args.fetch:
            if not args.scene:
                parser.error('--fetch needs --scene')
            report, _ = fetch_scene(args.scene, args.provider, zoom=args.zoom, k=args.k, root=root, offline=args.offline,
                                    release=args.release, box=args.box)
            print(json.dumps({k: v for k, v in report.items() if k != 'tiles'}, indent=1, default=_plain))
            return 0
        if args.contact_sheet:
            zooms = args.zooms
            arms = (('saved', None), *((args.provider, z) for z in zooms), ('esri', 20))
            out, facts = contact_sheet(root, _parse_targets(args.targets or DEFAULT_TARGETS), arms=arms, offline=args.offline)
            print(json.dumps({'sheet': str(out), 'rows': facts}, indent=1, default=_plain))
            return 0
        if args.paired:
            summary, _ = paired(root, args.sample, provider=args.provider, zooms=tuple(args.zooms), offline=args.offline,
                                log=log, limit=args.limit)
            print(json.dumps(summary, indent=1, default=_plain))
            return 0
    except AccessRequired as error:
        print(f'access required: {error}', file=sys.stderr)
        return 3
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
