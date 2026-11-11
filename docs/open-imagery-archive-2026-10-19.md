# Open imagery training archive, 19 October 2026

Storage and index standard for the free, training-usable orthophotos. The goal is not a
global mosaic; it is a set of native-resolution AOIs that can be read as training chips
without walking a directory tree.

**Canonical setup/index reference: [`docs/imagery-archive.md`](imagery-archive.md).** This
file is the dated work log for the first build.

## Format

- One **Cloud-Optimised GeoTIFF (COG)** per source and AOI.
- 512x512 internal tiles, JPEG quality 90 (or DEFLATE when alpha is meaningful), overviews
  built by the COG driver. Verified by `IMAGE_STRUCTURE.LAYOUT == COG`.
- EPSG:3857 for the current pilot. The pixel size is `ground_gsd / cos(latitude)`, so a
  0.08 m Dutch source is stored at ~0.13 Mercator metres, not oversampled by 1.6x.
- Random window reads are the training interface: `rasterio.open(path).read(window=...)`
  reads only the needed tiles and the matching overview.

## Catalog

`catalog.db` is SQLite with an R*Tree over WGS84 bounds:

    assets(id, source_id, title, licence, attribution, capture, gsd_m, pixel_m, crs,
           bounds_wsen, path, bytes, sha256, width, height, compression, created_at)
    asset_index USING rtree(id, minx, maxx, miny, maxy)

A bbox query returns the candidate COGs:

    SELECT a.path FROM assets a JOIN asset_index i ON a.id=i.id
    WHERE i.maxx>=? AND i.minx<=? AND i.maxy>=? AND i.miny<=?;

## Sources pulled

| Source | Licence | GSD | Output | Size |
|---|---|---|---|---|
| US NAIP | Public domain | 0.6 m | Arvada, CO | 36 MB |
| France IGN BD ORTHO | Open Licence 2.0 | 0.2 m | Villeurbanne, Lyon | 238 MB |
| Spain PNOA | Open | 0.25 m | Alcobendas, Madrid | 206 MB |
| Netherlands PDOK 2026 orthoHR | Open | 0.08 m | Zeist, Utrecht | 798 MB |
| Switzerland swisstopo | Open | 0.1 m | Oerlikon, Zurich | 804 MB |
| NSW Spatial Services | CC BY 4.0 | 0.1 m | Castle Hill, Sydney | 813 MB |
| LINZ Christchurch 2025 | CC BY 4.0 | 0.075 m | Christchurch, NZ | 51 MB |
| Sentinel-2 L2A | Copernicus terms | 10 m | 14 context COGs (Mooroolbark + all AOIs) | 1.2 MB total |

The first six sources, LINZ and the second AOI set are complete. Sentinel-2 supplies 14
context COGs, one per training AOI plus Mooroolbark, captured September-October 2026.
LINZ comes from the public `nz-imagery` S3 COGs, so no Basemaps API key is needed.
Sentinel-2 is context, not roof detail. The final 27-asset inventory is in
`docs/imagery-archive.md`.

Current totals: open COG archive 4.49 GB across 27 COGs and 14.40 billion pixels, R*Tree
indexed; licensed Vicmap archive 4.16 GB across 35,960 tiles, zero failures, R*Tree indexed;
Google archive 575 MB across 35,960 tiles, zero failures, R*Tree indexed. Google's current
layer is 2025-12-12 at z18-20; z21 is 2023-10-18.

## Deliberately excluded from the training set

Google, Mapbox, Azure Maps and Esri World Imagery are free-to-view basemaps, not
training-data licences. Their terms restrict bulk storage and derivative datasets, so
they stay in the review archive, not the training corpus. Vicmap is stored separately
under its confirmed licence (`/Volumes/gamma-systems-2/addr-tiles/vicmap`).

## Commands

    task imagery:open -- list
    task imagery:open -- fetch --source naip
    task imagery:open -- register --source pdok --path FILE   # interrupted run
    task imagery:open -- query --bbox 5.20,52.07,5.25,52.115

    task imagery:index-tiles -- --db /Volumes/gamma-systems-2/addr-tiles/vicmap/tiles.db \
        --provider vicmap --layer AERIAL_WM_256 --licence "Vicmap Basemaps licence"

The tile archives keep their source compression (Vicmap PNG, Google JPEG) and gain an
R*Tree over WGS84 tile bounds. The COG archive uses JPEG q90; both are indexed by bbox.

## Incident: DNS and SMB outage, 19 October 2026

