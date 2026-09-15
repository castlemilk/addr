package tile

import (
	"bytes"
	"compress/gzip"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"

	_ "modernc.org/sqlite"
)

// Store provides access to vector/raster map tiles backed by an in-memory LRU cache
// and an optional on-disk SQLite MBTiles container (e.g. on Kubernetes PVC).
type Store struct {
	cache       *ShardedLRUCache
	db          *sql.DB
	stmt        *sql.Stmt
	mu          sync.RWMutex
	mbtilesPath string
	generator   *Generator
}

// NewStore initializes the tile store with an LRU cache and an optional MBTiles database path.
func NewStore(mbtilesPath string, cacheBytes int64) (*Store, error) {
	cache := NewShardedLRUCache(cacheBytes)
	s := &Store{
		cache:       cache,
		mbtilesPath: mbtilesPath,
	}

	if mbtilesPath != "" {
		if _, err := os.Stat(mbtilesPath); err == nil {
			dsn := fmt.Sprintf("file:%s?mode=ro&_pragma=busy_timeout(5000)&_pragma=cache_size(-32000)&_pragma=mmap_size(268435456)&_pragma=query_only(1)", mbtilesPath)
			db, err := sql.Open("sqlite", dsn)
			if err != nil {
				slog.Warn("failed to open MBTiles database, running cache-only mode", "path", mbtilesPath, "error", err)
			} else {
				db.SetMaxOpenConns(32)
				db.SetMaxIdleConns(16)
				stmt, err := db.Prepare("SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ? LIMIT 1;")
				if err != nil {
					_ = db.Close()
					slog.Warn("failed to prepare MBTiles query statement", "error", err)
				} else {
					s.db = db
					s.stmt = stmt
					slog.Info("initialized MBTiles disk storage backend", "path", mbtilesPath)
				}
			}
		} else {
			slog.Info("MBTiles file not found, running with in-memory LRU tile cache", "path", mbtilesPath)
		}
	}

	return s, nil
}

// Close releases the prepared statement and database connection.
func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()

	s.cache.Clear()
	if s.stmt != nil {
		_ = s.stmt.Close()
	}
	if s.db != nil {
		return s.db.Close()
	}
	return nil
}

// SetGenerator attaches a realtime AI tile generator used on cache and MBTiles misses.
func (s *Store) SetGenerator(generator *Generator) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.generator = generator
}

// GetTile retrieves a tile by Web Mercator XYZ coordinates.
// Checks LRU cache first (< 10ns), falls back to MBTiles disk lookups, then to the
// realtime generator when configured.
func (s *Store) GetTile(z, x, y uint32) (Tile, bool, error) {
	return s.GetTileContext(context.Background(), z, x, y)
}

// GetTileContext is GetTile with request-scoped cancellation for realtime generation.
func (s *Store) GetTileContext(ctx context.Context, z, x, y uint32) (Tile, bool, error) {
	coord := TileCoord{Z: z, X: x, Y: y}

	// 1. Fast path: in-memory LRU cache
	if t, ok := s.cache.Get(coord); ok {
		return t, true, nil
	}

	// 2. Slow path: on-disk MBTiles database (e.g. from Kubernetes PVC)
	s.mu.RLock()
	stmt := s.stmt
	generator := s.generator
	s.mu.RUnlock()

	if stmt != nil {
		// Convert XYZ to TMS: y_tms = (1 << z) - 1 - y_xyz
		tmsY := (uint32(1) << z) - 1 - y

		var rawData []byte
		err := stmt.QueryRow(z, x, tmsY).Scan(&rawData)
		if err == nil && len(rawData) > 0 {
			tile := Tile{
				Data:        rawData,
				ContentType: "application/vnd.mapbox-vector-tile",
				ETag:        fmt.Sprintf(`"%d-%d-%d-%d"`, z, x, y, len(rawData)),
			}
			s.cache.Put(coord, tile)
			return tile, true, nil
		}
	}

	// 3. Realtime generation path: proxy to the AI tile generator and cache the result.
	if generator != nil {
		if data, found, err := generator.Generate(ctx, z, x, y); err != nil {
			slog.Warn("realtime tile generation failed", "z", z, "x", x, "y", y, "error", err)
		} else {
			tile := Tile{
				ContentType: "application/vnd.mapbox-vector-tile",
				Generated:   true,
			}
			if found {
				tile.Data = data
				tile.ETag = fmt.Sprintf(`"ai-%d-%d-%d-%d"`, z, x, y, len(data))
			}
			// Cache misses (including empty results) to avoid regenerating hot tiles.
			s.cache.Put(coord, tile)
			return tile, true, nil
		}
	}

	return Tile{}, false, nil
}

