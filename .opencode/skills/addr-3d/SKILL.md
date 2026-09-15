---
name: addr-3d
description: Use when working on the addr repo's 3D building pipeline — AI roof extraction (scripts/ml), per-address records, MBTiles tiles, MapLibre RealRoofLayer, Deep 3D Studio, heights/trees, or when debugging missing 3D geometry in the web UI.
---

# addr 3D roof & building pipeline

Repo: Go tile/geo service + Next.js 16 frontend + Python 3.12 ML pipeline.
Flow: aerial imagery → AI segmentation/depth → tile props (roof type, eave/ridge,
angle, area, kind) → per-address records → MapLibre roof meshes + Deep 3D Studio.

## Services & commands

```bash
task start            # ML sidecar :8090, Go :8085, web :3004 (PIDs in .run/)
task status           # includes health checks
task stop             # stops ALL services — there is no stop:web/stop:server task
task test             # go test + pytest + tsc
task lint             # go vet + eslint + ruff
task verify:ui        # headless Chrome smoke test, screenshots -> /tmp/addr-verify
task tiles -- --bbox 145.308,-37.797,145.320,-37.786 --zooms 16 --heights --trees --imagery-offset 3 --simplify-px 0.75 --min-area-m2 12 --no-osm --out data/australia_tiles.mbtiles
task addresses -- --bbox ...   # per-address roofs -> public/address-roofs.json
task sam3:review -- --bbox ... --zooms 16 --existing data/australia_tiles.mbtiles  # standalone SAM3 boundary review
```

Boundary-accuracy knobs: `--imagery-offset 3` (z19 source, tightest outlines,
~2-3x runtime), `--simplify-px 0.75`, `--min-area-m2 12`. SAM3 (`--sam3`) is
opt-in: ~40 s/tile extra, marginal gains where DINOv3 outlines are already tight.

## SAM3 review loop (`scripts/ml/sam3_review.py`)

Run SAM3 independently to validate boundaries before rebuilding. Writes per tile:
`preview_z_x_y.png` (red = existing archive footprints, yellow = SAM3 would
replace, green = SAM3 mask outlines, cyan = detection boxes with prompt/score/
area labels), `sam3_proposals.geojson` (WGS84 + `prompt`/`status`/`match_iou`),
and `report.json` (replace/extra/missed + mean best IoU, split by prompt).
`--prompts house,tree` reviews roofs and canopies together; SAM3 boxes are
normalized xyxy in model-input space, so multiply by the window image size.
High `missed` + low mean IoU = archive footprints don't line up with the imagery
(offset/merged/vegetation); the preview shows where. Bake in with
`task tiles -- --sam3 --sam3-trees`, then re-run `task addresses`.

## Studio alignment rules (`Deep3DStudio.tsx`, `deep-3d-model.ts`)

- One scale for the whole scene: `k = 32 / placement.metersPerWidth` (z18 tile
  width in metres) is used for the heightfield translation, the heightfield
  displacement, and the roof mesh (`sceneScaleFor`). The vertical gain is
  `k * depthScale * 1.5` for both; moving the relief slider rebuilds both.
- The heightfield is carved flat (0.12 m) inside the footprint: rendering both
  the displaced roof profile and the roof mesh at the same heights caused
  z-fighting that tore the mesh into sawtooth fragments. The mesh is the
  reconstruction; `peakRoofH` still reports `ridgeH` for the metrics panel.
- Footprint resolution: map selection first, then the studio's own nearest
  address-record lookup (45 m), then an explicit no-footprint notice. Without
  this, unresolved locations silently showed the synthetic parcel (area metric
  288 = 16x18, ridge metric = canopy max) with no mesh — the tell-tale sign.
- `runIdRef` guards async model runs; without it a stale synthetic run can
  complete last and clear the real building's mesh (`rebuildBuildingMesh(null)`).
  `[Deep3D] roof mesh: N wall tris, M roof tris` is logged on every rebuild.
- `generateDeep3DHeightmap` receives footprint UV (tile frame); it centres the
  ring on its own centroid and subtracts the same ring centre from each query
  point. If you change one frame, change the other or the bump stamps at the
  tile centre instead of the building.
- Camera framing targets the building at scene origin (plane translated by
  `-offsetEast * k`, `+offsetNorth * k`); keep `handleResetCamera` in sync.

Coordinate checks that settle 3D/2D alignment questions fast:
- Decoded MVT local coords are y-down and feed `tile_local_to_lonlat` directly.
- `ImageryFetcher.window(...).local_to_pixel()` is the one conversion from
  pipeline/MVT local space to preview pixels — draw in pixel space once (a
  second conversion compresses overlays towards the top-left corner).
- Validate overlays with G-NAF address dots (address points are property
  access points, not roof centres, but they must sit along street frontages).
- The archive + address association round-trips correctly: a mirrored/off
  conversion shows up as multi-hundred-metre match distances, not 0–10 m.

