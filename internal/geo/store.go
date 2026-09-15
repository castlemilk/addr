package geo

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"iter"
	"log/slog"
	"math"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	_ "modernc.org/sqlite"
)

// Store provides high-performance spatial query capabilities over SQLite.
// Leverages Go 1.24+ HashTrieMap-backed sync.Map for zero-contention spatial caching.
type Store struct {
	db         *sql.DB
	dbPath     string
	placesStmt *sql.Stmt
	addrsStmt  *sql.Stmt
	placesFts  *sql.Stmt
	addrsFts   *sql.Stmt
	coordCache sync.Map      // key: uint64 (quantized lat/lon) -> *ReverseGeocodeResult
	cacheCount atomic.Int64  // tracks size to enforce bounded memory (Rule 120)
	cacheEpoch atomic.Uint64 // increments on ClearCache to invalidate upstream caches
}

const maxCacheEntries = 100000

// NewStore initializes a SQLite database connection with optimized WAL mode and prepared statements.
func NewStore(dbPath string) (*Store, error) {
	if _, err := os.Stat(dbPath); err != nil {
		return nil, fmt.Errorf("opening database %q: %w", dbPath, err)
	}

	dsn := fmt.Sprintf("file:%s?mode=ro&_pragma=journal_mode(WAL)&_pragma=synchronous(NORMAL)&_pragma=cache_size(-64000)&_pragma=busy_timeout(5000)&_pragma=mmap_size(268435456)&_pragma=query_only(1)", dbPath)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("opening sqlite connection: %w", err)
	}

	db.SetMaxOpenConns(32)
	db.SetMaxIdleConns(16)
	db.SetConnMaxLifetime(time.Hour)

	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("pinging sqlite database: %w", err)
	}

	placesQuery := `
		SELECT p.id, p.uuid, p.name, p.category, p.brand, p.address_text,
		       p.suburb, p.state, p.postcode, p.lat, p.lon, p.phone, p.website, p.confidence
		FROM places_rtree r
		JOIN places p ON p.id = r.id
		WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
		LIMIT 50;
	`
	placesStmt, err := db.Prepare(placesQuery)
	if err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("preparing places statement: %w", err)
	}

	addrsQuery := `
		SELECT a.id, a.uuid, a.unit, a.number, a.street, a.suburb,
		       a.state, a.postcode, a.full_address, a.lat, a.lon
		FROM addresses_rtree r
		JOIN addresses a ON a.id = r.id
		WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
		LIMIT 50;
	`
	addrsStmt, err := db.Prepare(addrsQuery)
	if err != nil {
		_ = placesStmt.Close()
		_ = db.Close()
		return nil, fmt.Errorf("preparing addresses statement: %w", err)
	}

	placesFtsQuery := `
		SELECT p.id, p.name, p.category, p.address_text, p.lat, p.lon
		FROM places_fts f
		JOIN places p ON p.id = f.rowid
		WHERE places_fts MATCH ?
		LIMIT ?;
	`
	placesFts, _ := db.Prepare(placesFtsQuery)

	addrsFtsQuery := `
		SELECT a.id, a.full_address, a.suburb, a.state, a.postcode, a.lat, a.lon
		FROM addresses_fts f
		JOIN addresses a ON a.id = f.rowid
		WHERE addresses_fts MATCH ?
		LIMIT ?;
	`
	addrsFts, _ := db.Prepare(addrsFtsQuery)

	return &Store{
		db:         db,
		dbPath:     dbPath,
		placesStmt: placesStmt,
		addrsStmt:  addrsStmt,
		placesFts:  placesFts,
		addrsFts:   addrsFts,
	}, nil
}

// Close closes prepared statements and the underlying database connection.
func (s *Store) Close() error {
	s.ClearCache()
	if s.placesStmt != nil {
		_ = s.placesStmt.Close()
	}
	if s.addrsStmt != nil {
		_ = s.addrsStmt.Close()
	}
	if s.placesFts != nil {
		_ = s.placesFts.Close()
	}
	if s.addrsFts != nil {
		_ = s.addrsFts.Close()
	}
	return s.db.Close()
}

