# Teacher sources for the roof pipeline: open fine-pixel imagery, labels and heights (28 September 2026)

What exists, at what licence, for teaching the outline and structure models from pixels finer than the 0.236 m
Google tiles the pipeline sees. Forty-seven sources were checked on their official pages by a research agent on
28 September 2026; the machine-readable list is `data/references/teacher-sources/catalogue-2026-09-28.json`.
Licences and pixel sizes need confirming again before any commercial use. "ML ok" means the licence permits
training and derived datasets (CC BY, CC0, public-domain or explicit terms); share-alike is noted.

## What to use first (Australian and New Zealand housing, ML ok)

| source | pixel | licence | why it matters here | registry |
|---|---|---|---|---|
| City of Melbourne 2020 true ortho | 0.019 m (file header; portal says 0.10) | CC BY 4.0 | five times finer than the 2018 teacher over the same municipality; one 49 GB JPEG2000 with HTTP range reads | `com-2020-2cm` (remote JP2, teacher pixel 0.05 m) |
| City of Melbourne 2018 point cloud and 2018/2020 photomesh | RGB LAS 4.3 GB; 3D mesh | CC BY | the only open sub-metre heights in metro Melbourne, co-registered with the 2018 teacher windows: roof pitch and height teacher | planned (`heights_source`) |
| City of Melbourne tree canopies 2021 and tree points | polygons; 82k points | CC BY | tree-over-roof occlusion masks and species/DBH points over the 2019/2020 imagery | planned |
| Vicmap Vegetation Tree Urban Point | points with height and canopy radius | CC BY 4.0 | 16,848 machine-derived tree points inside the Mooroolbark bbox with LiDAR heights: weak tree labels over the pipeline suburb itself | next enrichment input |
| ACT imagery (ACTmapi ImageServers) | 0.05 to 0.075 m, 2019 to 2023; tiles to 2026 | CC BY 4.0 (ACT and Aerometrex) | Canberra suburbs, many capture dates of the same houses; export tested (2000 px TIFF); pairs with ACT LiDAR 2015/2020 CC BY 4.0 via ELVIS | `act-2023-urban-75mm` (ImageServer export) |
| NSW Imagery web service | 0.10 m towns, 0.075 m cache at z21 | CC BY 3.0 AU | Sydney and NSW towns; export works; mosaic mixes dates | planned |
| Queensland aerial_v2 COGs; latest state basemap | 0.10 to 0.15 m | CC BY 4.0 | small rural set today; SISP imagery older than three years becomes open | planned |
| LINZ Christchurch 2020-21 / 2025 (0.075, 0.05), Auckland 2024 (0.075, 4.9 TB), Wellington 2025 (0.075) | 0.05 to 0.075 m | CC BY 4.0 | public S3 COGs, detached housing close to Melbourne's; Christchurch 2025 0.075 is running now | `linz-*` |
| LINZ nz-elevation 1 m DSM/DEM | 1 m | CC BY 4.0 | same survey years as the imagery: imagery plus normalised DSM for height teaching | planned |
| LINZ NZ Building Outlines | vector | CC BY 4.0 | national roof outlines, 95 percent within 1 m: weak labels and capture dates | planned |
| OpenAerialMap over Australia | 0.013 to 0.15 m, 110 images | CC BY 4.0 per image | tiny volume, mostly parks and industry; check each image's licence | planned |
| Microsoft Global ML Building Footprints | vector, some heights | CDLA-Permissive-2.0 (not ODbL; `heights_source.py` corrected) | weak footprints for Australia | in use |

## Labelled datasets worth a look (ML ok)

- RID2 (Netherlands, about 0.08 m, CC BY 4.0, 7 GB): roof segments with orientation and superstructures; a
  roof-plane topology teacher, steeper roofs than ours.
- 3DBAG LoD2.2 (CC BY 4.0) plus Dutch Beeldmateriaal 0.075 m orthos (CC BY 4.0): rasterise roof planes onto the
  imagery for a national roof-plane teacher.
- NRW DOP10 true ortho plus LoD2 CityGML (dl-de/zero-2.0); SWISSIMAGE 10 cm plus swissBUILDINGS3D (attribution
  only).
- OAM-TCD (restor/tcd, 0.10 m, CC BY 4.0 main repo, 4,608 tiles, about 280k tree crowns): tree detector teacher.
  Filter by the per-row licence; the NC and SA repos are separate.
- Duke distributed solar PV (CC0, 0.30 m): rooftop PV polygons, coarser than our target.
- Open Cities AI (drone 0.02 to 0.20 m, OAM CC BY imagery, OSM ODbL labels): drone-to-aerial domain bridge.

## Not usable for a product (research-only or unclear)

Semantic Drone Dataset, UAVid, UDD, VDD, ISPRS Potsdam/Vaihingen, iSAID/DOTA, RID v1 (Google imagery), Building3D,
Mapillary Vistas; unclear: AIRS (rebuild it from LINZ 2015-16 imagery plus NZ Building Outlines, both CC BY 4.0),
COWC, pool sets on Google/Mapbox imagery, Brisbane City Council tiles, Tasmania downloads. YouTube drone footage is
uploader copyright under terms that forbid downloading; it is not a training source.

## Dead ends checked

No Victorian council other than City of Melbourne publishes orthos; Vicmap Imagery and the Greater Melbourne LiDAR
are licensed through the Coordinated Imagery Program; ELVIS over Mooroolbark returns only 5 m and 10 m DEMs; WA
Landgate and SA Location SA imagery are not open; Google Open Buildings does not cover Australia; the DroneDeploy
benchmark links are dead; no open oblique aerial programme exists in Australia or New Zealand (the City of
Melbourne photomesh and Dutch stereo frames are the nearest substitutes).

## How a source becomes a teacher

`scripts/ml/training/teacher_sources.py` registers it (licence, CRS, pixel, reader), cuts pipeline-sized windows
(`catalog`, `select`, `fetch`, `crop`), then `hires_teacher --source ID calibrate|teach|finalize|measure` runs the
premise measurement: SAM3 on the fine pixels against SAM3 on the 0.236 m student, matched one-to-one, right-corner
share and simplicity. The policy is manual: read the premise summary before running `distill_dataset --source ID`;
the code does not gate on it. Sources finer than about 0.05 m should carry `teacher_gsd_m` (today only
`com-2020-2cm` does, at 0.05 m) because SAM3 sub-tiling cost grows with the square of the pixel ratio.
