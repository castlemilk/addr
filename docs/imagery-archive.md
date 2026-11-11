# Imagery archive: setup and index

Canonical reference for the three imagery stores on `gamma-systems-2`: the licensed
Vicmap tiles, the Google review tiles, and the open-licence training COGs. Dated working
notes remain in `docs/open-imagery-archive-2026-10-19.md` and
`docs/global-aerial-sources-2026-10-19.md`; this page is the stable contract.

## Inventory, 20 October 2026

| Store | Contents | Tiles / assets | Size | Spatial index |
|---|---|---:|---:|---|
| Vicmap | `AERIAL_WM_256`, z18-20 PNG | 35,960 tiles | 4.16 GB | `tile_index` R*Tree |
| Google | `mt1 lyrs=s`, z18-20 JPEG | 35,960 tiles | 575 MB | `tile_index` R*Tree |
| Open COGs | 13 high-res AOIs + 14 Sentinel-2 context | 27 COGs | 4.49 GB | `asset_index` R*Tree |
| 3D heights | AHN DSM/DTM/nDSM (Utrecht, Rotterdam) + swissALTI3D DSM (Zurich, Interlaken) | 8 COGs | 649 MB | separate `asset_index` R*Tree |
| Roof labels | swissBUILDINGS3D mask/height/z/pitch/usage/instance id (Zurich, Interlaken) | 12 COGs | 8.6 MB | with the label planes |
| Roof training set | dataset-v1 (Zurich): 18 windows, 758 measured annotations | 20 MB | (consumed by train.py) |
| **Total** | | | **9.9 GB** | |

Storage root: `/Volumes/gamma-systems-2`. Live machine-readable state:
`/Volumes/gamma-systems-2/addr-tiles/STATUS.json`. Human layout:
`/Volumes/gamma-systems-2/addr-tiles/README.txt`.

## Directory layout

```
/Volumes/gamma-systems-2/
  addr-tiles/
    vicmap/
      AERIAL_WM_256/<z>/<x>/<y>.png
      tiles.db            # tiles + tile_index R*Tree + archive_meta
      latest.json         # crawl summary, Last-Modified distribution
    google/
      satellite/<z>/<x>/<y>.jpg
      tiles.db            # tiles + tile_index R*Tree + archive_meta
      latest.json         # complete crawl summary
  addr-imagery/
    open/
      <source>/<aoi>.tif  # COG per source and AOI
      catalog.db          # assets + asset_index R*Tree
    elevation/
      utrecht/{dsm,dtm,ndsm}_05m.tif
      rotterdam/{dsm,dtm,ndsm}_05m.tif
      zurich/dsm_05m.tif          # swissALTI3D 0.5 m DSM
      interlaken/dsm_05m.tif      # swissALTI3D 0.5 m DSM
      catalog.db          # DSM/DTM/nDSM assets + asset_index R*Tree
    labels/
      zurich/{roofmask,roofheight,roofz,roofpitch,roofclass,roofsid}.tif
      interlaken/{roofmask,roofheight,roofz,roofpitch,roofclass,roofsid}.tif
    rooflabels-zurich/    dataset-v1: images/, labels/, train/val/test.json, DATASET_CARD.md
```

## Compression

- **Vicmap**: source-native PNG, lossless, ~115 KB per 256 px tile. Not transcoded; the
  provider bytes and ETag/sha256 are kept.
- **Google**: source-native JPEG, ~16 KB per tile. Not re-encoded.
- **Open COGs**: Cloud-Optimised GeoTIFF, 512x512 internal tiles, **JPEG q90**, overviews
  built by the COG driver (2x to 128x depending on size). Verified with
  `IMAGE_STRUCTURE.LAYOUT == COG`. EPSG:3857; the pixel size is
  `ground_gsd / cos(latitude)` (`assets.pixel_m`), so high-latitude sources are not
  oversampled.

Nothing in the archive is stored uncompressed, and the tile stores are never retiled or
recompressed: the index points at the provider bytes.

## Index coverage