## Per-address algorithm (`scripts/ml/addresses.py`)

1. Decode the MBTiles `building` layer to WGS84, **dedupe tile-buffer copies by
   rounded centroid** (essential — without it one building can match dozens of
   addresses).
2. Match each G-NAF address to the containing/nearest footprint (default 12 m).
3. Count **distinct dwelling points** per footprint (base+unit G-NAF records
   share a point and must not inflate counts).
4. Classify kind via exact premises categories (opt-in `--places`, the bundled
   feed has mobile services at home addresses) + geometry: `house`, `units`,
   `business`, `school`, `industrial`, `civic`, `large`.
5. Emit JSONL/JSON: roof ring metres relative to building centroid, eave/ridge,
   type, angle, area, address point, neighbours.

## 3D rendering

- `src/lib/roof-geometry.ts` — shared `buildRoofSections()`: convex decomposition
  of the roof footprint + hipped/gabled/flat tents, positions in metres
  (x=east, y=north, z=up). Used by both the map layer and the studio.
- `src/lib/real-roof-layer.ts` — MapLibre custom layer (`real-roof-3d`), walls +
  coloured roof sections from live tile features. Attach only when
  `map.isStyleLoaded()`; retry on `idle` (style not done loading is a real
  failure mode).
- `src/components/Deep3DStudio.tsx` — resolves the selected address against
  `public/address-roofs.json` (`src/lib/address-roofs.ts`), falls back to tile
  features, renders the real roof mesh on top of a satellite-displaced
  heightfield, metrics from the address record.
- `src/lib/deep-3d-model.ts` — heightfield: real footprint drives the roof
  planes/positive slopes; outside the footprint only dense/dark vegetation
  rises (lawns stay low) so the roof silhouette stays legible.

## Critical gotchas

- **MapLibre worker**: the Next production bundle resolves the worker URL to
  `""`, so no vector/GeoJSON source ever loads (only rasters show). The build
  copies `maplibre-gl-worker.mjs` + `maplibre-gl-shared.mjs` to `public/maplibre`
  (`scripts/copy-maplibre-worker.mjs`, wired into `pnpm dev/build`) and
  `MapComponent` calls `setWorkerUrl("/maplibre/maplibre-gl-worker.mjs")`.
  Symptom: satellite imagery renders but no outlines/trees/roofs.
- **Custom layer projection**: use `options.defaultProjectionData.mainMatrix`
  (column-major, mercator 0..1 → clip). `modelViewProjectionMatrix` is a
  different matrix in MapLibre v6 and projects geometry off-screen.
- **Extrusion visibility**: `enable3D` must be
  `show3DBuildings && !enableLoD2Houses` so MapLibre prisms hide when the roof
  mesh layer owns buildings; `tree-3d-vector` visibility depends only on
  `enableTrees` or trees vanish when roof meshes are on.
- **eave_height units**: `roof.model_roof()` needs `meters_per_unit` (pipeline
  passes `meters_per_px`); wrong units clamp every eave to the 2.4 m minimum.
- **Stale `.next` chunks**: after rebuilding while an old `next start` holds the
  port you get 500s on hashed chunks and a dead map. Always `task stop` (not
  `stop:web`) before `task start`; if in doubt `rm -rf .next` and rebuild.
- The `modelOutput.metrics.peakHeightMeters` uses the roof peak when a footprint
  exists, otherwise the heightfield max.

## Verification workflow

- `task verify:ui` — tiles requested, no page errors, map + studio screenshots.
- For a specific address: open the app (default centre is 27 Cambridge Road,
  Mooroolbark), click Satellite, then the "LoD2 3D" control, and compare the
  studio mesh with the satellite imagery.
- Check props quickly: `curl -s --compressed localhost:8085/tiles/16/x/y.pbf`
  and decode with `mapbox_vector_tile` (see `scripts/ml/tests`).
- Data sanity: `scripts/ml/.venv/bin/python -m scripts.ml.addresses ...` prints
  matched counts and kind distribution; `--format json` writes the web asset.

## File map

- `scripts/ml/pipeline.py` — tile orchestration, SAM3 refinement, roof model.
- `scripts/ml/addresses.py` — per-address extraction (algorithm above).
- `scripts/ml/roof.py` — `model_roof` (type/eave/ridge/angle), SAR fit.
- `scripts/ml/server.py` — realtime sidecar (LRU, prefetch, `--sam3` opt-in).
- `src/components/MapComponent.tsx` — map init, layer lifecycle, building lookup.
- `src/lib/google-maps-style.ts` — 2D/3D layer styling and visibility flags.
- `data/australia_tiles.mbtiles` — pre-built demo archive; `data/australia_geo.db`
  — G-NAF addresses + places SQLite.
- `public/address-roofs.json` — generated per-address records (gitignored).
