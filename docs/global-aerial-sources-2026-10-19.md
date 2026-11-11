# Global aerial and high-resolution imagery sources, 19 October 2026

Where to pull current imagery beyond Vicmap, Google and MetroMap. The short answer: there is no single global
5-10 cm aerial mosaic. Flown aerial is regional and commercial or national; global sub-meter is satellite tasking;
free global coverage is Sentinel-2 at 10 m. Choose by region and licence, not by one provider.

The machine-readable inventory for the pipeline is `scripts/ml/imagery_sources.py`. This note adds the providers that
matter for a global archive and the refresh/caching caveats.

The built archive, its COG/tile formats and its R*Tree indexes are documented in
[`docs/imagery-archive.md`](imagery-archive.md).

## 1. Flown aerial subscriptions (freshest, 4-30 cm)

| Source | Coverage | GSD | Refresh | Access | Licence |
|---|---|---|---|---|---|
| Nearmap | AU, NZ, US, CA (populated areas) | 4-7.5 cm | Core up to 3x/year (AU 5+/year in places); Reach annual 4 cm US | Tile API, key | Quote; storage limits |
| MetroMap (Aerometrex) | AU metro | 5-10 cm | 4-6x/year | WMTS/WMS, key | AUD 2,640/yr Pro; API on Enterprise |
| Vexcel Data Program | US, CA, EU, AU, JP | 7.5-15 cm | Annual/seasonal; 2026 US plan 95% population, 7.5 cm cities | REST API, WMTS, ArcGIS | Quote |
| EagleView (Pictometry) | US 94% population, CA 65%+ | 7.5-15 cm | Annual/biennial | Imagery API | Quote |
| HxGN Content Program | US, Europe | 15-30 cm | Annual | WMS/WMTS/REST | Quote |
| Vicmap Basemaps | Victoria, AU | 0.118 m at z20 | Quarterly mosaic refresh; capture often 2+ years old | WMTS, licence | Annual fee (confirmed) |
| CIP / IWS | Victoria, AU | 7.5-10 cm | Project-based | IWS, invitation/licence | Government/CIP partner |

Nearmap is the practical step up for Australian freshness: 5.5-7.5 cm, several captures a year, versus Vicmap's
slower government mosaic and Google's irregular refresh. MetroMap is the alternative if an Aerometrex key exists.

## 2. Satellite tasking and archive (global, 30-100 cm)

| Source | Coverage | GSD | Recency | Access | Cost |
|---|---|---|---|---|---|
| Planet SkySat | Global | 50 cm | Tasking near-daily; archive | Planet API | Quote |
| PlanetScope | Global | 3 m | Daily | Planet API | Subscription |
| Maxar / Vantor WorldView Legion/3 | Global | 30 cm | Tasking/archive | OneAtlas/API | ~USD 17.50-33/km² archive for 30 cm |
| Airbus Pléiades Neo | Global | 30 cm | Twice-daily revisit; tasking | OneAtlas/API | ~USD 22.50-55/km² |
| BlackSky | Global | 1 m | High revisit | API | Quote |
| SkyFi | Global marketplace | 30 cm-3 m | Tasking or archive | Web/API | Per order, published pricing |
| UP42 | Global marketplace | Aggregated | Tasking or archive | API/console | Per order |
| ICEYE / Capella / Umbra | Global | SAR (not optical) | All-weather | API | Quote |

Satellite tasking is the only way to get a *new* global capture on demand, but it is 30-50 cm, weather-dependent for
optical, and priced per km² or per scene. It does not replace 5-10 cm aerial for roof detail.

## 3. Free global coverage

| Source | Coverage | GSD | Refresh | Access |
|---|---|---|---|---|
| Sentinel-2 | Global | 10 m | ~5-day revisit | Copernicus Data Space / Sentinel Hub, free |
| Landsat 8/9 | Global | 30 m | 16-day | USGS EarthExplorer, free |
| NASA GIBS | Global | 250 m-1 km | Daily | WMTS, free |
| Esri World Imagery | Global | 0.3-0.5 m | Irregular (Vantor/Maxar) | ArcGIS tiles, free endpoint with terms |
| Google | Global | 0.1-0.3 m in cities | Irregular | Map Tiles API, key; public mt1 outside licence |
| Mapbox Satellite | Global | 0.3-0.5 m | Irregular | Raster Tiles API, token; free tier |
| Azure Maps | Global | ~0.3 m, z19 max | Irregular | Render API, key; free tier |
| OpenAerialMap | Global crowdsourced | Variable (10-30 cm where present) | Opportunistic | OAM API, CC BY |
| Maxar Open Data | Disaster events | 0.3-0.5 m | Event-driven | CC BY-NC |
| Planet Open Data | Event/area | 3-50 m | Event-driven | Various |

