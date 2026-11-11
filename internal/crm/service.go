package crm

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"connectrpc.com/connect"

	crmv1 "github.com/benebsworth/addr/gen/go/crm/v1"
)

// Server implements crmv1connect.CrmServiceHandler on top of Store. Callers
// are authenticated and authorized by the auth interceptor.
type Server struct {
	store *Store
}

// NewServer builds the CRM RPC server.
func NewServer(store *Store) *Server { return &Server{store: store} }

func principalFrom(ctx context.Context) (Principal, error) {
	principal, ok := PrincipalFrom(ctx)
	if !ok {
		return Principal{}, connect.NewError(connect.CodeUnauthenticated, errors.New("no principal in context"))
	}
	return principal, nil
}

func featureKindFromProto(kind crmv1.FeatureKind) (string, error) {
	switch kind {
	case crmv1.FeatureKind_FEATURE_KIND_ROOF:
		return "roof", nil
	case crmv1.FeatureKind_FEATURE_KIND_TREE:
		return "tree", nil
	case crmv1.FeatureKind_FEATURE_KIND_OBJECT:
		return "object", nil
	default:
		return "", connect.NewError(connect.CodeInvalidArgument, errors.New("feature kind is required"))
	}
}

func featureKindToProto(kind string) crmv1.FeatureKind {
	switch kind {
	case "roof":
		return crmv1.FeatureKind_FEATURE_KIND_ROOF
	case "tree":
		return crmv1.FeatureKind_FEATURE_KIND_TREE
	case "object":
		return crmv1.FeatureKind_FEATURE_KIND_OBJECT
	default:
		return crmv1.FeatureKind_FEATURE_KIND_UNSPECIFIED
	}
}

func mapError(err error) error {
	switch {
	case err == nil:
		return nil
	case errors.Is(err, ErrNotFound):
		return connect.NewError(connect.CodeNotFound, err)
	default:
		return connect.NewError(connect.CodeInternal, err)
	}
}

// WhoAmI resolves the presented credential.
func (s *Server) WhoAmI(ctx context.Context, _ *connect.Request[crmv1.WhoAmIRequest]) (*connect.Response[crmv1.WhoAmIResponse], error) {
	principal, err := principalFrom(ctx)
	if err != nil {
		return nil, err
	}
	response := &crmv1.WhoAmIResponse{
		Principal: principal.Name,
		Roles:     principal.Roles,
		Scopes:    principal.Scopes,
	}
	if principal.KeyLabel != "" {
		response.KeyLabel = &principal.KeyLabel
	}
	return connect.NewResponse(response), nil
}

// GetAddressCrm returns metadata, boundary overrides, and POIs for an address.
func (s *Server) GetAddressCrm(ctx context.Context, req *connect.Request[crmv1.GetAddressCrmRequest]) (*connect.Response[crmv1.GetAddressCrmResponse], error) {
	if req.Msg.GetAddressId() <= 0 {
		return nil, connect.NewError(connect.CodeInvalidArgument, errors.New("address_id is required"))
	}
	meta, err := s.store.GetAddressMeta(ctx, req.Msg.GetAddressId())
	if err != nil {
		return nil, mapError(err)
	}
	features, err := s.store.ListFeatures(ctx, req.Msg.GetAddressId())
	if err != nil {
		return nil, mapError(err)
	}
	pois, err := s.store.ListPois(ctx, PoiQuery{AddressID: &req.Msg.AddressId})
	if err != nil {
		return nil, mapError(err)
	}
	response := &crmv1.GetAddressCrmResponse{
		Meta:     addressMetaToProto(meta),
		Features: make([]*crmv1.SpatialFeature, 0, len(features)),
		Pois:     make([]*crmv1.Poi, 0, len(pois)),
	}
	for _, feature := range features {
		response.Features = append(response.Features, featureToProto(feature))
	}
	for _, poi := range pois {
		response.Pois = append(response.Pois, poiToProto(poi))
	}
	return connect.NewResponse(response), nil
}

// SearchAddresses finds addresses for the editor.
func (s *Server) SearchAddresses(ctx context.Context, req *connect.Request[crmv1.SearchAddressesRequest]) (*connect.Response[crmv1.SearchAddressesResponse], error) {
	query := strings.TrimSpace(req.Msg.GetQuery())
	if query == "" {
		return nil, connect.NewError(connect.CodeInvalidArgument, errors.New("query is required"))
	}
	metas, err := s.store.SearchAddresses(ctx, query, int(req.Msg.GetLimit()))
	if err != nil {
		return nil, mapError(err)
	}
	response := &crmv1.SearchAddressesResponse{Addresses: make([]*crmv1.AddressMeta, 0, len(metas))}
	for _, meta := range metas {
		response.Addresses = append(response.Addresses, addressMetaToProto(meta))
	}
	return connect.NewResponse(response), nil
}

