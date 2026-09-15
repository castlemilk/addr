# GeoAU: Offline Australian GPS Address & POI Engine

A 100% offline, high-performance reverse geocoding engine and interactive frontend micro-UI for Australian cadastral addresses and commercial Points of Interest (POIs).

Built as a **Go 1.27 microservice** using **Buf v2** and **Connect-Go**, serving a **Connect-ES** Next.js frontend with embedded **SQLite R\*Tree & FTS5**.

---

## ⚡ Architecture & Tech Stack

```
   ┌─────────────────────────────────────────────────────────────┐
   │                   Next.js 16 + React 19                     │
   │               Connect-ES Typed Client                       │
   │         (MapComponent + Sidebar + Live Autocomplete)        │
   └──────────────────────────────┬──────────────────────────────┘
                                  │ Connect Protocol (HTTP/2 / h2c)
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │                 Go 1.27 Microservice (8085)                 │
   │           Connect-Go (connectrpc.com/connect)               │
   │  - errors.AsType[E] (Go 1.27 generic error extraction)      │
   │  - t.Attr structured testing attributes                     │
   │  - sync.WaitGroup.Go concurrent R*Tree lookups              │
   │  - new(expr) (Go 1.26 value-expression allocation)          │
   └──────────────────────────────┬──────────────────────────────┘
                                  │
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │           Embedded SQLite Store (australia_geo.db)          │
   │  - `places_rtree`: 7,800+ POIs with Haversine re-ranking    │
   │  - `addresses_rtree`: 10,000+ official G-NAF addresses      │
   │  - `places_fts` & `addresses_fts`: FTS5 full-text search    │
   └─────────────────────────────────────────────────────────────┘
```

---

## 🚀 Running the Services

### 1. Start the Go Microservice
```bash
./bin/geoserver -port 8085 -db data/australia_geo.db
```
*Health Check:* `curl http://localhost:8085/healthz`