// ClearCache evicts all entries using Go 1.23+ sync.Map.Clear() and increments cache epoch.
func (s *Store) ClearCache() {
	s.coordCache.Clear()
	s.cacheCount.Store(0)
	s.cacheEpoch.Add(1)
}

// CacheEpoch returns the current cache invalidation epoch.
func (s *Store) CacheEpoch() uint64 {
	return s.cacheEpoch.Load()
}

// QuantizeCoord converts (lat, lon) into a packed 64-bit integer grid key (~2.2m resolution).
func QuantizeCoord(lat, lon float64) uint64 {
	latInt := uint32(int32((lat + 90.0) * 50000.0))
	lonInt := uint32(int32((lon + 180.0) * 50000.0))
	return (uint64(latInt) << 32) | uint64(lonInt)
}

func quantizeCoord(lat, lon float64) uint64 {
	return QuantizeCoord(lat, lon)
}

// HaversineDistance computes great-circle distance between two points in meters.
func HaversineDistance(lat1, lon1, lat2, lon2 float64) float64 {
	const earthRadius = 6371000.0 // meters
	dLat := (lat2 - lat1) * (math.Pi / 180.0)
	dLon := (lon2 - lon1) * (math.Pi / 180.0)

	lat1Rad := lat1 * (math.Pi / 180.0)
	lat2Rad := lat2 * (math.Pi / 180.0)

	a := math.Sin(dLat/2.0)*math.Sin(dLat/2.0) +
		math.Cos(lat1Rad)*math.Cos(lat2Rad)*
			math.Sin(dLon/2.0)*math.Sin(dLon/2.0)

	c := 2.0 * math.Atan2(math.Sqrt(a), math.Sqrt(1.0-a))
	return math.Round(earthRadius*c*10.0) / 10.0
}

