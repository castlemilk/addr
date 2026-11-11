package crm_test

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/proto"

	crmv1 "github.com/benebsworth/addr/gen/go/crm/v1"
	"github.com/benebsworth/addr/gen/go/crm/v1/crmv1connect"
	"github.com/benebsworth/addr/internal/crm"
)

func newTestStore(t *testing.T) *crm.Store {
	t.Helper()
	store, err := crm.NewStore(filepath.Join(t.TempDir(), "crm.db"))
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	// Minimal cadastral table so address validation/lookups work.
	if _, err := store.DB().Exec(
		`CREATE TABLE addresses (id INTEGER PRIMARY KEY, full_address TEXT)`,
	); err != nil {
		t.Fatalf("create addresses: %v", err)
	}
	if _, err := store.DB().Exec(
		`INSERT INTO addresses (id, full_address) VALUES (1, '27 CAMBRIDGE ROAD, Mooroolbark VIC 3138')`,
	); err != nil {
		t.Fatalf("seed address: %v", err)
	}
	return store
}

func newTestServer(t *testing.T, store *crm.Store) *httptest.Server {
	t.Helper()
	server := crm.NewServer(store)
	path, handler := crmv1connect.NewCrmServiceHandler(
		server,
		connect.WithInterceptors(crm.NewAuthInterceptor(store)),
	)
	mux := http.NewServeMux()
	mux.Handle(path, handler)
	httpServer := httptest.NewServer(mux)
	t.Cleanup(httpServer.Close)
	return httpServer
}

func clientWithToken(url, token string) crmv1connect.CrmServiceClient {
	return crmv1connect.NewCrmServiceClient(
		http.DefaultClient,
		url,
		connect.WithInterceptors(bearerClientInterceptor(token)),
	)
}

func bearerClientInterceptor(token string) connect.UnaryInterceptorFunc {
	return func(next connect.UnaryFunc) connect.UnaryFunc {
		return func(ctx context.Context, req connect.AnyRequest) (connect.AnyResponse, error) {
			if token != "" {
				req.Header().Set("Authorization", "Bearer "+token)
			}
			return next(ctx, req)
		}
	}
}

func mustKey(t *testing.T, store *crm.Store, user string, scopes []string) string {
	t.Helper()
	secret, _, err := store.CreateAPIKey(context.Background(), user, "test", scopes)
	if err != nil {
		t.Fatalf("CreateAPIKey(%s): %v", user, err)
	}
	return secret
}

func TestResolveScopes(t *testing.T) {
	t.Parallel()
	editorScopes := crm.ResolveScopes([]string{"editor"}, nil)
	if len(editorScopes) != 6 {
		t.Fatalf("editor scopes = %v, want 6", editorScopes)
	}
	restricted := crm.ResolveScopes([]string{"editor"}, []string{crm.ScopePoiWrite, crm.ScopeRead})
	if len(restricted) != 2 {
		t.Fatalf("restricted scopes = %v, want 2", restricted)
	}
	admin := crm.ResolveScopes([]string{"admin"}, nil)
	principal := crm.Principal{Scopes: admin}
	if !principal.HasScope(crm.ScopeAdmin) || !principal.HasScope(crm.ScopeRoofWrite) {
		t.Fatalf("admin scopes missing expected values: %v", admin)
	}
}