### 2. Start the Connect-ES Frontend
```bash
NEXT_PUBLIC_GEO_SERVICE_URL=http://localhost:8085 pnpm run start -p 3004
```
Open [http://localhost:3004](http://localhost:3004) in your browser.

---

## 🧰 Taskfile Workflow

All build, run, test, and lint workflows are available through [Task](https://taskfile.dev):

```bash
task setup          # Go modules + pnpm install + Python venv
task generate       # buf lint + buf generate (Go/TS protobuf clients)
task build          # Go binary + Next.js production bundle

task test           # go test + pytest + tsc
task lint           # go vet + eslint + ruff

task start          # ML generator + Go service + web UI in background
task status         # process state + health checks for all three services
task logs           # tail .run/*.log
task stop           # stop all background services
task restart        # stop + start
task dev            # ML + Go in background, Next.js dev server in foreground

task models         # download ONNX models, e.g. task models -- --presets hot-buildings
task tiles          # batch-build building tiles, e.g. task tiles -- --bbox 145.308,-37.797,145.320,-37.786 --zooms 16 --heights
task addresses      # extract per-address roof records, e.g. task addresses -- --bbox 145.308,-37.797,145.320,-37.786
task verify:ui      # headless Chrome smoke test: tiles, roof meshes, Deep 3D Studio (screenshots in /tmp/addr-verify)
task ml             # run the realtime tile generator in the foreground
```

Ports default to Go `8085`, ML `8090`, web `3004` and can be overridden per run:
`SERVER_PORT=9000 ML_PORT=9001 WEB_PORT=9002 task start`. PID files and logs live in `.run/`.
`task start` wires the Go service to the ML sidecar automatically via
`TILE_GENERATOR_URL`, so new tiles are generated on demand.

---

## 🛰️ AI Building Tile Pipeline (2D Imagery → 3D Structures)

An offline Python pipeline (`scripts/ml/`) converts high-resolution aerial imagery
into **Google-Maps-style building footprints, tree canopies, and metric heights**,
written as gzip-compressed Mapbox Vector Tiles into an overlay MBTiles archive.
The existing Go microservice serves it unchanged at `/tiles/{z}/{x}/{y}.pbf`, and
the MapLibre style renders flat outlines at street zooms plus extruded volumes in
3D, with per-feature metadata popups (height, floors, footprint area, volume).

Pipeline stages:

```
aerial tiles (Google/ESRI) → building segmentation (ONNX, sliding window)
   → optional SAM3 text-prompt roof refinement → monocular depth (Depth Anything V2)
   → shared metric height calibration → roof model (eave/ridge/type/orientation)
   → vegetation (ExG) + depth canopy extraction → polygonization + simplification
   → OSM cadastre merge (IoU) → MVT/MBTiles (building + tree layers)
```

The MapLibre map renders building outlines at street zooms and, in 3D, the
`RealRoofLayer` builds real pitched-roof meshes from the tile footprints plus the
estimated eave/ridge/roof-type parameters (no procedural stock houses).

### 1. Install the pipeline environment

```bash
uv venv --python 3.12 scripts/ml/.venv
uv pip install --python scripts/ml/.venv/bin/python -r scripts/ml/requirements-dev.txt
```

### 2. Download pretrained models

```bash
# list available presets
scripts/ml/.venv/bin/python -m scripts.ml.fetch_models --list

# recommended: HOT fAIr DINOv3-S building footprints (aerial) + Depth Anything V2
scripts/ml/.venv/bin/python -m scripts.ml.fetch_models
```

| Preset | Model | Size | Notes |
| :--- | :--- | ---: | :--- |
| `hot-buildings` | HOT fAIr DINOv3-S (UPerNet) | 220 MB | Aerial building footprints, 256px window @ 192px stride |
| `geobase-unet` | GeoBase U-Net | 30 MB | Lightweight alternative |
| `ade20k-segformer` | SegFormer-B0 ADE20K | 15 MB | Generic classes (`building`, `house`), coarse |
| `sam3` | SAM 3 text-promptable (int8 ONNX) | 880 MB | Roof-outline refinement with prompts like `"house"` |
| `depth` | Depth Anything V2 Small | 95 MB | Monocular depth for per-building heights |

### 3. Build building tiles for an area

```bash
scripts/ml/.venv/bin/python -m scripts.ml.pipeline \
  --bbox 145.308,-37.797,145.320,-37.786 \
  --zooms 16 \
  --heights \
  --trees \
  --imagery-offset 3 \
  --simplify-px 0.75 \
  --min-area-m2 12 \
  --preview-dir data/previews \
  --geojson data/buildings.geojson \
  --out data/australia_tiles.mbtiles
```

Key options: `--zooms 15,16`, `--provider esri`, `--imagery-offset 3` (imagery
zoom = output + offset; z19 source imagery gives the tightest roof boundaries at
the cost of ~2-3x runtime), `--simplify-px 0.75` for finer polygon edges,
`--min-area-m2 12` to drop segmentation speckle, `--trees` to add the
depth-filtered canopy layer, `--sam3` to refine roof outlines with SAM 3 text
prompts (`--sam3-prompt house`, `--sam3-threshold 0.4`, `--sam3-min-iou 0.3`;
adds ~40 s/tile on CPU), `--sam3-trees` to also refine/complete canopies
(`--sam3-tree-prompt tree`), `--no-osm` to skip the Overpass merge, `--heuristic`
for a model-free offline run, `--procedural` for synthetic imagery,
`--limit-tiles N` for quick samples. `--preview-dir` writes annotated QA images
showing each footprint colored by source (red = AI building, green = AI tree,
blue = OSM, purple = merged) with heights.

### 3b. Extract per-address roofs

Associates G-NAF address points with the extracted roof footprints and emits one
record per address (roof ring in metres, roof model, area, and a building kind):

```bash
task addresses -- --bbox 145.308,-37.797,145.320,-37.786
# or directly
scripts/ml/.venv/bin/python -m scripts.ml.addresses \
  --tiles data/australia_tiles.mbtiles \
  --db data/australia_geo.db \
  --bbox 145.308,-37.797,145.320,-37.786 \
  --out public/address-roofs.json --format json
```

The tool deduplicates tile-buffer copies of footprints, matches each address to
the containing/nearest footprint (default 12 m), counts distinct dwellings per
footprint, and classifies the building kind: `house`, `units`, `business`,
`school`, `industrial`, `civic`, `large` (large uses footprint area and dwelling
count; POI categories are only consulted with `--places` because the bundled
places feed contains mobile services registered at home addresses).
`public/address-roofs.json` is generated data (gitignored) and feeds the Deep 3D
Studio lookup; without it the studio falls back to live map tile features.

### 3c. Review SAM3 boundaries separately

SAM 3 can be run on its own to inspect and validate roof and canopy boundaries
before baking them into an archive:

```bash
task sam3:review -- --bbox 145.310669,-37.792422,145.316162,-37.788081 --zooms 16 --limit-tiles 1 --prompts house,tree
# equivalently
scripts/ml/.venv/bin/python -m scripts.ml.sam3_review \
  --bbox 145.310669,-37.792422,145.316162,-37.788081 --zooms 16 \
  --existing data/australia_tiles.mbtiles \
  --prompts house,tree --threshold 0.4 --out data/sam3-review
```

For every tile it writes:

- `preview_z_x_y.png` — imagery with the existing archive footprints in red
  (yellow where a SAM3 proposal would replace them), SAM3 mask outlines in
  green, and cyan detection boxes labelled with the prompt, confidence, and
  area. Inspect this to judge boundary quality.
- `sam3_proposals.geojson` — SAM3 polygons in WGS84 with `prompt`, `score`,
  `area_m2`, `match_iou`, and `status` (`replace` / `weak` / `extra`).
- `report.json` — per-tile and per-prompt `replace`/`extra`/`missed` counts plus
  the mean best IoU against the existing archive.

Reading the report: high `missed` counts with low mean IoU mean the archive
footprints do not line up with the imagery/SAM3 (offset, merged, or vegetation
blobs) — the preview shows exactly where. When the proposals look good, bake
them into a rebuild with `--sam3` (roofs) and `--sam3-trees` (canopies, prompt
`tree` by default) and regenerate `task addresses`.

### 4. Serve and view

Restart the Go microservice with the generated archive (see above), reload the
frontend, and enable **3D Building Extrusions** in the layers menu. The
`local-vector` source exposes a `building` layer with `height`, `min_height`,
`render_height`, `render_min_height`, `source`, `levels`, `area_m2`, `kind`,
`roof_type`, `eave_height`, `ridge_height`, and `ridge_angle`, plus a `tree`
layer (AI trees toggle) with the same height fields. In 3D the roof mesh layer
constructs walls plus hipped/gabled/flat roofs from those parameters. Clicking a
rendered building or canopy shows estimated height, roof model, floors,
footprint area, and volume, labelled by provenance (AI / OSM / OSM+AI).

The **Deep 3D Studio** (opened from the map controls) resolves the selected
address against `address-roofs.json` (falling back to tile features), then
renders the real extracted roof mesh — convex-section decomposition of the roof
footprint with walls at eave height and hipped/gabled/flat planes — alongside the
satellite-displaced heightfield. The heightfield and the roof mesh share one
scene scale and vertical gain; the heightfield is carved flat under the
footprint so the reconstruction never z-fights the displacement, and the mesh
is the roof you see (the bump remains for footprints without a mesh). The metrics panel reports the
extracted roof area, eave, ridge, pitch, and volume for that address, and the
kind badge (`HOUSE`, `UNITS`, `BUSINESS`, ...) comes from the per-address
classification. If the map selection did not resolve a footprint, the studio
itself falls back to the nearest per-address record (45 m), and when nothing
exists it says so instead of silently rendering terrain (presets always show
that notice; roof-mesh triangle counts are logged as `[Deep3D]`). Drag with the
left mouse button to orbit (or use the rotate/auto-orbit buttons below the
viewport); the main map rotates with the new rotate buttons or right-drag, in
both 2D and 3D modes.

### 5. Realtime generation (no pre-build required)

For areas without pre-built MBTiles, run the generation sidecar and point the Go
service at it. On cache and MBTiles misses the Go tile server proxies to the
sidecar, caches the result in its in-memory LRU, and serves it with
`X-Tile-Generated: ai`. Empty results are cached too, so empty areas are only
generated once. Generated tiles can optionally be persisted back into an
MBTiles archive for future runs. Tree canopy extraction is on by default
(`--no-trees` to disable); it requires `--heights`.

```bash
# Terminal 1: generation sidecar (models load once; LRU + neighbor prefetch)
scripts/ml/.venv/bin/python -m scripts.ml.server --port 8090 --heights

# Terminal 2: Go microservice with realtime fallback
./bin/geoserver -port 8085 -db data/australia_geo.db \
  -tiles data/australia_tiles.mbtiles \
  -tile-generator-url http://localhost:8090
```

Or via environment: `TILE_GENERATOR_URL`, `TILE_GENERATOR_TIMEOUT` (default 120s).

Realtime tuning:

| Flag | Effect |
| :--- | :--- |
| `--imagery-offset 1` (default) | 2.8s/tile at z16; use `2` for higher-fidelity footprints (~4x windows) |
| `--concurrency 2` | Parallel tile generations (CPU-bound; 2–4 typical) |
| `--no-prefetch` | Disable neighbor tile warming after each generation |
| `--no-trees` | Disable canopy extraction (trees are on by default) |
| `--persist-mbtiles PATH` | Write generated tiles into an MBTiles archive |
| `--seg-stride`, `--threshold`, `--min-area-m2` | Quality/latency trade-offs |

Observe generation with `curl localhost:8085/tiles/stats` (cache hit ratio plus
`generator` request/found/empty/failure counters).

---

## 🛠️ Protobuf & Buf Workflow

API definitions are specified in `proto/geo/v1/geo.proto` and managed via **Buf v2**:

```bash
# Lint protobuf definitions
buf lint

# Generate Go (Connect-Go) and TypeScript (Connect-ES) code
buf generate
```

Generated outputs:
* `gen/go/geo/v1/geo.pb.go` & `geov1connect/geo.connect.go`
* `src/gen/geo/v1/geo_pb.ts` & `geo_connect.ts`

---

## 🧪 Testing the Go Microservice

`task test` runs all suites: Go unit/integration tests, the Python ML pipeline
tests (`scripts/ml/tests`, including per-address association/classification),
and `tsc`. `task lint` runs `go vet`, ESLint, and ruff.

End-to-end UI verification runs the production bundle in headless Chrome,
switches to satellite, waits for vector tiles, opens the Deep 3D Studio, and
fails on page errors or missing tile requests:

```bash
task verify:ui                 # screenshots written to /tmp/addr-verify
APP_URL=http://localhost:3004 CHROME_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" node scripts/verify-map.mjs
```

Tests adhere to `skunkworq-skills/go-best-practices` using Go 1.27 `t.Attr` structured attributes:

```bash
go test -v ./...
```

Output:
```
=== RUN   TestHaversineDistance
=== ATTR  TestHaversineDistance tier unit
=== ATTR  TestHaversineDistance component spatial
--- PASS: TestHaversineDistance (0.00s)
=== RUN   TestStoreQueries
=== ATTR  TestStoreQueries tier integration
=== ATTR  TestStoreQueries db sqlite
--- PASS: TestStoreQueries (0.02s)
```

---

## 🔌 Connect RPC Endpoints

All endpoints support standard Connect, gRPC, and JSON over HTTP POST:

### 1. Reverse Geocode (GPS $\to$ POI & Address)
```bash
curl -s -X POST http://localhost:8085/geo.v1.GeoService/ReverseGeocode \
  -H "Content-Type: application/json" \
  -d '{"latitude": -33.8848, "longitude": 151.1960}'
```
*Response in ~1.3 ms:*
```json
{
  "query": { "latitude": -33.8848, "longitude": 151.196 },
  "matchType": "poi_and_address",
  "place": {
    "name": "Zigi's Wine Bar",
    "category": "wine_bar",
    "addressText": "8 Shepherd St",
    "distanceMeters": 7.1
  },
  "address": {
    "fullAddress": "202-206 BROADWAY, Chippendale NSW 2008",
    "distanceMeters": 86.9
  },
  "executionTimeMs": 1.29
}
```

### 2. Elastic Autocomplete & Typo-Tolerant Search
Full-text search features bidirectional Australian street abbreviation expansion (e.g., `rd` $\leftrightarrow$ `road`, `st` $\leftrightarrow$ `street`) and Damerau-Levenshtein edit-distance typo tolerance:

```bash
# Exact street match
curl -s -X POST http://localhost:8085/geo.v1.GeoService/Search \
  -H "Content-Type: application/json" \
  -d '{"query": "27 cambridge road", "limit": 1}'

# Abbreviation match (rd -> road)
curl -s -X POST http://localhost:8085/geo.v1.GeoService/Search \
  -H "Content-Type: application/json" \
  -d '{"query": "27 cambridge rd", "limit": 1}'

# Typo tolerance (transposition "cambrdige" -> "cambridge")
curl -s -X POST http://localhost:8085/geo.v1.GeoService/Search \
  -H "Content-Type: application/json" \
  -d '{"query": "27 cambrdige road", "limit": 1}'

# Prefix autocomplete as you type
curl -s -X POST http://localhost:8085/geo.v1.GeoService/Search \
  -H "Content-Type: application/json" \
  -d '{"query": "27 camb", "limit": 1}'
```

All 4 queries accurately resolve to `27 CAMBRIDGE ROAD, Mooroolbark VIC 3138` with top ranking score.

### 3. Database Statistics
```bash
curl -s -X POST http://localhost:8085/geo.v1.GeoService/GetStats \
  -H "Content-Type: application/json" \
  -d '{}'
```

---

## 📊 Benchmarks & Performance Evaluations

Benchmarks were conducted on an Apple Silicon M5 Max with Go 1.27 using the new `b.Loop()` iterator API (`go test -bench=. -benchmem -benchtime=2s`).

### 1. Lookup Latency & Memory Evolution

| Benchmark Scenario | Baseline (Mutex + Ad-hoc Queries) | Optimized (HashTrieMap `sync.Map` + Prepared Stmts + Proto Cache) | Latency Improvement | Allocation Reduction |
| :--- | :--- | :--- | :--- | :--- |
| **Store Hot ReverseGeocode** | `156,200 ns/op` (101 KB) | **`57.6 ns/op`** (240 B) | **2,711x faster** | 3,013 $\to$ 1 alloc (**99.97%**) |
| **Store Parallel ReverseGeocode** | `31,400 ns/op` (Lock contention) | **`18.4 ns/op`** (Zero lock overhead) | **1,706x faster** | Contention eliminated |
| **Service Hot ReverseGeocode** | `1,215 ns/op` (3,672 B) | **`262.8 ns/op`** (264 B) | **4.6x faster** | 30 $\to$ 3 allocs (**90%**) |
| **Service Parallel ReverseGeocode** | `757.7 ns/op` (1,650 B) | **`206.2 ns/op`** (264 B) | **3.7x faster** | 13 $\to$ 3 allocs (**77%**) |
| **Service Cold ReverseGeocode (DB)** | `156,000 ns/op` | **`134,140 ns/op`** (Prepared R*Tree) | **14% faster** | SQL parsing bypassed |
| **End-to-End HTTP Roundtrip** | `27,064 ns/op` (24,249 B) | **`20,800 B/op`** (225 allocs) | Scalable HTTP/2 pipeline | 252 $\to$ 225 allocs |

### 2. Key Profiling Discoveries & Architectural Optimizations

1. **`sync.Map` vs `sync.RWMutex` in Go 1.24+**:
   - In Go 1.24+, `sync.Map` is rebuilt on a concurrent **HashTrieMap** providing lock-free reads and segmented shard-level writes without write-amplification or dirty-map promotions.
   - For spatial coordinate caching (quantized 64-bit integer grid keys), `sync.Map` outperforms `sync.RWMutex` under multi-core parallel workloads by **1,700x**, dropping parallel lookups to **18.4 ns**.
2. **SQLite Virtual Memory (`mmap_size`)**:
   - `pprof` analysis revealed that 54% of CPU execution during uncached spatial queries was consumed by Darwin `fcntl` advisory locks and `syscall.rawsyscalln` reads.
   - Enabling `_pragma=mmap_size(268435456)&_pragma=query_only(1)` maps the database directly into virtual memory pages, eliminating read syscalls.
3. **Prepared Statements on Spatial R\*Tree & FTS5**:
   - Preparing `placesStmt`, `addrsStmt`, `placesFts`, and `addrsFts` at startup eliminates repeated query parsing and SQLite VDBE bytecode compilation per request.
4. **Service-Level Protobuf Response Caching**:
   - Connect-Go handlers previously reconstructed nested `*geov1.Place` and `*geov1.Address` structs on every request (30 allocations per hot hit).
   - By caching pre-built `*geov1.ReverseGeocodeResponse` pointers in a bounded service `sync.Map` keyed by `(cell uint64, radius uint32)`, hot hits perform a shallow copy and only instantiate the exact query coordinate (reduced from 30 allocs to 3 allocs, 262.8 ns).
5. **Exact Slice Pre-Allocation**:
   - Replaced dynamic `append()` on empty slices with `make([]*geov1.Place, len(places))` across `ReverseGeocode`, `Search`, `GetNearby`, and `GetStats`, preventing slice header relocations.