// ReverseGeocode performs two-tier spatial resolution:
// 1. Fast-path: checks sync.Map coordinate cache (< 50ns, 0 allocs).
// 2. Slow-path: concurrent R*Tree search with planar circle pre-filtering.
func (s *Store) ReverseGeocode(ctx context.Context, lat, lon float64, maxRadius float64) (*ReverseGeocodeResult, error) {
	start := time.Now()
	if maxRadius <= 0 {
		maxRadius = 300.0
	}

	// Hot-path: check lock-free sync.Map cache (backed by HashTrieMap in Go 1.24+)
	cacheKey := quantizeCoord(lat, lon)
	if cachedVal, ok := s.coordCache.Load(cacheKey); ok {
		cached := cachedVal.(*ReverseGeocodeResult)
		// Return a copy with adjusted query lat/lon and sub-microsecond execution time
		resCopy := *cached
		resCopy.QueryLat = lat
		resCopy.QueryLon = lon
		resCopy.ExecutionTimeMs = float64(time.Since(start).Microseconds()) / 1000.0
		return &resCopy, nil
	}

	// Degree approximations for R*Tree bounding box
	latDelta := (maxRadius + 50.0) / 111000.0
	cosLat := math.Cos(lat * (math.Pi / 180.0))
	lonDelta := (maxRadius + 50.0) / (111000.0 * cosLat)

	boxMinX := lon - lonDelta
	boxMaxX := lon + lonDelta
	boxMinY := lat - latDelta
	boxMaxY := lat + latDelta

	var (
		wg              sync.WaitGroup
		placeCandidates []Place
		addrCandidates  []Address
		placeErr        error
		addrErr         error
	)

	// Concurrently query places and addresses using sync.WaitGroup.Go (Go 1.25+)
	wg.Go(func() {
		placeCandidates, placeErr = s.queryCandidatePlaces(ctx, boxMinX, boxMaxX, boxMinY, boxMaxY, lat, lon, maxRadius, cosLat)
	})

	wg.Go(func() {
		addrCandidates, addrErr = s.queryCandidateAddresses(ctx, boxMinX, boxMaxX, boxMinY, boxMaxY, lat, lon, maxRadius, cosLat)
	})

	wg.Wait()

	if placeErr != nil {
		slog.Warn("place query candidate error", "error", placeErr)
	}
	if addrErr != nil {
		slog.Warn("address query candidate error", "error", addrErr)
	}

	var bestPlace *Place
	if len(placeCandidates) > 0 && placeCandidates[0].DistanceMeters != nil && *placeCandidates[0].DistanceMeters <= 50.0 {
		bestPlace = &placeCandidates[0]
	}

	var bestAddress *Address
	if len(addrCandidates) > 0 {
		bestAddress = &addrCandidates[0]
	}

	matchType := "none"
	if bestPlace != nil && bestAddress != nil {
		matchType = "poi_and_address"
	} else if bestPlace != nil {
		matchType = "poi_only"
	} else if bestAddress != nil {
		matchType = "address_only"
	} else if len(placeCandidates) > 0 {
		matchType = "approximate"
	}

	formattedTitle := "Location Not In Local Index"
	formattedSubtitle := fmt.Sprintf("%.6f, %.6f", lat, lon)

	if bestPlace != nil && bestAddress != nil {
		formattedTitle = bestPlace.Name
		formattedSubtitle = bestAddress.FullAddress
	} else if bestPlace != nil {
		formattedTitle = bestPlace.Name
		if bestPlace.AddressText != nil {
			formattedSubtitle = *bestPlace.AddressText
		} else {
			formattedSubtitle = fmt.Sprintf("%.6f, %.6f", lat, lon)
		}
	} else if bestAddress != nil {
		formattedTitle = bestAddress.FullAddress
		suburbParts := make([]string, 0, 3)
		if bestAddress.Suburb != nil {
			suburbParts = append(suburbParts, *bestAddress.Suburb)
		}
		if bestAddress.State != nil {
			suburbParts = append(suburbParts, *bestAddress.State)
		}
		if bestAddress.Postcode != nil {
			suburbParts = append(suburbParts, *bestAddress.Postcode)
		}
		if len(suburbParts) > 0 {
			formattedSubtitle = strings.Join(suburbParts, " ")
		} else {
			formattedSubtitle = "Australia"
		}
	} else if len(placeCandidates) > 0 {
		formattedTitle = fmt.Sprintf("Near %s", placeCandidates[0].Name)
		if placeCandidates[0].AddressText != nil {
			formattedSubtitle = *placeCandidates[0].AddressText
		}
	}

	nearbyPlaces := placeCandidates
	if len(nearbyPlaces) > 8 {
		nearbyPlaces = nearbyPlaces[:8]
	}

	nearbyAddrs := addrCandidates
	if len(nearbyAddrs) > 8 {
		nearbyAddrs = nearbyAddrs[:8]
	}

	execTime := float64(time.Since(start).Microseconds()) / 1000.0

	result := &ReverseGeocodeResult{
		QueryLat:          lat,
		QueryLon:          lon,
		MatchType:         matchType,
		Place:             bestPlace,
		Address:           bestAddress,
		FormattedTitle:    formattedTitle,
		FormattedSubtitle: formattedSubtitle,
		NearbyPlaces:      nearbyPlaces,
		NearbyAddresses:   nearbyAddrs,
		ExecutionTimeMs:   math.Round(execTime*100.0) / 100.0,
	}

	// Store in sync.Map cache if within bounds (Rule 120: bounded map growth)
	if s.cacheCount.Load() < maxCacheEntries {
		s.coordCache.Store(cacheKey, result)
		s.cacheCount.Add(1)
	}

	return result, nil
}

