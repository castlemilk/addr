# syntax=docker/dockerfile:1
# ------------------------------------------------------------------------------
# Build Stage: Pure Go static compilation
# ------------------------------------------------------------------------------
FROM golang:1.24-alpine AS builder

WORKDIR /src

# Install build essentials and ca-certificates
RUN apk add --no-cache ca-certificates git

# Cache dependencies
COPY go.mod go.sum ./
RUN go mod download

# Copy source tree
COPY . .

# Compile static, stripped binary (CGO_ENABLED=0 for pure-Go modernc.org/sqlite)
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -ldflags="-s -w -extldflags '-static'" \
    -trimpath \
    -o /bin/geoserver \
    ./cmd/server

# ------------------------------------------------------------------------------
# Runtime Stage: Minimal, unprivileged Distroless container
# ------------------------------------------------------------------------------
FROM gcr.io/distroless/static-debian12:nonroot

WORKDIR /app

# Copy CA certs and statically linked binary
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=builder /bin/geoserver /app/geoserver

# Non-root user (UID 65532 is nonroot in distroless)
USER 65532:65532

# Persistent data directory for SQLite & MBTiles (mounted via Kubernetes PVC)
VOLUME ["/data"]

ENV PORT=8080 \
    DB_PATH=/data/australia_geo.db \
    TILES_PATH=/data/australia_tiles.mbtiles \
    TILE_CACHE_MB=512

EXPOSE 8080

ENTRYPOINT ["/app/geoserver"]
