package service

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	connect "connectrpc.com/connect"
	geov1 "github.com/benebsworth/addr/gen/go/geo/v1"
	"github.com/benebsworth/addr/gen/go/geo/v1/geov1connect"
	"github.com/benebsworth/addr/internal/geo"
)

var _ geov1connect.GeoServiceHandler = (*GeoServer)(nil)

const maxProtoCacheEntries = 100000

type reverseCacheKey struct {
	cell   uint64
	radius uint32
}

type cachedProtoEntry struct {
	epoch uint64
	resp  *geov1.ReverseGeocodeResponse
}

// GeoServer implements the connect-go GeoServiceHandler interface.
// Uses Go 1.24+ HashTrieMap-backed sync.Map for zero-allocation response caching.
type GeoServer struct {
	store      *geo.Store
	protoCache sync.Map     // key: reverseCacheKey -> cachedProtoEntry
	cacheCount atomic.Int64 // enforces bounded memory (Rule 120)
}

// NewGeoServer creates a new GeoServer instance.
func NewGeoServer(store *geo.Store) *GeoServer {
	return &GeoServer{store: store}
}

// ClearCache clears the service response cache.
func (s *GeoServer) ClearCache() {
	s.protoCache.Clear()
	s.cacheCount.Store(0)
}

// ReverseGeocode resolves a coordinate to the nearest POI and/or G-NAF address.
func (s *GeoServer) ReverseGeocode(
	ctx context.Context,
	req *connect.Request[geov1.ReverseGeocodeRequest],
) (*connect.Response[geov1.ReverseGeocodeResponse], error) {
	start := time.Now()
	msg := req.Msg
	if msg.Latitude < -90.0 || msg.Latitude > 90.0 || msg.Longitude < -180.0 || msg.Longitude > 180.0 {
		return nil, connect.NewError(connect.CodeInvalidArgument, fmt.Errorf("invalid coordinates"))
	}

	maxRadius := 300.0
	if msg.MaxRadiusMeters != nil && *msg.MaxRadiusMeters > 0 {
		maxRadius = *msg.MaxRadiusMeters
	}

	cacheKey := reverseCacheKey{
		cell:   geo.QuantizeCoord(msg.Latitude, msg.Longitude),
		radius: uint32(maxRadius),
	}
	currentEpoch := s.store.CacheEpoch()

	// Hot-path: service-level protobuf response cache (bypasses object reconstruction)
	if val, ok := s.protoCache.Load(cacheKey); ok {
		entry := val.(cachedProtoEntry)
		if entry.epoch == currentEpoch {
			cached := entry.resp
			return connect.NewResponse(&geov1.ReverseGeocodeResponse{
				Query: &geov1.Coordinate{
					Latitude:  msg.Latitude,
					Longitude: msg.Longitude,
				},
				MatchType:         cached.MatchType,
				Place:             cached.Place,
				Address:           cached.Address,
				FormattedTitle:    cached.FormattedTitle,
				FormattedSubtitle: cached.FormattedSubtitle,
				NearbyPlaces:      cached.NearbyPlaces,
				NearbyAddresses:   cached.NearbyAddresses,
				ExecutionTimeMs:   float64(time.Since(start).Microseconds()) / 1000.0,
			}), nil
		}
	}

	result, err := s.store.ReverseGeocode(ctx, msg.Latitude, msg.Longitude, maxRadius)
	if err != nil {
		slog.Error("reverse geocode failed", "error", err)
		return nil, connect.NewError(connect.CodeInternal, fmt.Errorf("reverse geocode failed: %w", err))
	}

	resp := &geov1.ReverseGeocodeResponse{
		Query: &geov1.Coordinate{
			Latitude:  result.QueryLat,
			Longitude: result.QueryLon,
		},
		MatchType:         result.MatchType,
		FormattedTitle:    result.FormattedTitle,
		FormattedSubtitle: result.FormattedSubtitle,
		ExecutionTimeMs:   result.ExecutionTimeMs,
	}

	if result.Place != nil {
		resp.Place = toProtoPlace(result.Place)
	}
	if result.Address != nil {
		resp.Address = toProtoAddress(result.Address)
	}

	if len(result.NearbyPlaces) > 0 {
		resp.NearbyPlaces = make([]*geov1.Place, len(result.NearbyPlaces))
		for i := range result.NearbyPlaces {
			resp.NearbyPlaces[i] = toProtoPlace(&result.NearbyPlaces[i])
		}
	}
	if len(result.NearbyAddresses) > 0 {
		resp.NearbyAddresses = make([]*geov1.Address, len(result.NearbyAddresses))
		for i := range result.NearbyAddresses {
			resp.NearbyAddresses[i] = toProtoAddress(&result.NearbyAddresses[i])
		}
	}

	// Cache the constructed proto response (Rule 120: bounded sync.Map)
	if s.cacheCount.Load() < maxProtoCacheEntries {
		s.protoCache.Store(cacheKey, cachedProtoEntry{
			epoch: currentEpoch,
			resp:  resp,
		})
		s.cacheCount.Add(1)
	}

	return connect.NewResponse(resp), nil
}