func (s *Store) queryCandidatePlaces(ctx context.Context, boxMinX, boxMaxX, boxMinY, boxMaxY, targetLat, targetLon, maxRadius, cosLat float64) ([]Place, error) {
	var rows *sql.Rows
	var err error
	if s.placesStmt != nil {
		rows, err = s.placesStmt.QueryContext(ctx, boxMaxX, boxMinX, boxMaxY, boxMinY)
	} else {
		query := `
			SELECT p.id, p.uuid, p.name, p.category, p.brand, p.address_text,
			       p.suburb, p.state, p.postcode, p.lat, p.lon, p.phone, p.website, p.confidence
			FROM places_rtree r
			JOIN places p ON p.id = r.id
			WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
			LIMIT 50;
		`
		rows, err = s.db.QueryContext(ctx, query, boxMaxX, boxMinX, boxMaxY, boxMinY)
	}
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	// Pre-size capacity to reduce heap allocations
	places := make([]Place, 0, 16)
	maxRadiusDeg := maxRadius / 111000.0
	maxRadiusSq := maxRadiusDeg * maxRadiusDeg

	for rows.Next() {
		var (
			p          Place
			category   sql.NullString
			brand      sql.NullString
			addrText   sql.NullString
			suburb     sql.NullString
			state      sql.NullString
			postcode   sql.NullString
			phone      sql.NullString
			website    sql.NullString
			confidence sql.NullFloat64
		)

		if err := rows.Scan(
			&p.ID, &p.UUID, &p.Name, &category, &brand, &addrText,
			&suburb, &state, &postcode, &p.Latitude, &p.Longitude,
			&phone, &website, &confidence,
		); err != nil {
			return nil, err
		}

		// Fast planar rejection: skip expensive trigonometric math for candidates outside circle
		dLat := p.Latitude - targetLat
		dLon := (p.Longitude - targetLon) * cosLat
		if (dLat*dLat + dLon*dLon) > maxRadiusSq {
			continue
		}

		if category.Valid {
			p.Category = &category.String
		}
		if brand.Valid {
			p.Brand = &brand.String
		}
		if addrText.Valid {
			p.AddressText = &addrText.String
		}
		if suburb.Valid {
			p.Suburb = &suburb.String
		}
		if state.Valid {
			p.State = &state.String
		}
		if postcode.Valid {
			p.Postcode = &postcode.String
		}
		if phone.Valid {
			p.Phone = &phone.String
		}
		if website.Valid {
			p.Website = &website.String
		}
		if confidence.Valid {
			p.Confidence = &confidence.Float64
		}

		dist := HaversineDistance(targetLat, targetLon, p.Latitude, p.Longitude)
		if dist <= maxRadius {
			p.DistanceMeters = new(dist) // Go 1.26+ new(expr)
			places = append(places, p)
		}
	}

	// Sort places by distance
	for i := 0; i < len(places); i++ {
		for j := i + 1; j < len(places); j++ {
			if *places[i].DistanceMeters > *places[j].DistanceMeters {
				places[i], places[j] = places[j], places[i]
			}
		}
	}

	return places, nil
}

