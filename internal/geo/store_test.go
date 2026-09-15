package geo_test

import (
	"context"
	"path/filepath"
	"testing"

	"github.com/benebsworth/addr/internal/geo"
)

func TestHaversineDistance(t *testing.T) {
	// Go 1.27 structured test attribute
	t.Attr("tier", "unit")
	t.Attr("component", "spatial")

	tests := []struct {
		name       string
		lat1, lon1 float64
		lat2, lon2 float64
		expected   float64
		tolerance  float64
	}{
		{
			name:      "same point",
			lat1:      -33.8848,
			lon1:      151.1960,
			lat2:      -33.8848,
			lon2:      151.1960,
			expected:  0.0,
			tolerance: 0.1,
		},
		{
			name:      "sydney broadway to martin place",
			lat1:      -33.8848,
			lon1:      151.1960,
			lat2:      -33.8688,
			lon2:      151.2093,
			expected:  2160.0,
			tolerance: 50.0,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := geo.HaversineDistance(tt.lat1, tt.lon1, tt.lat2, tt.lon2)
			diff := got - tt.expected
			if diff < 0 {
				diff = -diff
			}
			if diff > tt.tolerance {
				t.Errorf("HaversineDistance() = %v, expected %v (tolerance %v)", got, tt.expected, tt.tolerance)
			}
		})
	}
}

func TestStoreQueries(t *testing.T) {
	// Go 1.27 structured test attribute
	t.Attr("tier", "integration")
	t.Attr("db", "sqlite")

	dbPath := filepath.Join("..", "..", "data", "australia_geo.db")
	store, err := geo.NewStore(dbPath)
	if err != nil {
		t.Skipf("skipping integration test: database not found at %s: %v", dbPath, err)
	}
	defer store.Close()

	ctx := context.Background()

	t.Run("GetStats", func(t *testing.T) {
		stats, err := store.GetStats(ctx)
		if err != nil {
			t.Fatalf("GetStats failed: %v", err)
		}
		if stats.PlaceCount == 0 {
			t.Errorf("expected > 0 places, got %d", stats.PlaceCount)
		}
		if stats.AddressCount == 0 {
			t.Errorf("expected > 0 addresses, got %d", stats.AddressCount)
		}
	})

	t.Run("ReverseGeocode Broadway", func(t *testing.T) {
		res, err := store.ReverseGeocode(ctx, -33.8848, 151.1960, 250.0)
		if err != nil {
			t.Fatalf("ReverseGeocode failed: %v", err)
		}
		if res.MatchType == "none" {
			t.Errorf("expected valid match type, got %s", res.MatchType)
		}
		if res.Place == nil {
			t.Errorf("expected POI match, got nil")
		}
	})

	t.Run("Search Coffee", func(t *testing.T) {
		results, err := store.Search(ctx, "coffee", nil, nil, 5)
		if err != nil {
			t.Fatalf("Search failed: %v", err)
		}
		if len(results) == 0 {
			t.Errorf("expected search results for 'coffee', got 0")
		}
	})
}