At about 15:29 AEDT the Google crawl aborted after 10,449 tiles, and the swisstopo and
NSW runs stopped. The errors were not HTTP blocks: there was no 401/403/429, only
`NameResolutionError` for `mt1.google.com`, `wms.geo.admin.ch` and
`maps.six.nsw.gov.au`, and the SMB volume unmounted (`socket is not connected`).

Diagnosis: the local resolver had cached the router's failing DNS (192.168.88.1);
1.1.1.1 and 8.8.8.8 answered normally. `dscacheutil -flushcache` restored resolution and
the same tile URL returned HTTP 200. The drive was remounted; `PRAGMA integrity_check`
returned `ok` for all three databases.

Fix: remount the share, flush the resolver cache, delete the incomplete
`.tif`/`.ovr.tmp` COGs, increase connection-error retries to eight with up to 60 s
backoff, raise the Google consecutive-failure cap to 50, and resume from the manifests.
The SQLite tile databases keep every completed tile, so no downloaded tile was lost.

## Sampling training chips

The catalog is the lookup, not the image. For a training chip:

1. Query `catalog.db` with the chip's WGS84 bbox to get the candidate COGs.
2. Convert the chip bbox to EPSG:3857 and pick the pixel window with `pixel_m`
   (`ground_gsd / cos(lat)`), not `gsd_m`; the COG transform already uses `pixel_m`.
3. `rasterio.open(path).read(window=...)` reads only the needed 512x512 tiles and the
   matching overview when the requested resolution is coarser.
4. Record `source_id`, `capture`, `licence` and `sha256` with the chip. The COG is
   compressed, but the source licence still applies to any derived dataset.

A 512x512 window read from the PDOK COG over SMB measured about 102 ms cold and
sub-millisecond once the tiles were cached by the client.

## Second training AOI set

The first set is mostly leafy detached suburbs. `scripts/ml/open_aois.py` adds one
contrasting site per source, sequentially, with a 10 m Sentinel-2 context COG each:

| AOI | Source | Contrast |
|---|---|---|
| Phoenix, AZ | NAIP | desert suburban, tile roofs |
| Paris | IGN | dense European block |
| Valencia | PNOA | coastal Mediterranean |
| Interlaken | swisstopo | alpine valley |
| Newcastle / Maitland, NSW | NSW | coastal Australian; Maitland inland fallback after a persistent ArcGIS 500 |
| Rotterdam | PDOK | port/industrial |

A rerun is safe: an AOI whose source and bounds already exist in the catalog is skipped.
Phoenix (22 MB), Paris (124 MB), Valencia (133 MB), Interlaken (688 MB), Rotterdam
(484 MB) and Maitland (93 MB) are complete. Newcastle returned a persistent ArcGIS
`HTTP 500: Error: bytes` on one tile even at 1024 px exports, so the second NSW site is
Maitland (inland Hunter Valley) at 1.5 km. The earlier swisstopo 502/503 outage recovered
without losing a completed tile.

Long pulls run in a detached `tmux` session (`scripts/run_google_until_done.sh`,
`scripts/run_nsw_remaining.sh`) because OpenCode runtime updates terminate harness-owned
background shells. The Google script restarts the crawler on a non-zero exit and breaks
only when `latest.json` says `complete`; it then builds the R*Tree.

## Chip sampler

`scripts/ml/open_chips.py` turns a WGS84 bbox into JPEG training chips without loading a
whole COG:

    task imagery:chips -- --bbox 5.20,52.07,5.25,52.115 \
      --db /Volumes/gamma-systems-2/addr-imagery/open/catalog.db \
      --out /Volumes/gamma-systems-2/addr-imagery/chips \
      --size-m 512 --stride-m 512 --res-m 0.5

The requested resolution is clamped up to the asset's ground GSD, so a Sentinel-2 chip
is 10 m rather than an invented 0.5 m. Each chip gets a JSONL manifest row with source,
capture, licence, attribution, ground/pixel GSD, WGS84 bounds, asset path and sha256.
A 256 m test over PDOK produced 512x512 RGB JPEGs of 89-103 KB with no full-file decode.

During the Google crawl a resume hit `sqlite3.OperationalError: database is locked` at
`archive.conn.commit()`. Cause: an earlier harness watcher started `tile_index` on the
same `tiles.db` while the crawler was writing. The DB passed `PRAGMA integrity_check`;
the competing index process was removed and indexing was deferred to the completion
supervisor. The crawler now uses `busy_timeout=30000`. The final Google archive is
complete at 35,960 tiles and the R*Tree was built after completion.