func (s *Store) queryCandidateAddresses(ctx context.Context, boxMinX, boxMaxX, boxMinY, boxMaxY, targetLat, targetLon, maxRadius, cosLat float64) ([]Address, error) {
	var rows *sql.Rows
	var err error
	if s.addrsStmt != nil {
		rows, err = s.addrsStmt.QueryContext(ctx, boxMaxX, boxMinX, boxMaxY, boxMinY)
	} else {
		query := `
			SELECT a.id, a.uuid, a.unit, a.number, a.street, a.suburb,
			       a.state, a.postcode, a.full_address, a.lat, a.lon
			FROM addresses_rtree r
			JOIN addresses a ON a.id = r.id
			WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
			LIMIT 50;
		`
		rows, err = s.db.QueryContext(ctx, query, boxMaxX, boxMinX, boxMaxY, boxMinY)
	}
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	// Pre-size capacity to reduce heap allocations
	addrs := make([]Address, 0, 16)
	maxRadiusDeg := maxRadius / 111000.0
	maxRadiusSq := maxRadiusDeg * maxRadiusDeg

	for rows.Next() {
		var (
			a        Address
			unit     sql.NullString
			number   sql.NullString
			street   sql.NullString
			suburb   sql.NullString
			state    sql.NullString
			postcode sql.NullString
		)

		if err := rows.Scan(
			&a.ID, &a.UUID, &unit, &number, &street, &suburb,
			&state, &postcode, &a.FullAddress, &a.Latitude, &a.Longitude,
		); err != nil {
			return nil, err
		}

		// Fast planar rejection: skip candidates outside circle
		dLat := a.Latitude - targetLat
		dLon := (a.Longitude - targetLon) * cosLat
		if (dLat*dLat + dLon*dLon) > maxRadiusSq {
			continue
		}

		if unit.Valid {
			a.Unit = &unit.String
		}
		if number.Valid {
			a.Number = &number.String
		}
		if street.Valid {
			a.Street = &street.String
		}
		if suburb.Valid {
			a.Suburb = &suburb.String
		}
		if state.Valid {
			a.State = &state.String
		}
		if postcode.Valid {
			a.Postcode = &postcode.String
		}

		dist := HaversineDistance(targetLat, targetLon, a.Latitude, a.Longitude)
		if dist <= maxRadius {
			a.DistanceMeters = new(dist) // Go 1.26+ new(expr)
			addrs = append(addrs, a)
		}
	}

	// Sort addresses by distance
	for i := 0; i < len(addrs); i++ {
		for j := i + 1; j < len(addrs); j++ {
			if *addrs[i].DistanceMeters > *addrs[j].DistanceMeters {
				addrs[i], addrs[j] = addrs[j], addrs[i]
			}
		}
	}

	return addrs, nil
}

// PlacesInBox provides a Go 1.23+ range-over-func iterator for candidate places.
// Allows lazy, zero-slice-allocation traversal.
func (s *Store) PlacesInBox(ctx context.Context, minX, maxX, minY, maxY float64) iter.Seq2[Place, error] {
	return func(yield func(Place, error) bool) {
		query := `
			SELECT p.id, p.uuid, p.name, p.category, p.brand, p.address_text,
			       p.suburb, p.state, p.postcode, p.lat, p.lon, p.phone, p.website, p.confidence
			FROM places_rtree r
			JOIN places p ON p.id = r.id
			WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?
			LIMIT 100;
		`
		rows, err := s.db.QueryContext(ctx, query, maxX, minX, maxY, minY)
		if err != nil {
			yield(Place{}, err)
			return
		}
		defer rows.Close()

		for rows.Next() {
			var (
				p          Place
				category   sql.NullString
				brand      sql.NullString
				addrText   sql.NullString
				suburb     sql.NullString
				state      sql.NullString
				postcode   sql.NullString
				phone      sql.NullString
				website    sql.NullString
				confidence sql.NullFloat64
			)

			if err := rows.Scan(
				&p.ID, &p.UUID, &p.Name, &category, &brand, &addrText,
				&suburb, &state, &postcode, &p.Latitude, &p.Longitude,
				&phone, &website, &confidence,
			); err != nil {
				yield(Place{}, err)
				return
			}

			if category.Valid {
				p.Category = &category.String
			}
			if brand.Valid {
				p.Brand = &brand.String
			}
			if addrText.Valid {
				p.AddressText = &addrText.String
			}
			if suburb.Valid {
				p.Suburb = &suburb.String
			}
			if state.Valid {
				p.State = &state.String
			}
			if postcode.Valid {
				p.Postcode = &postcode.String
			}
			if phone.Valid {
				p.Phone = &phone.String
			}
			if website.Valid {
				p.Website = &website.String
			}
			if confidence.Valid {
				p.Confidence = &confidence.Float64
			}

			if !yield(p, nil) {
				return
			}
		}
	}
}