func TestAuthenticationAndRBAC(t *testing.T) {
	t.Parallel()
	store := newTestStore(t)
	ctx := context.Background()

	if _, err := store.CreateUser(ctx, "alice", []string{"editor"}); err != nil {
		t.Fatalf("CreateUser: %v", err)
	}
	if _, err := store.CreateUser(ctx, "vic", []string{"viewer"}); err != nil {
		t.Fatalf("CreateUser: %v", err)
	}
	editorKey := mustKey(t, store, "alice", nil)
	viewerKey := mustKey(t, store, "vic", nil)
	httpServer := newTestServer(t, store)

	t.Run("anonymous is rejected", func(t *testing.T) {
		client := clientWithToken(httpServer.URL, "")
		_, err := client.WhoAmI(ctx, req(&crmv1.WhoAmIRequest{}))
		assertConnectCode(t, err, connect.CodeUnauthenticated)
	})

	t.Run("viewer cannot edit metadata", func(t *testing.T) {
		client := clientWithToken(httpServer.URL, viewerKey)
		_, err := client.UpdateAddressMeta(ctx, req(&crmv1.UpdateAddressMetaRequest{
			AddressId: 1,
			Status:    strPtr("quoted"),
		}))
		assertConnectCode(t, err, connect.CodePermissionDenied)
	})

	t.Run("viewer can read", func(t *testing.T) {
		client := clientWithToken(httpServer.URL, viewerKey)
		response, err := client.WhoAmI(ctx, req(&crmv1.WhoAmIRequest{}))
		if err != nil {
			t.Fatalf("WhoAmI: %v", err)
		}
		if response.Msg.GetPrincipal() != "vic" {
			t.Fatalf("principal = %q", response.Msg.GetPrincipal())
		}
	})

	t.Run("editor edits metadata and sees it back", func(t *testing.T) {
		client := clientWithToken(httpServer.URL, editorKey)
		if _, err := client.UpdateAddressMeta(ctx, req(&crmv1.UpdateAddressMetaRequest{
			AddressId: 1,
			Status:    strPtr("quoted"),
			OwnerName: strPtr("Jane Citizen"),
			Tags:      []string{"inspection"},
		})); err != nil {
			t.Fatalf("UpdateAddressMeta: %v", err)
		}
		response, err := client.GetAddressCrm(ctx, req(&crmv1.GetAddressCrmRequest{AddressId: 1}))
		if err != nil {
			t.Fatalf("GetAddressCrm: %v", err)
		}
		meta := response.Msg.GetMeta()
		if meta.GetStatus() != "quoted" || meta.GetOwnerName() != "Jane Citizen" {
			t.Fatalf("meta = %+v", meta)
		}
		if len(meta.GetTags()) != 1 || meta.GetTags()[0] != "inspection" {
			t.Fatalf("tags = %v", meta.GetTags())
		}
	})

	t.Run("revoked key is rejected", func(t *testing.T) {
		secret, key, err := store.CreateAPIKey(ctx, "alice", "temp", nil)
		if err != nil {
			t.Fatalf("CreateAPIKey: %v", err)
		}
		if err := store.RevokeAPIKey(ctx, key.ID); err != nil {
			t.Fatalf("RevokeAPIKey: %v", err)
		}
		client := clientWithToken(httpServer.URL, secret)
		_, err = client.WhoAmI(ctx, req(&crmv1.WhoAmIRequest{}))
		assertConnectCode(t, err, connect.CodeUnauthenticated)
	})
}

func TestNoAuthModeServesLocalAdmin(t *testing.T) {
	t.Parallel()
	store := newTestStore(t)
	server := crm.NewServer(store)
	path, handler := crmv1connect.NewCrmServiceHandler(
		server,
		connect.WithInterceptors(crm.NewAuthInterceptor(store, true)),
	)
	mux := http.NewServeMux()
	mux.Handle(path, handler)
	httpServer := httptest.NewServer(mux)
	t.Cleanup(httpServer.Close)
	ctx := context.Background()
	anonymous := clientWithToken(httpServer.URL, "")

	who, err := anonymous.WhoAmI(ctx, req(&crmv1.WhoAmIRequest{}))
	if err != nil {
		t.Fatalf("anonymous WhoAmI with noauth: %v", err)
	}
	if who.Msg.GetPrincipal() != "local" || who.Msg.GetKeyLabel() != "local-noauth" {
		t.Fatalf("principal = %q label = %q, want local/local-noauth",
			who.Msg.GetPrincipal(), who.Msg.GetKeyLabel())
	}
	principal := crm.Principal{Scopes: who.Msg.GetScopes()}
	for _, scope := range []string{crm.ScopeRead, crm.ScopeRoofWrite, crm.ScopeAdmin} {
		if !principal.HasScope(scope) {
			t.Fatalf("local principal missing scope %q: %v", scope, who.Msg.GetScopes())
		}
	}
	// A write without any credential succeeds and is audited as local-noauth.
	upserted, err := anonymous.UpsertFeature(ctx, req(&crmv1.UpsertFeatureRequest{
		AddressId:    1,
		Kind:         crmv1.FeatureKind_FEATURE_KIND_OBJECT,
		GeometryJson: `{"type":"Polygon","coordinates":[[[145.31,-37.79],[145.32,-37.79],[145.32,-37.78],[145.31,-37.78],[145.31,-37.79]]]}`,
		Label:        strPtr("roof-box proposal"),
	}))
	if err != nil {
		t.Fatalf("anonymous UpsertFeature with noauth: %v", err)
	}
	got, err := store.GetFeature(ctx, upserted.Msg.GetFeature().GetId())
	if err != nil {
		t.Fatalf("GetFeature: %v", err)
	}
	if got.UpdatedBy != "local" {
		t.Fatalf("UpdatedBy = %q, want local", got.UpdatedBy)
	}
}

