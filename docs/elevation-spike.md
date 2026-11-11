# 3D elevation spike: AHN DSM/DTM/nDSM

First open 3D-height pull for training. The USGS 3DEP dynamic service is **bare-earth
DEM only**, so it cannot give roof heights. The Dutch AHN WCS publishes both a 0.5 m DSM
and a 0.5 m DTM, so it is the cleaner open pair for the existing PDOK AOIs.

## What was pulled

| AOI | Product | Size | Grid | Source |
|---|---|---:|---|---|
| Utrecht | DSM 0.5 m | 72.3 MB | 3442 x 5556 | AHN WCS `dsm_05m` |
| Utrecht | DTM 0.5 m | 58.0 MB | 3442 x 5556 | AHN WCS `dtm_05m` |
| Utrecht | nDSM (DSM-DTM) | 37.2 MB | 3442 x 5556 | derived |
| Rotterdam | DSM 0.5 m | 57.0 MB | 3537 x 5503 | AHN WCS `dsm_05m` |
| Rotterdam | DTM 0.5 m | 49.8 MB | 3537 x 5503 | AHN WCS `dtm_05m` |
| Rotterdam | nDSM (DSM-DTM) | 26.7 MB | 3537 x 5503 | derived |
| Zurich | swissALTI3D DSM 0.5 m | 211.8 MB | 7405 x 10112 | 60 STAC tiles |
| Interlaken | swissALTI3D DSM 0.5 m | 135.8 MB | 7605 x 8933 | 56 STAC tiles |

- Licence: **CC BY 4.0** (AHN / PDOK); attribution in every catalog row.
- Swiss coverage: **swissALTI3D** (swisstopo) 0.5 m DSM, open data, EPSG:2056, pulled from the
  public STAC API (`data.geo.admin.ch/api/stac/v0.9`, collection `ch.swisstopo.swissalti3d`);
  2019 vintage for these tiles. No DTM, so it is an absolute surface, not an nDSM.
- Native CRS: EPSG:28992 (AHN) and EPSG:2056 (swissALTI3D). COGs are single-band float32,
  DEFLATE predictor=2, 512x512 tiles, overviews 2-32.
- nDSM is masked to finite positive values; building/tree heights are present, so the
  surface includes canopies as well as roofs. A hillshade is the readable preview; the
  3D surface is a diagnostic.

## Layout

```
/Volumes/gamma-systems-2/addr-imagery/elevation/
  catalog.db                 # assets + asset_index R*Tree
  utrecht/{dsm,dtm,ndsm}_05m.tif
  utrecht/ndsm-hillshade.png
  utrecht/ndsm-3d.png
  rotterdam/...
  zurich/dsm_05m.tif, zurich/dsm-hillshade.png, zurich/dsm-3d-patch.png
  interlaken/dsm_05m.tif, interlaken/dsm-hillshade.png, interlaken/dsm-3d-patch.png
```

`catalog.db`:

```sql
CREATE TABLE assets (
  id, source_id, product, aoi, capture, licence, attribution, gsd_m,
  crs, bounds_wsen, path, bytes, sha256, width, height, dtype, nodata,
  compression, created_at
);
CREATE VIRTUAL TABLE asset_index USING rtree(id, minx, maxx, miny, maxy);
```

BBox query:

```sql
SELECT product, path, gsd_m, crs, bounds_wsen
FROM assets a JOIN asset_index i ON a.id = i.id
WHERE i.maxx >= :west AND i.minx <= :east
  AND i.maxy >= :south AND i.miny <= :north;
```

## Commands

```bash
task imagery:elevation-spike -- --out /Volumes/gamma-systems-2/addr-imagery/elevation --aoi utrecht
task imagery:elevation-spike -- --out /Volumes/gamma-systems-2/addr-imagery/elevation --aoi rotterdam
task imagery:elevation-spike -- --out /Volumes/gamma-systems-2/addr-imagery/elevation --aoi all
```

The WCS rejects a single request much larger than 1 km, so the fetcher tiles each AOI
into 1 km DSM/DTM chunks and mosaics them on the 0.5 m grid.

## Roof label planes (swissBUILDINGS3D)

`scripts/ml/citygml_pull.py` downloads the public open-data CityGML tiles that intersect an
AOI and rasterises label planes onto the DSM grid, so RGB, height and roof labels share one
pixel grid:

| Plane | Meaning |
|---|---|
| `roofmask.tif` | uint8 roof surface pixels |
| `roofheight.tif` | float32 metres above ground (measured height) |
| `roofz.tif` | float32 absolute roof elevation |
| `roofpitch.tif` | float32 pitch estimate in degrees |
| `roofclass.tif` | uint8 building usage class (see CLASSES) |
| `roofsid.tif` | uint32 per-building instance id |

Findings that changed the plan:

- **swissSURFACE3D is a DSM, not a DTM.** Its own metadata says "digital surface model
  ... including ... constructive works". Differencing it against swissALTI3D gives a near-zero
  nDSM (half the pixels identical within 5 cm), so Switzerland has **no open bare-earth
  model** and no measured nDSM. An opening-derived ground was tried and rejected: on
  sloped ground it biases heights by 0.2-1 m. Switzerland therefore ships the absolute DSM only.
- **The dataset has no roof-form attribute.** `OBJEKTART` is building *usage*
  ("Gebaeude Einzelhaus", "Hochhaus", "Offenes Gebaeude", "Im Bau"), so the class plane is
  usage, and pitch must be derived from geometry.
- **Heights live on the building, not the roof.** `bldg:measuredHeight` (10,856), with
  `DACH_MAX`/`DACH_MIN`/`GELAENDEPUNKT` as fallbacks.
- Cross-check: CityGML measured height (median 15.31 m) and swissALTI3D DSM minus roof
  (14.90 m) agree within 0.4 m over Zurich, which is a real validation of both sources.

## Chips with their 3D targets

`scripts/ml/open_chips.py --height-db ... --labels-dir ...` writes one compressed `.npz`
beside each RGB chip with the channels found (height, dsm, roofmask, roofheight, roofz,
roofpitch, roofclass, roofsid), and records the channel list in the manifest row.

## Next open 3D sources

- **swissALTI3D DTM** — swisstopo publishes a terrain model too; pairing it with the DSM gives
  an nDSM for Zurich/Interlaken, matching the AHN treatment.
- **LINZ 1 m DSM/DEM** — pairs with Christchurch/Wellington imagery.
- **ACT LiDAR** — 1 m, pairs with ACT imagery.
- **3DBAG** — LOD2 roof planes/normals; rasterize onto the PDOK grid for plane labels.
- **USGS 3DEP** — public-domain bare-earth DEM; useful for terrain/slope, not roof height.