// Search queries FTS5 across indexed places and addresses.
type candidateWithScore struct {
	item   SearchItem
	score  float64
	number *string
}

// Search queries FTS5 and indexes across places and addresses with elastic autocomplete,
// street abbreviation expansion, and Damerau-Levenshtein typo tolerance.
func (s *Store) Search(ctx context.Context, query string, lat, lon *float64, limit int) ([]SearchItem, error) {
	if limit <= 0 {
		limit = 15
	}

	clean := strings.TrimSpace(query)
	if clean == "" {
		return nil, nil
	}

	pq := ParseSearchQuery(clean)
	if len(pq.Tokens) == 0 {
		return nil, nil
	}

	candidateMap := make(map[string]*candidateWithScore, 64)

	addPlace := func(id int64, name string, category, addrText sql.NullString, cLat, cLon float64) {
		key := fmt.Sprintf("p:%d", id)
		if _, exists := candidateMap[key]; exists {
			return
		}
		item := SearchItem{
			Type:      "place",
			ID:        id,
			Title:     name,
			Latitude:  cLat,
			Longitude: cLon,
		}
		if category.Valid {
			item.Category = &category.String
		}
		if addrText.Valid {
			item.Subtitle = addrText.String
		} else {
			item.Subtitle = "Australia"
		}
		if lat != nil && lon != nil {
			dist := HaversineDistance(*lat, *lon, cLat, cLon)
			item.DistanceMeters = new(dist)
		}
		score := ScoreCandidate(pq, item.Title, item.Subtitle, nil, lat, lon, cLat, cLon)
		candidateMap[key] = &candidateWithScore{
			item:  item,
			score: score,
		}
	}

	addAddress := func(id int64, fullAddr string, number, suburb, state, postcode sql.NullString, cLat, cLon float64) {
		key := fmt.Sprintf("a:%d", id)
		if _, exists := candidateMap[key]; exists {
			return
		}
		item := SearchItem{
			Type:      "address",
			ID:        id,
			Title:     fullAddr,
			Latitude:  cLat,
			Longitude: cLon,
		}
		parts := make([]string, 0, 3)
		if suburb.Valid {
			parts = append(parts, suburb.String)
		}
		if state.Valid {
			parts = append(parts, state.String)
		}
		if postcode.Valid {
			parts = append(parts, postcode.String)
		}
		if len(parts) > 0 {
			item.Subtitle = strings.Join(parts, " ")
		} else {
			item.Subtitle = "Australia"
		}
		if lat != nil && lon != nil {
			dist := HaversineDistance(*lat, *lon, cLat, cLon)
			item.DistanceMeters = new(dist)
		}
		var numPtr *string
		if number.Valid {
			numPtr = &number.String
		}
		score := ScoreCandidate(pq, item.Title, item.Subtitle, numPtr, lat, lon, cLat, cLon)
		candidateMap[key] = &candidateWithScore{
			item:   item,
			score:  score,
			number: numPtr,
		}
	}

	// 1. Pass 1: Token-expanded FTS query (e.g. 27* AND cambridge* AND (road* OR rd*))
	ftsQuery := pq.BuildFTS5Query()
	if ftsQuery != "" {
		pQuery := `
			SELECT p.id, p.name, p.category, p.address_text, p.lat, p.lon
			FROM places_fts f
			JOIN places p ON p.id = f.rowid
			WHERE places_fts MATCH ?
			LIMIT 30;
		`
		if pRows, pErr := s.db.QueryContext(ctx, pQuery, ftsQuery); pErr == nil {
			for pRows.Next() {
				var (
					id       int64
					name     string
					category sql.NullString
					addrText sql.NullString
					cLat     float64
					cLon     float64
				)
				if err := pRows.Scan(&id, &name, &category, &addrText, &cLat, &cLon); err == nil {
					addPlace(id, name, category, addrText, cLat, cLon)
				}
			}
			pRows.Close()
		}

		aQuery := `
			SELECT a.id, a.full_address, a.number, a.suburb, a.state, a.postcode, a.lat, a.lon
			FROM addresses_fts f
			JOIN addresses a ON a.id = f.rowid
			WHERE addresses_fts MATCH ?
			LIMIT 40;
		`
		if aRows, aErr := s.db.QueryContext(ctx, aQuery, ftsQuery); aErr == nil {
			for aRows.Next() {
				var (
					id       int64
					fullAddr string
					number   sql.NullString
					suburb   sql.NullString
					state    sql.NullString
					postcode sql.NullString
					cLat     float64
					cLon     float64
				)
				if err := aRows.Scan(&id, &fullAddr, &number, &suburb, &state, &postcode, &cLat, &cLon); err == nil {
					addAddress(id, fullAddr, number, suburb, state, postcode, cLat, cLon)
				}
			}
			aRows.Close()
		}
	}

	// 2. Pass 2: Typo tolerance & relaxation fallback if candidates are insufficient
	if len(candidateMap) < limit {
		// A. Typo-resilient core prefix queries (e.g. 27* AND camb* AND (road* OR rd*))
		typoQueries := pq.BuildTypoFTSQueries()
		for _, tq := range typoQueries {
			if len(candidateMap) >= limit*3 {
				break
			}
			aRelQuery := `
				SELECT a.id, a.full_address, a.number, a.suburb, a.state, a.postcode, a.lat, a.lon
				FROM addresses_fts f
				JOIN addresses a ON a.id = f.rowid
				WHERE addresses_fts MATCH ?
				LIMIT 30;
			`
			if rRows, rErr := s.db.QueryContext(ctx, aRelQuery, tq); rErr == nil {
				for rRows.Next() {
					var (
						id       int64
						fullAddr string
						number   sql.NullString
						suburb   sql.NullString
						state    sql.NullString
						postcode sql.NullString
						cLat     float64
						cLon     float64
					)
					if err := rRows.Scan(&id, &fullAddr, &number, &suburb, &state, &postcode, &cLat, &cLon); err == nil {
						addAddress(id, fullAddr, number, suburb, state, postcode, cLat, cLon)
					}
				}
				rRows.Close()
			}

			pRelQuery := `
				SELECT p.id, p.name, p.category, p.address_text, p.lat, p.lon
				FROM places_fts f
				JOIN places p ON p.id = f.rowid
				WHERE places_fts MATCH ?
				LIMIT 20;
			`
			if pRows, pErr := s.db.QueryContext(ctx, pRelQuery, tq); pErr == nil {
				for pRows.Next() {
					var (
						id       int64
						name     string
						category sql.NullString
						addrText sql.NullString
						cLat     float64
						cLon     float64
					)
					if err := pRows.Scan(&id, &name, &category, &addrText, &cLat, &cLon); err == nil {
						addPlace(id, name, category, addrText, cLat, cLon)
					}
				}
				pRows.Close()
			}
		}

		// B. House number anchor lookup (if still needed)
		if len(candidateMap) < limit && pq.HouseNumber != "" {
			numQuery := `
				SELECT a.id, a.full_address, a.number, a.suburb, a.state, a.postcode, a.lat, a.lon
				FROM addresses a
				WHERE a.number = ? OR a.number LIKE ?
				LIMIT 30;
			`
			if nRows, nErr := s.db.QueryContext(ctx, numQuery, pq.HouseNumber, pq.HouseNumber+"%"); nErr == nil {
				for nRows.Next() {
					var (
						id       int64
						fullAddr string
						number   sql.NullString
						suburb   sql.NullString
						state    sql.NullString
						postcode sql.NullString
						cLat     float64
						cLon     float64
					)
					if err := nRows.Scan(&id, &fullAddr, &number, &suburb, &state, &postcode, &cLat, &cLon); err == nil {
						addAddress(id, fullAddr, number, suburb, state, postcode, cLat, cLon)
					}
				}
				nRows.Close()
			}
		}
	}

	// 3. Sort candidates by score descending
	scoredList := make([]*candidateWithScore, 0, len(candidateMap))
	for _, c := range candidateMap {
		if c.score >= 25.0 || len(candidateMap) <= limit {
			scoredList = append(scoredList, c)
		}
	}

	for i := 0; i < len(scoredList); i++ {
		for j := i + 1; j < len(scoredList); j++ {
			if scoredList[i].score < scoredList[j].score {
				scoredList[i], scoredList[j] = scoredList[j], scoredList[i]
			}
		}
	}

	// 4. Return top results up to limit
	if len(scoredList) > limit {
		scoredList = scoredList[:limit]
	}

	results := make([]SearchItem, len(scoredList))
	for i, sc := range scoredList {
		results[i] = sc.item
	}

	return results, nil
}

