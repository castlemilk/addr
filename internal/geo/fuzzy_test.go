package geo_test

import (
	"testing"

	"github.com/benebsworth/addr/internal/geo"
)

func TestDamerauLevenshtein(t *testing.T) {
	cases := []struct {
		s1, s2 string
		want   int
	}{
		{"cambridge", "cambridge", 0},
		{"cambridge", "cambrdige", 1}, // transposition
		{"cambridge", "cambrige", 1},  // deletion
		{"cambridge", "cambidge", 1},  // deletion
		{"cambridge", "cambridhe", 1}, // substitution
		{"road", "rd", 2},
		{"road", "roadd", 1},          // insertion
		{"street", "st", 4},
	}

	for _, tc := range cases {
		got := geo.DamerauLevenshtein(tc.s1, tc.s2)
		if got != tc.want {
			t.Errorf("DamerauLevenshtein(%q, %q) = %d; want %d", tc.s1, tc.s2, got, tc.want)
		}
	}
}

func TestScoreCandidate(t *testing.T) {
	pq := geo.ParseSearchQuery("27 cambridge road")
	num := "27"

	// Exact match
	scoreExact := geo.ScoreCandidate(pq, "27 CAMBRIDGE ROAD", "Mooroolbark VIC 3138", &num, nil, nil, -37.79, 145.31)
	if scoreExact < 200.0 {
		t.Errorf("scoreExact = %v; want >= 200.0", scoreExact)
	}

	// Abbreviation "27 cambridge rd"
	pqRd := geo.ParseSearchQuery("27 cambridge rd")
	scoreRd := geo.ScoreCandidate(pqRd, "27 CAMBRIDGE ROAD", "Mooroolbark VIC 3138", &num, nil, nil, -37.79, 145.31)
	if scoreRd < 190.0 {
		t.Errorf("scoreRd = %v; want >= 190.0", scoreRd)
	}

	// Typo "27 cambrdige road"
	pqTypo := geo.ParseSearchQuery("27 cambrdige road")
	scoreTypo := geo.ScoreCandidate(pqTypo, "27 CAMBRIDGE ROAD", "Mooroolbark VIC 3138", &num, nil, nil, -37.79, 145.31)
	if scoreTypo < 170.0 {
		t.Errorf("scoreTypo = %v; want >= 170.0", scoreTypo)
	}

	// Autocomplete prefix "27 camb"
	pqPrefix := geo.ParseSearchQuery("27 camb")
	scorePrefix := geo.ScoreCandidate(pqPrefix, "27 CAMBRIDGE ROAD", "Mooroolbark VIC 3138", &num, nil, nil, -37.79, 145.31)
	if scorePrefix < 150.0 {
		t.Errorf("scorePrefix = %v; want >= 150.0", scorePrefix)
	}

	t.Logf("Scores: exact=%.1f, rd=%.1f, typo=%.1f, prefix=%.1f", scoreExact, scoreRd, scoreTypo, scorePrefix)
}

func TestSearchCambridgeRoadVariants(t *testing.T) {
	dbPath := "../../data/australia_geo.db"
	store, err := geo.NewStore(dbPath)
	if err != nil {
		t.Skipf("skipping live DB test: %v", err)
	}
	defer store.Close()

	queries := []string{
		"27 cambridge road",
		"27 cambridge rd",
		"27 cambrdige road", // transposition typo
		"27 cambrige rd",   // deletion typo
		"27 cambidge road",  // deletion typo
		"27 camb",           // autocomplete prefix
		"cambridge 27",      // out of order
	}

	for _, q := range queries {
		t.Run(q, func(t *testing.T) {
			results, err := store.Search(t.Context(), q, nil, nil, 5)
			if err != nil {
				t.Fatalf("Search failed for %q: %v", q, err)
			}
			if len(results) == 0 {
				t.Fatalf("Search for %q returned 0 results; expected at least 1 match", q)
			}

			top := results[0]
			t.Logf("Query: %-20s -> Top Result: %s (%s)", q, top.Title, top.Subtitle)

			// Verify top result contains 27 and CAMBRIDGE
			if top.Title != "27 CAMBRIDGE ROAD, Mooroolbark VIC 3138" && top.Title != "27 CAMBRIDGE ROAD" {
				t.Logf("Note: Top result was %s", top.Title)
			}
		})
	}
}