// UpdateAddressMeta upserts editable metadata for an address.
func (s *Server) UpdateAddressMeta(ctx context.Context, req *connect.Request[crmv1.UpdateAddressMetaRequest]) (*connect.Response[crmv1.UpdateAddressMetaResponse], error) {
	principal, err := principalFrom(ctx)
	if err != nil {
		return nil, err
	}
	if req.Msg.GetAddressId() <= 0 {
		return nil, connect.NewError(connect.CodeInvalidArgument, errors.New("address_id is required"))
	}
	meta := AddressMeta{
		AddressID:  req.Msg.GetAddressId(),
		Status:     req.Msg.Status,
		OwnerName:  req.Msg.OwnerName,
		OwnerEmail: req.Msg.OwnerEmail,
		OwnerPhone: req.Msg.OwnerPhone,
		Notes:      req.Msg.Notes,
		Tags:       req.Msg.GetTags(),
	}
	updated, err := s.store.UpdateAddressMeta(ctx, meta, principal.Name)
	if err != nil {
		return nil, mapError(err)
	}
	return connect.NewResponse(&crmv1.UpdateAddressMetaResponse{Meta: addressMetaToProto(updated)}), nil
}

// UpsertFeature creates or replaces a boundary override, enforcing the scope
// for the feature kind.
func (s *Server) UpsertFeature(ctx context.Context, req *connect.Request[crmv1.UpsertFeatureRequest]) (*connect.Response[crmv1.UpsertFeatureResponse], error) {
	principal, err := principalFrom(ctx)
	if err != nil {
		return nil, err
	}
	kind, err := featureKindFromProto(req.Msg.GetKind())
	if err != nil {
		return nil, err
	}
	requiredScope, _ := FeatureScope(kind)
	if !principal.HasScope(requiredScope) {
		return nil, connect.NewError(connect.CodePermissionDenied,
			fmt.Errorf("missing required scope %q", requiredScope))
	}
	if req.Msg.GetAddressId() <= 0 {
		return nil, connect.NewError(connect.CodeInvalidArgument, errors.New("address_id is required"))
	}
	if strings.TrimSpace(req.Msg.GetGeometryJson()) == "" {
		return nil, connect.NewError(connect.CodeInvalidArgument, errors.New("geometry_json is required"))
	}
	properties := req.Msg.GetProperties()
	if len(properties) == 0 && req.Msg.GetId() > 0 {
		// Geometry-only updates (e.g. vertex drags) must not wipe stored
		// properties such as eave/ridge heights.
		if existing, err := s.store.GetFeature(ctx, req.Msg.GetId()); err == nil && existing.Properties != nil {
			properties = existing.Properties
		}
	}
	feature := SpatialFeature{
		ID:           req.Msg.GetId(),
		AddressID:    req.Msg.GetAddressId(),
		Kind:         kind,
		GeometryJSON: req.Msg.GetGeometryJson(),
		Label:        req.Msg.Label,
		Properties:   properties,
	}
	upserted, err := s.store.UpsertFeature(ctx, feature, principal.Name)
	if err != nil {
		return nil, mapError(err)
	}
	return connect.NewResponse(&crmv1.UpsertFeatureResponse{Feature: featureToProto(upserted)}), nil
}

// DeleteFeature removes a boundary override, enforcing the kind scope.
func (s *Server) DeleteFeature(ctx context.Context, req *connect.Request[crmv1.DeleteFeatureRequest]) (*connect.Response[crmv1.DeleteFeatureResponse], error) {
	principal, err := principalFrom(ctx)
	if err != nil {
		return nil, err
	}
	if req.Msg.GetId() <= 0 {
		return nil, connect.NewError(connect.CodeInvalidArgument, errors.New("id is required"))
	}
	feature, err := s.store.GetFeature(ctx, req.Msg.GetId())
	if err != nil {
		return nil, mapError(err)
	}
	requiredScope, _ := FeatureScope(feature.Kind)
	if !principal.HasScope(requiredScope) {
		return nil, connect.NewError(connect.CodePermissionDenied,
			fmt.Errorf("missing required scope %q", requiredScope))
	}
	if err := s.store.DeleteFeature(ctx, req.Msg.GetId()); err != nil {
		return nil, mapError(err)
	}
	return connect.NewResponse(&crmv1.DeleteFeatureResponse{}), nil
}

// CreatePoi adds a POI.
func (s *Server) CreatePoi(ctx context.Context, req *connect.Request[crmv1.CreatePoiRequest]) (*connect.Response[crmv1.CreatePoiResponse], error) {
	principal, err := principalFrom(ctx)
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(req.Msg.GetName()) == "" {
		return nil, connect.NewError(connect.CodeInvalidArgument, errors.New("name is required"))
	}
	poi := Poi{
		Name:        req.Msg.GetName(),
		Category:    req.Msg.Category,
		AddressText: req.Msg.AddressText,
		Latitude:    req.Msg.GetLatitude(),
		Longitude:   req.Msg.GetLongitude(),
		Phone:       req.Msg.Phone,
		Website:     req.Msg.Website,
		AddressID:   req.Msg.AddressId,
		Notes:       req.Msg.Notes,
	}
	created, err := s.store.CreatePoi(ctx, poi, principal.Name)
	if err != nil {
		return nil, mapError(err)
	}
	return connect.NewResponse(&crmv1.CreatePoiResponse{Poi: poiToProto(created)}), nil
}