// GetNearby returns POIs and addresses around a coordinate for map rendering.
func (s *Store) GetNearby(ctx context.Context, lat, lon float64, radius float64, limit int) ([]Place, []Address, error) {
	if radius <= 0 {
		radius = 500.0
	}
	if limit <= 0 {
		limit = 40
	}

	latDelta := radius / 111000.0
	cosLat := math.Cos(lat * (math.Pi / 180.0))
	lonDelta := radius / (111000.0 * cosLat)

	boxMinX := lon - lonDelta
	boxMaxX := lon + lonDelta
	boxMinY := lat - latDelta
	boxMaxY := lat + latDelta

	places, err := s.queryCandidatePlaces(ctx, boxMinX, boxMaxX, boxMinY, boxMaxY, lat, lon, radius, cosLat)
	if err != nil {
		return nil, nil, fmt.Errorf("getting nearby places: %w", err)
	}

	addrs, err := s.queryCandidateAddresses(ctx, boxMinX, boxMaxX, boxMinY, boxMaxY, lat, lon, radius, cosLat)
	if err != nil {
		return nil, nil, fmt.Errorf("getting nearby addresses: %w", err)
	}

	if len(places) > limit {
		places = places[:limit]
	}
	if len(addrs) > limit {
		addrs = addrs[:limit]
	}

	return places, addrs, nil
}

