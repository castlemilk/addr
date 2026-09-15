package geo_test

import (
	"context"
	"math"
	"path/filepath"
	"testing"

	"github.com/benebsworth/addr/internal/geo"
)

func BenchmarkHaversine(b *testing.B) {
	lat1, lon1 := -33.8848, 151.1960
	lat2, lon2 := -33.8688, 151.2093

	// Go 1.24+ b.Loop()
	for b.Loop() {
		_ = geo.HaversineDistance(lat1, lon1, lat2, lon2)
	}
}

func BenchmarkReverseGeocode(b *testing.B) {
	dbPath := filepath.Join("..", "..", "data", "australia_geo.db")
	store, err := geo.NewStore(dbPath)
	if err != nil {
		b.Fatalf("failed to open database: %v", err)
	}
	defer store.Close()

	ctx := context.Background()
	lat, lon := -33.8848, 151.1960

	// Go 1.24+ b.Loop()
	for b.Loop() {
		_, err := store.ReverseGeocode(ctx, lat, lon, 250.0)
		if err != nil {
			b.Fatalf("ReverseGeocode failed: %v", err)
		}
	}
}

func BenchmarkReverseGeocode_Parallel(b *testing.B) {
	dbPath := filepath.Join("..", "..", "data", "australia_geo.db")
	store, err := geo.NewStore(dbPath)
	if err != nil {
		b.Fatalf("failed to open database: %v", err)
	}
	defer store.Close()

	ctx := context.Background()

	// Points to simulate concurrent real-world load across Sydney and Melbourne
	points := [][2]float64{
		{-33.8848, 151.1960}, // Sydney Broadway
		{-33.8688, 151.2093}, // Sydney Martin Pl
		{-37.8100, 144.9587}, // Melbourne Queen St
		{-37.8000, 144.9579}, // Melbourne Elizabeth St
	}

	b.RunParallel(func(pb *testing.PB) {
		i := 0
		for pb.Next() {
			pt := points[i%len(points)]
			_, _ = store.ReverseGeocode(ctx, pt[0], pt[1], 250.0)
			i++
		}
	})
}

func BenchmarkSearch(b *testing.B) {
	dbPath := filepath.Join("..", "..", "data", "australia_geo.db")
	store, err := geo.NewStore(dbPath)
	if err != nil {
		b.Fatalf("failed to open database: %v", err)
	}
	defer store.Close()

	ctx := context.Background()
	queries := []string{"coffee", "bar", "broadway", "street"}

	// Go 1.24+ b.Loop()
	i := 0
	for b.Loop() {
		q := queries[i%len(queries)]
		_, _ = store.Search(ctx, q, nil, nil, 10)
		i++
	}
}

// Fast equirectangular distance approximation benchmark
func BenchmarkEquirectangularDistance(b *testing.B) {
	lat1, lon1 := -33.8848, 151.1960
	lat2, lon2 := -33.8688, 151.2093

	for b.Loop() {
		const degToRad = math.Pi / 180.0
		const earthRadius = 6371000.0
		x := (lon2 - lon1) * math.Cos((lat1+lat2)/2.0*degToRad)
		y := lat2 - lat1
		_ = math.Hypot(x, y) * degToRad * earthRadius
	}
}