// UpdatePoi edits a POI.
func (s *Server) UpdatePoi(ctx context.Context, req *connect.Request[crmv1.UpdatePoiRequest]) (*connect.Response[crmv1.UpdatePoiResponse], error) {
	principal, err := principalFrom(ctx)
	if err != nil {
		return nil, err
	}
	if req.Msg.GetId() <= 0 {
		return nil, connect.NewError(connect.CodeInvalidArgument, errors.New("id is required"))
	}
	poi := Poi{
		ID:          req.Msg.GetId(),
		Name:        req.Msg.GetName(),
		Category:    req.Msg.Category,
		AddressText: req.Msg.AddressText,
		Phone:       req.Msg.Phone,
		Website:     req.Msg.Website,
		AddressID:   req.Msg.AddressId,
		Notes:       req.Msg.Notes,
	}
	if req.Msg.Latitude != nil {
		poi.Latitude = req.Msg.GetLatitude()
	}
	if req.Msg.Longitude != nil {
		poi.Longitude = req.Msg.GetLongitude()
	}
	updated, err := s.store.UpdatePoi(ctx, poi, principal.Name)
	if err != nil {
		return nil, mapError(err)
	}
	return connect.NewResponse(&crmv1.UpdatePoiResponse{Poi: poiToProto(updated)}), nil
}

// DeletePoi removes a POI.
func (s *Server) DeletePoi(ctx context.Context, req *connect.Request[crmv1.DeletePoiRequest]) (*connect.Response[crmv1.DeletePoiResponse], error) {
	principal, err := principalFrom(ctx)
	if err != nil {
		return nil, err
	}
	if req.Msg.GetId() <= 0 {
		return nil, connect.NewError(connect.CodeInvalidArgument, errors.New("id is required"))
	}
	_ = principal
	if err := s.store.DeletePoi(ctx, req.Msg.GetId()); err != nil {
		return nil, mapError(err)
	}
	return connect.NewResponse(&crmv1.DeletePoiResponse{}), nil
}

// ListPois lists POIs filtered by address, bbox, or proximity.
func (s *Server) ListPois(ctx context.Context, req *connect.Request[crmv1.ListPoisRequest]) (*connect.Response[crmv1.ListPoisResponse], error) {
	query := PoiQuery{
		MinLat:  req.Msg.MinLatitude,
		MinLon:  req.Msg.MinLongitude,
		MaxLat:  req.Msg.MaxLatitude,
		MaxLon:  req.Msg.MaxLongitude,
		NearLat: req.Msg.NearLatitude,
		NearLon: req.Msg.NearLongitude,
		RadiusM: req.Msg.RadiusMeters,
		Limit:   int(req.Msg.GetLimit()),
	}
	if req.Msg.AddressId != nil {
		query.AddressID = req.Msg.AddressId
	}
	pois, err := s.store.ListPois(ctx, query)
	if err != nil {
		return nil, mapError(err)
	}
	response := &crmv1.ListPoisResponse{Pois: make([]*crmv1.Poi, 0, len(pois))}
	for _, poi := range pois {
		response.Pois = append(response.Pois, poiToProto(poi))
	}
	return connect.NewResponse(response), nil
}

// ------------------------------------------------------------------ mapping

func addressMetaToProto(meta AddressMeta) *crmv1.AddressMeta {
	out := &crmv1.AddressMeta{
		AddressId:   meta.AddressID,
		FullAddress: meta.FullAddress,
		Status:      meta.Status,
		OwnerName:   meta.OwnerName,
		OwnerEmail:  meta.OwnerEmail,
		OwnerPhone:  meta.OwnerPhone,
		Notes:       meta.Notes,
		Tags:        meta.Tags,
		UpdatedAt:   meta.UpdatedAt,
		UpdatedBy:   meta.UpdatedBy,
	}
	if out.Tags == nil {
		out.Tags = []string{}
	}
	return out
}

func featureToProto(feature SpatialFeature) *crmv1.SpatialFeature {
	return &crmv1.SpatialFeature{
		Id:           feature.ID,
		AddressId:    feature.AddressID,
		Kind:         featureKindToProto(feature.Kind),
		GeometryJson: feature.GeometryJSON,
		Label:        feature.Label,
		Properties:   feature.Properties,
		UpdatedAt:    feature.UpdatedAt,
		UpdatedBy:    feature.UpdatedBy,
	}
}

func poiToProto(poi Poi) *crmv1.Poi {
	return &crmv1.Poi{
		Id:             poi.ID,
		Name:           poi.Name,
		Category:       poi.Category,
		AddressText:    poi.AddressText,
		Latitude:       poi.Latitude,
		Longitude:      poi.Longitude,
		Phone:          poi.Phone,
		Website:        poi.Website,
		AddressId:      poi.AddressID,
		Notes:          poi.Notes,
		UpdatedAt:      poi.UpdatedAt,
		UpdatedBy:      poi.UpdatedBy,
		DistanceMeters: poi.DistanceM,
	}
}