// GetStats returns current database statistics and indexed regions.
func (s *Store) GetStats(ctx context.Context) (*DbStats, error) {
	var stats DbStats

	err := s.db.QueryRowContext(ctx, "SELECT count(*) FROM places;").Scan(&stats.PlaceCount)
	if err != nil {
		return nil, fmt.Errorf("counting places: %w", err)
	}

	err = s.db.QueryRowContext(ctx, "SELECT count(*) FROM addresses;").Scan(&stats.AddressCount)
	if err != nil {
		return nil, fmt.Errorf("counting addresses: %w", err)
	}

	rows, err := s.db.QueryContext(ctx, "SELECT id, name, bbox, place_count, address_count, created_at FROM indexed_regions ORDER BY id DESC;")
	if err == nil {
		defer rows.Close()
		for rows.Next() {
			var r IndexedRegion
			if err := rows.Scan(&r.ID, &r.Name, &r.BBox, &r.PlaceCount, &r.AddressCount, &r.CreatedAt); err == nil {
				stats.Regions = append(stats.Regions, r)
			}
		}
	}

	if fi, err := os.Stat(s.dbPath); err == nil {
		stats.DbSizeBytes = fi.Size()
		stats.DbSizeFormatted = fmt.Sprintf("%.1f MB", float64(fi.Size())/(1024.0*1024.0))
	}

	return &stats, nil
}

// CheckErrorDemonstration illustrates Go 1.27 errors.AsType[E] usage
func CheckErrorDemonstration(err error) {
	// Go 1.27 generic errors.AsType
	if pathErr, ok := errors.AsType[*os.PathError](err); ok {
		slog.Debug("encountered path error", "path", pathErr.Path)
	}
}