func TestFeatureScopesAreKindSpecific(t *testing.T) {
	t.Parallel()
	store := newTestStore(t)
	ctx := context.Background()
	if _, err := store.CreateUser(ctx, "roofer", []string{"editor"}); err != nil {
		t.Fatalf("CreateUser: %v", err)
	}
	roofOnly := mustKey(t, store, "roofer", []string{crm.ScopeRead, crm.ScopeRoofWrite})
	httpServer := newTestServer(t, store)
	client := clientWithToken(httpServer.URL, roofOnly)

	geometry := `{"type":"Polygon","coordinates":[[[145.3125,-37.7905],[145.3126,-37.7905],[145.3126,-37.7906],[145.3125,-37.7906],[145.3125,-37.7905]]]}`

	roof, err := client.UpsertFeature(ctx, req(&crmv1.UpsertFeatureRequest{
		AddressId:    1,
		Kind:         crmv1.FeatureKind_FEATURE_KIND_ROOF,
		GeometryJson: geometry,
	}))
	if err != nil {
		t.Fatalf("roof upsert should be allowed: %v", err)
	}
	if roof.Msg.GetFeature().GetId() == 0 {
		t.Fatalf("roof id missing")
	}

	_, err = client.UpsertFeature(ctx, req(&crmv1.UpsertFeatureRequest{
		AddressId:    1,
		Kind:         crmv1.FeatureKind_FEATURE_KIND_TREE,
		GeometryJson: geometry,
	}))
	assertConnectCode(t, err, connect.CodePermissionDenied)

	// Deleting a tree written by an admin requires the tree scope.
	if _, err := store.UpsertFeature(ctx, crm.SpatialFeature{
		AddressID:    1,
		Kind:         "tree",
		GeometryJSON: geometry,
	}, "admin"); err != nil {
		t.Fatalf("seed tree: %v", err)
	}
	features, err := client.GetAddressCrm(ctx, req(&crmv1.GetAddressCrmRequest{AddressId: 1}))
	if err != nil {
		t.Fatalf("GetAddressCrm: %v", err)
	}
	if len(features.Msg.GetFeatures()) != 2 {
		t.Fatalf("features = %d, want 2", len(features.Msg.GetFeatures()))
	}
}

func TestPoiCRUD(t *testing.T) {
	t.Parallel()
	store := newTestStore(t)
	ctx := context.Background()
	if _, err := store.CreateUser(ctx, "agent", []string{"editor"}); err != nil {
		t.Fatalf("CreateUser: %v", err)
	}
	key := mustKey(t, store, "agent", nil)
	httpServer := newTestServer(t, store)
	client := clientWithToken(httpServer.URL, key)

	created, err := client.CreatePoi(ctx, req(&crmv1.CreatePoiRequest{
		Name:      "Roof inspection",
		Category:  strPtr("inspection"),
		Latitude:  -37.7905,
		Longitude: 145.3125,
		AddressId: int64Ptr(1),
		Notes:     strPtr("booked for Tuesday"),
	}))
	if err != nil {
		t.Fatalf("CreatePoi: %v", err)
	}
	poiID := created.Msg.GetPoi().GetId()
	if poiID == 0 {
		t.Fatal("poi id missing")
	}

	listed, err := client.ListPois(ctx, req(&crmv1.ListPoisRequest{AddressId: int64Ptr(1)}))
	if err != nil {
		t.Fatalf("ListPois: %v", err)
	}
	if len(listed.Msg.GetPois()) != 1 {
		t.Fatalf("pois = %d, want 1", len(listed.Msg.GetPois()))
	}

	if _, err := client.UpdatePoi(ctx, req(&crmv1.UpdatePoiRequest{
		Id:    poiID,
		Name:  strPtr("Roof inspection (rescheduled)"),
		Notes: strPtr("booked for Thursday"),
	})); err != nil {
		t.Fatalf("UpdatePoi: %v", err)
	}
	updated, err := client.GetAddressCrm(ctx, req(&crmv1.GetAddressCrmRequest{AddressId: 1}))
	if err != nil {
		t.Fatalf("GetAddressCrm: %v", err)
	}
	if updated.Msg.GetPois()[0].GetName() != "Roof inspection (rescheduled)" {
		t.Fatalf("poi name = %q", updated.Msg.GetPois()[0].GetName())
	}

	if _, err := client.DeletePoi(ctx, req(&crmv1.DeletePoiRequest{Id: poiID})); err != nil {
		t.Fatalf("DeletePoi: %v", err)
	}
	listed, err = client.ListPois(ctx, req(&crmv1.ListPoisRequest{AddressId: int64Ptr(1)}))
	if err != nil {
		t.Fatalf("ListPois after delete: %v", err)
	}
	if len(listed.Msg.GetPois()) != 0 {
		t.Fatalf("pois after delete = %d, want 0", len(listed.Msg.GetPois()))
	}
}