| Index | Rows | Joins to | Bounds |
|---|---:|---|---|
| Vicmap `tile_index` | 35,960 | `tiles.rowid` | WGS84 per 256 px tile |
| Google `tile_index` | 35,960 | `tiles.rowid` | WGS84 per 256 px tile |
| COG `asset_index` | 27 | `assets.id` | WGS84 per source/AOI |
| 3D height `asset_index` | 8 | `assets.id` | WGS84 per DSM/DTM/nDSM |
| Roof label planes | 12 | (no index: same grid as the DSM) | WGS84 |

Every row carries the exact bytes and sha256; the COG rows also carry ground GSD, Mercator
pixel size, capture, licence and attribution. The index is an R*Tree over WGS84 bounds, so
a bbox query is an index range scan, not a directory walk.

## Index A: tile archives (`tiles.db`)

Used by Vicmap and Google. `scripts/ml/tile_index.py` adds the spatial index and metadata.

```sql
CREATE TABLE tiles (
  z, x, y, path, bytes, sha256, last_modified, etag, fetched_at,
  PRIMARY KEY (z, x, y)
);
CREATE VIRTUAL TABLE tile_index USING rtree(id, minx, maxx, miny, maxy);
-- id = tiles.rowid; bounds are WGS84 degrees
CREATE TABLE archive_meta (
  provider, layer, licence, source, tiles, bytes, bbox_wsen, zooms, built_at
);
```

BBox lookup:

```sql
SELECT t.z, t.x, t.y, t.path
FROM tile_index i JOIN tiles t ON t.rowid = i.id
WHERE i.maxx >= :west AND i.minx <= :east
  AND i.maxy >= :south AND i.miny <= :north
ORDER BY t.z, t.x, t.y;
```

Then read the returned PNG/JPEG paths directly, or stitch the z/x/y tiles if a single
raster is needed. The tiles table already has the exact `bytes` and `sha256` for
verification.

## Index B: COG catalog (`catalog.db`)

Used by the open training archive. `scripts/ml/open_imagery.py` writes it.

```sql
CREATE TABLE assets (
  id, source_id, title, licence, attribution, capture,
  gsd_m,            -- ground metres per pixel
  pixel_m,          -- EPSG:3857 metres per pixel (gsd_m / cos(lat))
  crs, bounds_wsen, path, bytes, sha256, width, height, compression, created_at
);
CREATE VIRTUAL TABLE asset_index USING rtree(id, minx, maxx, miny, maxy);
```

BBox lookup:

```sql
SELECT a.path, a.source_id, a.capture, a.gsd_m, a.pixel_m, a.licence, a.sha256
FROM assets a JOIN asset_index i ON a.id = i.id
WHERE i.maxx >= :west AND i.minx <= :east
  AND i.maxy >= :south AND i.miny <= :north;
```

Read only the needed window:

```python
import rasterio
from rasterio.windows import from_bounds

with rasterio.open(row["path"]) as ds:
    window = from_bounds(left, bottom, right, top, ds.transform)
    chip = ds.read([1, 2, 3], window=window, out_shape=(3, 512, 512))
```

Use `pixel_m` for the COG transform; use `gsd_m` for the ground size of the chip. The
COG's overviews make a coarse read cheap.

## Sources and licences

### Licensed / review stores

| Store | Capture | Licence and use |
|---|---|---|
| Vicmap `AERIAL_WM_256` | Last-Modified z18 2025-12-13, z19 2026-01-10, z20 2026-02-14; capture not published | Confirmed Vicmap Basemaps licence. Licensed imagery, not an open dataset. |
| Google `mt1` | z18-20 2025-12-12; z21 2023-10-18 | Internal review archive only. Public tile endpoint is outside the Map Tiles API licence; not in the training corpus. |

Vicmap Last-Modified is a publish time, not a flight date. Do not treat it as a capture
date.

### Open training COGs (13 high-resolution)

| Source | AOIs | Licence | GSD |
|---|---|---|---|
| US NAIP | Arvada CO; Phoenix AZ | Public domain | 0.6 m |
| France IGN BD ORTHO | Lyon; Paris | Open Licence 2.0 | 0.2 m |
| Spain PNOA | Madrid; Valencia | Open | 0.25 m |
| Netherlands PDOK 2026 orthoHR | Utrecht; Rotterdam | Open | 0.08 m |
| Switzerland swisstopo | Zurich; Interlaken | Open | 0.1 m |
| NSW Spatial Services | Sydney; Maitland | CC BY 4.0 | 0.1 m |
| LINZ Christchurch 2025 | Christchurch | CC BY 4.0 | 0.075 m |

