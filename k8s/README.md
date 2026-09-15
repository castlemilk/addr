# Kubernetes Deployment Guide (Vultr Kubernetes Engine - VKE)

This directory contains production-ready Kubernetes manifests for deploying the offline `addr-geoserver` microservice with persistent spatial databases and an in-memory vector tile LRU cache.

---

## 1. Architecture on VKE

- **Container Runtime**: Distroless non-root (`gcr.io/distroless/static-debian12:nonroot`) static Go binary with pure-Go SQLite (`modernc.org/sqlite`).
- **Storage Layer**: 50Gi+ Vultr Block Storage volume (`vultr-block-storage` CSI driver) mounted at `/data`.
  - `australia_geo.db`: Offline G-NAF & OpenStreetMap spatial SQLite database with FTS5 and spatial indexing.
  - `australia_tiles.mbtiles`: Vector tile MBTiles archive for vector tile rendering.
- **In-Memory Cache**: 32-shard concurrent LRU cache strictly bounded to e.g. 512MB RAM, eliminating pod OOM kills while delivering sub-20ns hot tile lookups.
- **Realtime Generation (optional)**: Set `TILE_GENERATOR_URL` to a running `scripts/ml/server.py` sidecar to generate building tiles on cache/MBTiles misses (`X-Tile-Generated: ai`). Deploy the sidecar as a separate Deployment with its own CPU/memory limits; tiles can be persisted back to the PVC by running the sidecar with `--persist-mbtiles /data/australia_tiles.mbtiles`.
- **Protocols**: Dual HTTP/1.1 and HTTP/2 cleartext (h2c) supporting Connect-Go, gRPC, and REST clients.

---

## 2. Deploying to Vultr Kubernetes Engine

### Step A: Verify StorageClass
VKE pre-installs the `vultr-block-storage` CSI driver. Verify it is available in your cluster:
```bash
kubectl get storageclass
# Expected output:
# NAME                   PROVISIONER          RECLAIMPOLICY   VOLUMEBINDINGMODE   ALLOWVOLUMEEXPANSION
# vultr-block-storage    csi.vultr.com        Delete          Immediate           true
```

### Step B: Apply Manifests
```bash
kubectl apply -k k8s/
```

### Step C: Seed Data into the Persistent Volume
Because block storage starts empty, seed the spatial database (`australia_geo.db`) and tile archive (`australia_tiles.mbtiles`) onto the volume:

```bash
# Wait for the pod to start or use a seed pod
POD_NAME=$(kubectl get pods -l app.kubernetes.io/name=addr-geoserver -o jsonpath='{.items[0].metadata.name}')

# Copy SQLite cadastral & POI database to the PVC volume
kubectl cp data/australia_geo.db ${POD_NAME}:/data/australia_geo.db

# Copy vector tile archive to the PVC volume
kubectl cp data/australia_tiles.mbtiles ${POD_NAME}:/data/australia_tiles.mbtiles
```

Alternatively, use a Kubernetes Job with `rclone` or `wget` to pull directly from private object storage.

---

## 3. Monitoring & Cache Telemetry

The microservice exposes health and telemetry endpoints:

| Endpoint | Purpose | Format |
|---|---|---|
| `GET /healthz` | Kubernetes Liveness Probe | Plaintext (`ok`) |
| `GET /readyz` | Kubernetes Readiness Probe | Plaintext (`ready`) |
| `GET /tiles/stats` | Cache hit ratio, total bytes, evictions, generator counters | JSON |
| `GET /tiles/{z}/{x}/{y}.pbf` | Vector tile endpoint | Protocol Buffers (`application/vnd.mapbox-vector-tile`) |
| `POST /geo.v1.GeoService/Search` | Typo-tolerant autocomplete & search | Connect-Go / gRPC / JSON |

### Sample Cache Metrics Response:
```json
{
  "hits": 14205,
  "misses": 381,
  "hit_ratio": 0.9739,
  "evictions": 42,
  "total_bytes": 184549376,
  "total_items": 1280,
  "max_bytes": 536870912,
  "generator": {
    "requests": 129,
    "found": 121,
    "empty": 8,
    "failures": 0
  }
}
```