// Search performs full-text search across places and addresses.
func (s *GeoServer) Search(
	ctx context.Context,
	req *connect.Request[geov1.SearchRequest],
) (*connect.Response[geov1.SearchResponse], error) {
	msg := req.Msg
	limit := 15
	if msg.Limit != nil && *msg.Limit > 0 {
		limit = int(*msg.Limit)
	}

	var lat, lon *float64
	if msg.Latitude != nil && msg.Longitude != nil {
		lat = msg.Latitude
		lon = msg.Longitude
	}

	items, err := s.store.Search(ctx, msg.Query, lat, lon, limit)
	if err != nil {
		slog.Error("search failed", "error", err)
		return nil, connect.NewError(connect.CodeInternal, fmt.Errorf("search failed: %w", err))
	}

	protoItems := make([]*geov1.SearchItem, len(items))
	for i, it := range items {
		protoItems[i] = &geov1.SearchItem{
			Type:           it.Type,
			Id:             it.ID,
			Title:          it.Title,
			Subtitle:       it.Subtitle,
			Category:       it.Category,
			Latitude:       it.Latitude,
			Longitude:      it.Longitude,
			DistanceMeters: it.DistanceMeters,
		}
	}

	return connect.NewResponse(&geov1.SearchResponse{Results: protoItems}), nil
}

// GetNearby fetches POIs and addresses around a coordinate for map viewport rendering.
func (s *GeoServer) GetNearby(
	ctx context.Context,
	req *connect.Request[geov1.GetNearbyRequest],
) (*connect.Response[geov1.GetNearbyResponse], error) {
	msg := req.Msg
	radius := 500.0
	if msg.RadiusMeters != nil && *msg.RadiusMeters > 0 {
		radius = *msg.RadiusMeters
	}
	limit := 40
	if msg.Limit != nil && *msg.Limit > 0 {
		limit = int(*msg.Limit)
	}

	places, addrs, err := s.store.GetNearby(ctx, msg.Latitude, msg.Longitude, radius, limit)
	if err != nil {
		slog.Error("get nearby failed", "error", err)
		return nil, connect.NewError(connect.CodeInternal, fmt.Errorf("get nearby failed: %w", err))
	}

	protoPlaces := make([]*geov1.Place, len(places))
	for i := range places {
		protoPlaces[i] = toProtoPlace(&places[i])
	}

	protoAddrs := make([]*geov1.Address, len(addrs))
	for i := range addrs {
		protoAddrs[i] = toProtoAddress(&addrs[i])
	}

	return connect.NewResponse(&geov1.GetNearbyResponse{
		Places:    protoPlaces,
		Addresses: protoAddrs,
	}), nil
}

// GetStats returns current offline database statistics.
func (s *GeoServer) GetStats(
	ctx context.Context,
	_ *connect.Request[geov1.GetStatsRequest],
) (*connect.Response[geov1.GetStatsResponse], error) {
	stats, err := s.store.GetStats(ctx)
	if err != nil {
		slog.Error("get stats failed", "error", err)
		return nil, connect.NewError(connect.CodeInternal, fmt.Errorf("get stats failed: %w", err))
	}

	protoRegions := make([]*geov1.IndexedRegion, len(stats.Regions))
	for i, r := range stats.Regions {
		protoRegions[i] = &geov1.IndexedRegion{
			Id:           r.ID,
			Name:         r.Name,
			Bbox:         r.BBox,
			PlaceCount:   r.PlaceCount,
			AddressCount: r.AddressCount,
			CreatedAt:    r.CreatedAt,
		}
	}

	return connect.NewResponse(&geov1.GetStatsResponse{
		PlaceCount:      stats.PlaceCount,
		AddressCount:    stats.AddressCount,
		Regions:         protoRegions,
		DbSizeBytes:     stats.DbSizeBytes,
		DbSizeFormatted: stats.DbSizeFormatted,
	}), nil
}

func toProtoPlace(p *geo.Place) *geov1.Place {
	if p == nil {
		return nil
	}
	return &geov1.Place{
		Id:             p.ID,
		Uuid:           p.UUID,
		Name:           p.Name,
		Category:       p.Category,
		Brand:          p.Brand,
		AddressText:    p.AddressText,
		Suburb:         p.Suburb,
		State:          p.State,
		Postcode:       p.Postcode,
		Latitude:       p.Latitude,
		Longitude:      p.Longitude,
		Phone:          p.Phone,
		Website:        p.Website,
		Confidence:     p.Confidence,
		DistanceMeters: p.DistanceMeters,
	}
}

func toProtoAddress(a *geo.Address) *geov1.Address {
	if a == nil {
		return nil
	}
	return &geov1.Address{
		Id:             a.ID,
		Uuid:           a.UUID,
		Unit:           a.Unit,
		Number:         a.Number,
		Street:         a.Street,
		Suburb:         a.Suburb,
		State:          a.State,
		Postcode:       a.Postcode,
		FullAddress:    a.FullAddress,
		Latitude:       a.Latitude,
		Longitude:      a.Longitude,
		DistanceMeters: a.DistanceMeters,
	}
}
