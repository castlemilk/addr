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

func TestReverseGeocodeReturnsNearestAddress(t *testing.T) {
	// Go 1.27 structured test attribute
	t.Attr("tier", "integration")
	t.Attr("db", "sqlite")

	dbPath := filepath.Join("..", "..", "data", "australia_geo.db")
	store, err := geo.NewStore(dbPath)
	if err != nil {
		t.Skipf("skipping integration test: database not found at %s: %v", dbPath, err)
	}
	defer store.Close()

	// 27 Cambridge Road pin: a dense area where the old LIMIT 50 truncated
	// candidates before distance ordering and returned arbitrary addresses.
	res, err := store.ReverseGeocode(context.Background(), -37.79044, 145.31252, 250.0)
	if err != nil {
		t.Fatalf("ReverseGeocode failed: %v", err)
	}
	if res.Address == nil {
		t.Fatalf("expected an address match, got nil")
	}
	if res.Address.ID != 29790 {
		t.Fatalf("expected address id 29790 (27 Cambridge Road), got %d (%s)", res.Address.ID, res.Address.FullAddress)
	}
}

func TestReverseGeocodeDoesNotJoinDistantPoi(t *testing.T) {
	// Go 1.27 structured test attribute
	t.Attr("tier", "integration")
	t.Attr("db", "sqlite")

	dbPath := filepath.Join("..", "..", "data", "australia_geo.db")
	store, err := geo.NewStore(dbPath)
	if err != nil {
		t.Skipf("skipping integration test: database not found at %s: %v", dbPath, err)
	}
	defer store.Close()

	// Pin sits on 27 Cambridge Road's point; LKPT Fitness (29 Cambridge Rd)
	// is ~42m away and must not headline the result as poi_and_address.
	res, err := store.ReverseGeocode(context.Background(), -37.79044, 145.31252, 250.0)
	if err != nil {
		t.Fatalf("ReverseGeocode failed: %v", err)
	}
	if res.MatchType != "address_only" {
		t.Fatalf("expected match type address_only, got %q", res.MatchType)
	}
	if res.Place != nil {
		t.Fatalf("expected no headline POI, got %s", res.Place.Name)
	}

	// Tapping the POI itself joins it with its own street number (29).
	poiRes, err := store.ReverseGeocode(context.Background(), -37.79051399230957, 145.31298828125, 250.0)
	if err != nil {
		t.Fatalf("ReverseGeocode failed: %v", err)
	}
	if poiRes.MatchType != "poi_and_address" {
		t.Fatalf("expected match type poi_and_address, got %q", poiRes.MatchType)
	}
	if poiRes.Place == nil || poiRes.Place.Name != "LKPT Fitness" {
		t.Fatalf("expected LKPT Fitness headline POI, got %+v", poiRes.Place)
	}
	if poiRes.Address == nil || poiRes.Address.Number == nil || *poiRes.Address.Number != "29" {
		t.Fatalf("expected a 29 Cambridge Road address, got %+v", poiRes.Address)
	}
}