Newcastle, NSW was the intended second NSW site; the ArcGIS export returned a persistent
`HTTP 500: Error: bytes` on one tile, so Maitland (inland Hunter Valley, 1.5 km) is the
fallback.

### 3D heights (8 COGs)

- **AHN** (Netherlands, CC BY 4.0): 0.5 m DSM and DTM plus derived nDSM for Utrecht and
  Rotterdam, EPSG:28992.
- **swissALTI3D** (swisstopo, open data): 0.5 m DSM for Zurich and Interlaken, EPSG:2056,
  pulled from the public STAC API. Absolute surface, no DTM yet.

Single-band float32 COGs, DEFLATE predictor=2, overviews 2-32. See
`docs/elevation-spike.md` for the schema, query and previews. Surfaces include canopies, so
the hillshade is the readable preview and the 3D surface a diagnostic.

### Roof label planes (12 COGs) and the training export

swissBUILDINGS3D 3.0 (open data) rasterised onto the Zurich and Interlaken DSM grids:
roof mask, measured height above ground, absolute roof elevation, pitch estimate, building
usage class and a per-building instance id. This is the first plane-level label source in
the archive. See `docs/elevation-spike.md` for the schema, the Swiss data quirks and the
CityGML versus DSM height cross-check.

`scripts/ml/rooflabels_export.py` (`--labels ... --ortho-db ... --out ...`) turns them into
a dataset-v1 training set: 18 windows, 758 measured annotations, split 127/29/24. The labels
are publisher survey geometry, not a segmentation model's output, so agreement with a model
is evidence about the model rather than a self-consistent loop.

### Sentinel-2 context (14 COGs)

10 m L2A true colour, Copernicus terms, one scene per AOI plus Mooroolbark. Scenes are
September-October 2026 and under 15% cloud. This is context, not roof detail.

Google, Mapbox, Azure Maps and Esri World Imagery are deliberately excluded from the
training corpus: free-to-view is not a training-data licence.

## Commands

```bash
# inventory
task imagery:open -- list

# fetch one open source at its default AOI
task imagery:open -- fetch --source naip \
  --out /Volumes/gamma-systems-2/addr-imagery/open

# second AOI set (rerun-safe)
task imagery:open-aois

# index a tile archive
task imagery:index-tiles -- \
  --db /Volumes/gamma-systems-2/addr-tiles/vicmap/tiles.db \
  --provider vicmap --layer AERIAL_WM_256 \
  --licence "Vicmap Basemaps licence"

# sample training chips from the COG catalog
task imagery:chips -- \
  --bbox 5.20,52.07,5.25,52.115 \
  --db /Volumes/gamma-systems-2/addr-imagery/open/catalog.db \
  --out /Volumes/gamma-systems-2/addr-imagery/chips \
  --size-m 512 --stride-m 512 --res-m 0.5
```

## Operational notes

- Long pulls run in detached `tmux` (`scripts/run_google_until_done.sh`,
  `scripts/run_nsw_remaining.sh`). Harness-owned background shells are terminated by
  OpenCode runtime updates; tmux survives them.
- The Google supervisor restarts the crawler on a non-zero exit and builds the R*Tree
  only after `latest.json` reports `complete`.
- Do not run `imagery:index-tiles` against a tile DB while its crawler is writing. SQLite
  `database is locked` was observed when a stale watcher indexed the Google DB mid-crawl.
  The crawler now uses `busy_timeout=30000`; indexing still waits for completion.
- Local DB backups: `data/imagery/backups/*-final.db`.
- `STATUS.json` is the quick machine-readable check; `PRAGMA integrity_check` and sampled
  sha256 checks are the correctness checks.
- 512x512 COG window reads over SMB measured ~102 ms cold and sub-millisecond cached.

## Post-restart verification, 22 October 2026

After the host restart the drive remounted with all row counts and R*Tree rows intact:
Vicmap 35,960/35,960, Google 35,960/35,960, open 27/27. Sampled sha256 checks passed
5/5 Vicmap, 5/5 Google and 3/3 open assets. No crawler or tmux session is running.
