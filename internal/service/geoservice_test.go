package service_test

import (
	"context"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	connect "connectrpc.com/connect"
	geov1 "github.com/benebsworth/addr/gen/go/geo/v1"
	"github.com/benebsworth/addr/gen/go/geo/v1/geov1connect"
	"github.com/benebsworth/addr/internal/geo"
	"github.com/benebsworth/addr/internal/service"
)

func setupTestService(tb testing.TB) (*service.GeoServer, *geo.Store) {
	tb.Helper()
	dbPath := filepath.Join("..", "..", "data", "australia_geo.db")
	store, err := geo.NewStore(dbPath)
	if err != nil {
		tb.Fatalf("failed to open store at %s: %v", dbPath, err)
	}
	server := service.NewGeoServer(store)
	return server, store
}

// BenchmarkService_ReverseGeocode_Hot measures cached lookup through the service layer.
func BenchmarkService_ReverseGeocode_Hot(b *testing.B) {
	server, store := setupTestService(b)
	defer store.Close()

	ctx := context.Background()
	req := connect.NewRequest(&geov1.ReverseGeocodeRequest{
		Latitude:  -33.8848,
		Longitude: 151.1960,
	})

	// Warm the cache once
	_, _ = server.ReverseGeocode(ctx, req)

	// Go 1.24+ b.Loop()
	for b.Loop() {
		_, err := server.ReverseGeocode(ctx, req)
		if err != nil {
			b.Fatalf("ReverseGeocode failed: %v", err)
		}
	}
}

// BenchmarkService_ReverseGeocode_Cold measures uncached spatial lookup through the service layer.
func BenchmarkService_ReverseGeocode_Cold(b *testing.B) {
	server, store := setupTestService(b)
	defer store.Close()

	ctx := context.Background()
	req := connect.NewRequest(&geov1.ReverseGeocodeRequest{
		Latitude:  -33.8848,
		Longitude: 151.1960,
	})

	for b.Loop() {
		store.ClearCache() // Evict to force cold database lookup
		_, err := server.ReverseGeocode(ctx, req)
		if err != nil {
			b.Fatalf("ReverseGeocode failed: %v", err)
		}
	}
}

// BenchmarkService_ReverseGeocode_Parallel measures concurrent service lookups across multiple cores.
func BenchmarkService_ReverseGeocode_Parallel(b *testing.B) {
	server, store := setupTestService(b)
	defer store.Close()

	ctx := context.Background()
	points := [][2]float64{
		{-33.8848, 151.1960}, // Broadway
		{-33.8688, 151.2093}, // Martin Place
		{-37.8100, 144.9587}, // Queen St Melbourne
		{-37.8000, 144.9579}, // Elizabeth St Melbourne
	}

	b.RunParallel(func(pb *testing.PB) {
		i := 0
		for pb.Next() {
			pt := points[i%len(points)]
			req := connect.NewRequest(&geov1.ReverseGeocodeRequest{
				Latitude:  pt[0],
				Longitude: pt[1],
			})
			_, _ = server.ReverseGeocode(ctx, req)
			i++
		}
	})
}

// BenchmarkService_Search measures search RPC through the service layer.
func BenchmarkService_Search(b *testing.B) {
	server, store := setupTestService(b)
	defer store.Close()

	ctx := context.Background()
	queries := []string{"coffee", "bar", "broadway"}

	i := 0
	for b.Loop() {
		q := queries[i%len(queries)]
		req := connect.NewRequest(&geov1.SearchRequest{
			Query: q,
		})
		_, _ = server.Search(ctx, req)
		i++
	}
}

// BenchmarkService_GetNearby measures fetching nearby POIs and addresses.
func BenchmarkService_GetNearby(b *testing.B) {
	server, store := setupTestService(b)
	defer store.Close()

	ctx := context.Background()
	req := connect.NewRequest(&geov1.GetNearbyRequest{
		Latitude:  -33.8848,
		Longitude: 151.1960,
	})

	for b.Loop() {
		_, _ = server.GetNearby(ctx, req)
	}
}

// BenchmarkService_EndToEnd_HTTP measures complete HTTP request roundtrip through Connect-Go handler.
func BenchmarkService_EndToEnd_HTTP(b *testing.B) {
	server, store := setupTestService(b)
	defer store.Close()

	path, handler := geov1connect.NewGeoServiceHandler(server)
	body := `{"latitude": -33.8848, "longitude": 151.1960}`

	// Warm cache
	wInit := httptest.NewRecorder()
	rInit := httptest.NewRequest("POST", path+"ReverseGeocode", strings.NewReader(body))
	rInit.Header.Set("Content-Type", "application/json")
	handler.ServeHTTP(wInit, rInit)

	for b.Loop() {
		w := httptest.NewRecorder()
		r := httptest.NewRequest("POST", path+"ReverseGeocode", strings.NewReader(body))
		r.Header.Set("Content-Type", "application/json")
		handler.ServeHTTP(w, r)
	}
}