Sentinel-2 is the only free source with global, frequent, predictable coverage. At 10 m it is context, not roof
evidence. Esri, Google, Mapbox and Azure are global basemaps at 0.3-0.5 m; their refresh is irregular and their terms
restrict bulk storage.

## 4. National and regional open orthos (free, sub-meter, not global)

| Source | Coverage | GSD | Refresh | Licence |
|---|---|---|---|---|
| NAIP (USDA/USGS) | US | 0.6 m (some state 7.5-30 cm) | 2-3 years; 2025 complete, 2026 dates publishing | Public domain |
| IGN BD ORTHO | France | 20 cm | 3-4 years | Open Licence 2.0 |
| PNOA | Spain | 25-50 cm | 2-3 years | Open |
| PDOK | Netherlands | 8-25 cm | Annual | Open |
| swisstopo | Switzerland | 10 cm | ~3 years | Open |
| LINZ | New Zealand | 5 cm urban to 0.5 m rural | 3-5 years | CC BY 4.0 |
| NSW Spatial Services | NSW, AU | 5-10 cm urban | Program-based | CC BY 4.0 |
| Landgate | WA, AU | 10-15 cm towns, 50 cm broad | Program-based | Open Data policy |
| Other AU states | QLD, SA, TAS, ACT, NT | 10-50 cm | Program-based | Varies |
| Provincial Canada | ON, BC, etc. | 10-30 cm | Program-based | Varies |
| German states | DE | ~20 cm | 2-3 years | Open (varies) |
| UK APGB | UK | 25 cm | Annual/biennial | Licence |
| GSI | Japan | 10 cm class | Program-based | Attribution terms |

These are the best free wins for a global archive: NAIP in the US, IGN in France, PNOA in Spain, PDOK in the
Netherlands, swisstopo in Switzerland, LINZ in New Zealand, and the Australian state programs. They are usually
available as WMS/WMTS, ArcGIS ImageServer, STAC/COG, or bulk GeoTIFF, so a tile scraper is often the wrong tool.

## 5. What this means for addr

- Victoria: keep the licensed Vicmap archive and the dated Google z20 layer. Add Nearmap, or MetroMap if an
  Aerometrex key exists: they are the routine 5-10 cm, multi-capture-per-year options here.
- Global: route by country. Use national open orthos where they exist, Sentinel-2 for free context, and a satellite
  marketplace (SkyFi/UP42) for one-off fresh captures. Do not attempt one global crawl.
- Record per-tile provenance: provider, capture date (or `unknown`), licence, and whether storage is permitted.
  Google and Vicmap do not publish per-tile capture dates; do not infer them from tile Last-Modified.
- Cache/storage terms differ: open national data usually allows storage with attribution; commercial aerial and
  basemap terms often cap caching. A network-drive archive is a storage use, not just a browser cache.

## 6. Access notes

- Nearmap: `https://api.nearmap.com/tiles/v3/Vert/{z}/{x}/{y}.jpg?apikey=...`
- MetroMap: WMTS via `api.metromap.com.au/ogc/key/{key}/...`
- Vexcel: REST API/WMTS, token from the Vexcel platform
- EagleView: Imagery API, developer portal
- HxGN: WMS/WMTS/REST streaming subscription
- Planet: Planet API/STAC, `PL_API_KEY`
- Maxar/Vantor: OneAtlas/API
- Airbus: OneAtlas/API
- SkyFi: web/API marketplace
- UP42: API/console marketplace
- Sentinel-2: Copernicus Data Space / Sentinel Hub
- NAIP: `imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer`
- IGN: Geoplateforme WMS/WFS
- PNOA: IGN Spain WMS
- PDOK: WMS/WMTS
- swisstopo: WMTS
- LINZ: public `nz-imagery` S3 COGs (Basemaps tiles need a free API key)
- NSW: Spatial Services imagery services

Prices are indicative and quote-based; confirm with the provider before ordering. The repo's
`scripts/ml/imagery_sources.py` carries the measured local numbers for the providers already tested.

The built archive is documented in [`docs/imagery-archive.md`](imagery-archive.md): 27
open COGs (13 high-resolution AOIs plus 14 Sentinel-2 context COGs), a Vicmap tile store,
and a Google review tile store, each with an R*Tree bbox index. The dated build log is
`docs/open-imagery-archive-2026-10-19.md`.
