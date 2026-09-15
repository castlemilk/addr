package geo

// Place represents a Point of Interest (business, landmark, amenity).
type Place struct {
	ID             int64
	UUID           string
	Name           string
	Category       *string
	Brand          *string
	AddressText    *string
	Suburb         *string
	State          *string
	Postcode       *string
	Latitude       float64
	Longitude      float64
	Phone          *string
	Website        *string
	Confidence     *float64
	DistanceMeters *float64
}

// Address represents an official Australian cadastral address (G-NAF).
type Address struct {
	ID             int64
	UUID           string
	Unit           *string
	Number         *string
	Street         *string
	Suburb         *string
	State          *string
	Postcode       *string
	FullAddress    string
	Latitude       float64
	Longitude      float64
	DistanceMeters *float64
}

// ReverseGeocodeResult represents the combined two-tier resolution.
type ReverseGeocodeResult struct {
	QueryLat          float64
	QueryLon          float64
	MatchType         string
	Place             *Place
	Address           *Address
	FormattedTitle    string
	FormattedSubtitle string
	NearbyPlaces      []Place
	NearbyAddresses   []Address
	ExecutionTimeMs   float64
}

// SearchItem represents an item found via full-text search.
type SearchItem struct {
	Type           string // "place" or "address"
	ID             int64
	Title          string
	Subtitle       string
	Category       *string
	Latitude       float64
	Longitude      float64
	DistanceMeters *float64
}

// IndexedRegion represents an indexed regional bounding box in the database.
type IndexedRegion struct {
	ID           int64
	Name         string
	BBox         string
	PlaceCount   int64
	AddressCount int64
	CreatedAt    string
}

// DbStats represents metadata and size statistics.
type DbStats struct {
	PlaceCount      int64
	AddressCount    int64
	Regions         []IndexedRegion
	DbSizeBytes     int64
	DbSizeFormatted string
}