func TestSearchAddresses(t *testing.T) {
	t.Parallel()
	store := newTestStore(t)
	ctx := context.Background()
	if _, err := store.CreateUser(ctx, "agent", []string{"editor"}); err != nil {
		t.Fatalf("CreateUser: %v", err)
	}
	key := mustKey(t, store, "agent", nil)
	httpServer := newTestServer(t, store)
	client := clientWithToken(httpServer.URL, key)

	response, err := client.SearchAddresses(ctx, req(&crmv1.SearchAddressesRequest{Query: "CAMBRIDGE"}))
	if err != nil {
		t.Fatalf("SearchAddresses: %v", err)
	}
	if len(response.Msg.GetAddresses()) != 1 {
		t.Fatalf("addresses = %d, want 1", len(response.Msg.GetAddresses()))
	}
	if response.Msg.GetAddresses()[0].GetFullAddress() == "" {
		t.Fatal("full address missing")
	}
}

func assertConnectCode(t *testing.T, err error, want connect.Code) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected error code %v, got nil", want)
	}
	var connectErr *connect.Error
	if !errors.As(err, &connectErr) {
		t.Fatalf("expected connect error, got %T: %v", err, err)
	}
	if connectErr.Code() != want {
		t.Fatalf("error code = %v, want %v (%v)", connectErr.Code(), want, err)
	}
}

func strPtr(value string) *string { return &value }

func req[T any](msg *T) *connect.Request[T] { return connect.NewRequest(msg) }
func int64Ptr(value int64) *int64           { return &value }

func TestGeometryOnlyUpdatePreservesProperties(t *testing.T) {
	t.Parallel()
	store := newTestStore(t)
	ctx := context.Background()
	if _, err := store.CreateUser(ctx, "roofer2", []string{"editor"}); err != nil {
		t.Fatalf("CreateUser: %v", err)
	}
	key := mustKey(t, store, "roofer2", []string{crm.ScopeRead, crm.ScopeRoofWrite})
	client := clientWithToken(newTestServer(t, store).URL, key)

	geometry := `{"type":"Polygon","coordinates":[[[145.3125,-37.7905],[145.3126,-37.7905],[145.3126,-37.7906],[145.3125,-37.7906],[145.3125,-37.7905]]]}`
	created, err := client.UpsertFeature(ctx, req(&crmv1.UpsertFeatureRequest{
		AddressId:    7,
		Kind:         crmv1.FeatureKind_FEATURE_KIND_ROOF,
		GeometryJson: geometry,
		Properties:   map[string]string{"eave_height": "2.7", "ridge_height": "6.8"},
	}))
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	updated, err := client.UpsertFeature(ctx, req(&crmv1.UpsertFeatureRequest{
		Id:           created.Msg.GetFeature().GetId(),
		AddressId:    7,
		Kind:         crmv1.FeatureKind_FEATURE_KIND_ROOF,
		GeometryJson: geometry,
		Label:        proto.String("vertex drag"),
	}))
	if err != nil {
		t.Fatalf("update: %v", err)
	}
	got := updated.Msg.GetFeature().GetProperties()
	if got["eave_height"] != "2.7" || got["ridge_height"] != "6.8" {
		t.Fatalf("properties lost on geometry-only update: %v", got)
	}
}
