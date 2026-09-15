package tile

import (
	"context"
	"database/sql"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	_ "modernc.org/sqlite"
)

var testGzipPayload = []byte{0x1f, 0x8b, 0x08, 0x00, 0x01, 0x02, 0x03}

func newTestMBTiles(t *testing.T, z, x, y int, data []byte) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "tiles.mbtiles")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatalf("open mbtiles: %v", err)
	}
	defer db.Close()
	if _, err := db.Exec("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)"); err != nil {
		t.Fatalf("create tiles table: %v", err)
	}
	tmsRow := (1 << z) - 1 - y
	if _, err := db.Exec(
		"INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
		z, x, tmsRow, data,
	); err != nil {
		t.Fatalf("insert tile: %v", err)
	}
	return path
}

func TestStoreUsesGeneratorOnMissAndCaches(t *testing.T) {
	var calls atomic.Int64
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/tile/16/100/200.pbf" {
			t.Errorf("unexpected generator path: %s", r.URL.Path)
		}
		w.Header().Set("Content-Encoding", "gzip")
		_, _ = w.Write(testGzipPayload)
	}))
	defer upstream.Close()

	store, err := NewStore("", 1<<20)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	defer store.Close()
	store.SetGenerator(NewGenerator(upstream.URL, 5*time.Second))

	tile, ok, err := store.GetTileContext(context.Background(), 16, 100, 200)
	if err != nil || !ok {
		t.Fatalf("expected generated tile, ok=%v err=%v", ok, err)
	}
	if string(tile.Data) != string(testGzipPayload) {
		t.Fatalf("payload mismatch: %q", tile.Data)
	}
	if !tile.Generated {
		t.Fatal("expected Generated=true")
	}

	if _, ok, _ := store.GetTile(16, 100, 200); !ok {
		t.Fatal("expected cached hit")
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("expected 1 generator call, got %d", got)
	}

	stats := store.StoreStats()
	if stats.Generator["found"] != 1 {
		t.Fatalf("expected 1 found generation, got %v", stats.Generator)
	}
}

func TestStoreCachesEmptyGeneratorResult(t *testing.T) {
	var calls atomic.Int64
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()

	store, err := NewStore("", 1<<20)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	defer store.Close()
	store.SetGenerator(NewGenerator(upstream.URL, 5*time.Second))

	tile, ok, err := store.GetTileContext(context.Background(), 16, 1, 2)
	if err != nil || !ok {
		t.Fatalf("expected cached empty tile, ok=%v err=%v", ok, err)
	}
	if len(tile.Data) != 0 || !tile.Generated {
		t.Fatalf("expected empty generated tile, got %+v", tile)
	}

	tile, ok, _ = store.GetTile(16, 1, 2)
	if !ok || len(tile.Data) != 0 {
		t.Fatal("expected cached empty tile on second call")
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("expected 1 generator call, got %d", got)
	}
}

func TestStoreDegradesGracefullyOnGeneratorError(t *testing.T) {
	var calls atomic.Int64
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer upstream.Close()

	store, err := NewStore("", 1<<20)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	defer store.Close()
	store.SetGenerator(NewGenerator(upstream.URL, 5*time.Second))

	if _, ok, err := store.GetTile(16, 3, 4); ok || err != nil {
		t.Fatalf("expected graceful miss, ok=%v err=%v", ok, err)
	}
	if _, ok, _ := store.GetTile(16, 3, 4); ok {
		t.Fatal("failed generations must not be cached")
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("expected retry on failure, got %d calls", got)
	}
	if failures := store.StoreStats().Generator["failures"]; failures != 2 {
		t.Fatalf("expected 2 recorded failures, got %d", failures)
	}
}

func TestStorePrefersMBTilesOverGenerator(t *testing.T) {
	path := newTestMBTiles(t, 16, 100, 200, []byte("disk-tile"))
	var calls atomic.Int64
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		_, _ = w.Write(testGzipPayload)
	}))
	defer upstream.Close()

	store, err := NewStore(path, 1<<20)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	defer store.Close()
	store.SetGenerator(NewGenerator(upstream.URL, 5*time.Second))

	tile, ok, err := store.GetTile(16, 100, 200)
	if err != nil || !ok {
		t.Fatalf("expected disk tile, ok=%v err=%v", ok, err)
	}
	if string(tile.Data) != "disk-tile" || tile.Generated {
		t.Fatalf("expected disk tile without generation, got %+v", tile)
	}
	if calls.Load() != 0 {
		t.Fatal("generator must not be called when MBTiles has the tile")
	}
}

func TestHandlerMarksGeneratedTiles(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Encoding", "gzip")
		_, _ = w.Write(testGzipPayload)
	}))
	defer upstream.Close()

	store, err := NewStore("", 1<<20)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	defer store.Close()
	store.SetGenerator(NewGenerator(upstream.URL, 5*time.Second))

	request := httptest.NewRequest(http.MethodGet, "/tiles/16/100/200.pbf", nil)
	recorder := httptest.NewRecorder()
	store.Handler().ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", recorder.Code)
	}
	if got := recorder.Header().Get("X-Tile-Generated"); got != "ai" {
		t.Fatalf("expected X-Tile-Generated=ai, got %q", got)
	}
	if got := recorder.Header().Get("Content-Encoding"); got != "gzip" {
		t.Fatalf("expected gzip content encoding, got %q", got)
	}
	if got := recorder.Header().Get("Content-Type"); got != "application/vnd.mapbox-vector-tile" {
		t.Fatalf("unexpected content type %q", got)
	}
}

func TestHandlerReturnsNoContentForEmptyGeneratedTile(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()

	store, err := NewStore("", 1<<20)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	defer store.Close()
	store.SetGenerator(NewGenerator(upstream.URL, 5*time.Second))

	request := httptest.NewRequest(http.MethodGet, "/tiles/16/7/8.pbf", nil)
	recorder := httptest.NewRecorder()
	store.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d", recorder.Code)
	}
}