// PutTile manually inserts a tile into the in-memory cache.
func (s *Store) PutTile(z, x, y uint32, tile Tile) {
	s.cache.Put(TileCoord{Z: z, X: x, Y: y}, tile)
}

// Stats returns operational metrics for Prometheus/monitoring.
func (s *Store) Stats() CacheStats {
	return s.cache.Stats()
}

// StoreStats combines cache metrics with realtime generator counters.
type StoreStats struct {
	CacheStats
	Generator map[string]uint64 `json:"generator,omitempty"`
}

// StoreStats returns cache metrics plus generator counters when realtime generation is enabled.
func (s *Store) StoreStats() StoreStats {
	stats := StoreStats{CacheStats: s.cache.Stats()}
	s.mu.RLock()
	generator := s.generator
	s.mu.RUnlock()
	if generator != nil {
		stats.Generator = generator.Stats()
	}
	return stats
}

// Handler returns an http.Handler that serves vector tiles and Prometheus-ready cache metrics.
func (s *Store) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, If-None-Match")

		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}

		if r.URL.Path == "/tiles/stats" || r.URL.Path == "/tiles/metrics" {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(s.StoreStats())
			return
		}

		// Parse /tiles/{z}/{x}/{y}.pbf
		path := strings.TrimPrefix(r.URL.Path, "/tiles/")
		parts := strings.Split(path, "/")
		if len(parts) != 3 {
			http.Error(w, "invalid tile coordinates format: /tiles/{z}/{x}/{y}.pbf", http.StatusBadRequest)
			return
		}

		zVal, err := strconv.ParseUint(parts[0], 10, 32)
		if err != nil {
			http.Error(w, "invalid z", http.StatusBadRequest)
			return
		}

		xVal, err := strconv.ParseUint(parts[1], 10, 32)
		if err != nil {
			http.Error(w, "invalid x", http.StatusBadRequest)
			return
		}

		yPart := strings.TrimSuffix(parts[2], ".pbf")
		yPart = strings.TrimSuffix(yPart, ".mvt")
		yVal, err := strconv.ParseUint(yPart, 10, 32)
		if err != nil {
			http.Error(w, "invalid y", http.StatusBadRequest)
			return
		}

		tile, ok, err := s.GetTileContext(r.Context(), uint32(zVal), uint32(xVal), uint32(yVal))
		if err != nil {
			http.Error(w, "failed to read tile", http.StatusInternalServerError)
			return
		}

		if !ok || len(tile.Data) == 0 {
			// Return empty 204 No Content for missing/empty tiles to avoid client errors
			w.WriteHeader(http.StatusNoContent)
			return
		}

		// Check ETag
		if match := r.Header.Get("If-None-Match"); match != "" && match == tile.ETag {
			w.WriteHeader(http.StatusNotModified)
			return
		}

		w.Header().Set("Content-Type", tile.ContentType)
		w.Header().Set("Cache-Control", "public, max-age=604800, immutable")
		if tile.ETag != "" {
			w.Header().Set("ETag", tile.ETag)
		}
		if tile.Generated {
			w.Header().Set("X-Tile-Generated", "ai")
		}

		// Check if payload is gzipped
		if isGzipped(tile.Data) {
			w.Header().Set("Content-Encoding", "gzip")
		}

		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(tile.Data)
	})
}

// isGzipped checks whether the buffer starts with the gzip magic bytes.
func isGzipped(b []byte) bool {
	return len(b) >= 2 && b[0] == 0x1f && b[1] == 0x8b
}

// GzipCompress helper to compress tile data before storage.
func GzipCompress(data []byte) ([]byte, error) {
	var buf bytes.Buffer
	gw := gzip.NewWriter(&buf)
	if _, err := io.Copy(gw, bytes.NewReader(data)); err != nil {
		return nil, err
	}
	if err := gw.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}
