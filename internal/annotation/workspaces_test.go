package annotation

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

const bridgeTestKey = "synthetic-identity-bridge-key-at-least-32-bytes"
const legacyTestKey = "synthetic-legacy-workspace-token-at-least-24"

func signedWorkspaceRequest(t *testing.T, identity BridgeIdentity, method, path string, body []byte) *http.Request {
	t.Helper()
	r := httptest.NewRequest(method, path, bytes.NewReader(body))
	digest := sha256.Sum256(body)
	identity.Method, identity.URI, identity.BodySHA256 = method, r.URL.RequestURI(), hex.EncodeToString(digest[:])
	identity.IssuedAt = time.Now().Unix()
	identity.ExpiresAt = identity.IssuedAt + 60
	data, _ := json.Marshal(identity)
	encoded := base64.RawURLEncoding.EncodeToString(data)
	mac := hmac.New(sha256.New, []byte(bridgeTestKey))
	mac.Write([]byte(encoded))
	r.Header.Set(IdentityHeader, encoded+"."+base64.RawURLEncoding.EncodeToString(mac.Sum(nil)))
	r.Header.Set("Content-Type", "application/json")
	return r
}

func TestSharedWorkspaceHTTPIsolation(t *testing.T) {
	testSharedWorkspaceHTTPIsolation(t, openTestStore(t))
}

func testSharedWorkspaceHTTPIsolation(t *testing.T, root *SQLStore) {
	t.Helper()
	createProject(t, root, "legacy-project")
	w, err := NewWorkspaces(context.Background(), root, "owner@example.test")
	if err != nil {
		t.Fatal(err)
	}
	h, err := NewHTTPHandler(root, HTTPConfig{Token: legacyTestKey, Workspaces: w, IdentityBridgeKey: bridgeTestKey})
	if err != nil {
		t.Fatal(err)
	}
	a := BridgeIdentity{UID: "ordinary-a", Email: "a@example.test", Provider: "password"}
	b := BridgeIdentity{UID: "ordinary-b", Email: "b@example.test", Provider: "password"}
	call := func(identity BridgeIdentity, method, path string, input any, status int) Object {
		t.Helper()
		var body []byte
		if input != nil {
			body = raw(t, input)
		}
		r := signedWorkspaceRequest(t, identity, method, path, body)
		out := httptest.NewRecorder()
		h.ServeHTTP(out, r)
		if out.Code != status {
			t.Fatalf("%s %s (%s) HTTP %d expected %d: %s", method, path, identity.UID, out.Code, status, out.Body.String())
		}
		var result Object
		if json.Unmarshal(out.Body.Bytes(), &result) != nil {
			t.Fatal("invalid response")
		}
		return result
	}
	workspaceID := func(identity BridgeIdentity) string {
		info := call(identity, "GET", "/api/v1/info", nil, 200)
		if info["accountUid"] != identity.UID || info["requestId"] == "" {
			t.Fatal("connection receipt is missing its actual account or request ID")
		}
		return info["workspace"].(map[string]any)["id"].(string)
	}
	aID, bID := workspaceID(a), workspaceID(b)
	if aID == bID || aID == "legacy" || bID == "legacy" {
		t.Fatal("identities share a workspace")
	}
	if workspaceID(a) != aID {
		t.Fatal("workspace identity changed")
	}
	for _, identity := range []BridgeIdentity{a, b} {
		result := call(identity, "GET", "/api/v1/projects", nil, 200)
		if len(result["projects"].([]any)) != 0 {
			t.Fatal("new workspace exposes existing projects")
		}
		call(identity, "POST", "/api/v1/projects", Object{"project": Object{"id": "same-project", "name": identity.UID}, "expectedRevision": 0}, 200)
		item := imageItem("same-project")
		item.ID = "same-item"
		item.Notes = identity.UID
		call(identity, "POST", "/api/v1/items", Object{"item": item, "expectedRevision": 0}, 200)
	}
	private := imageItem("same-project")
	private.ID = "only-a"
	private.Notes = "private-a"
	call(a, "POST", "/api/v1/items", Object{"item": private, "expectedRevision": 0}, 200)
	call(b, "GET", "/api/v1/items/only-a", nil, 404)
	private.Notes = "attempted overwrite"
	call(b, "PUT", "/api/v1/items/only-a", Object{"item": private, "expectedRevision": 1}, 409)
	call(b, "DELETE", "/api/v1/items/only-a?expectedRevision=1", nil, 404)
	for _, identity := range []BridgeIdentity{a, b} {
		item := call(identity, "GET", "/api/v1/items/same-item", nil, 200)["item"].(map[string]any)
		if item["notes"] != identity.UID {
			t.Fatal("same-ID read crossed workspaces")
		}
		result := call(identity, "GET", "/api/v1/projects/same-project/items", nil, 200)
		count := 1
		if identity.UID == a.UID {
			count = 2
		}
		if len(result["items"].([]any)) != count {
			t.Fatal("foreign list/count")
		}
		export := call(identity, "GET", "/api/v1/export?projectId=same-project", nil, 200)
		if len(export["items"].([]any)) != count {
			t.Fatal("foreign export")
		}
		changes := call(identity, "GET", "/api/v1/changes", nil, 200)
		for _, value := range changes["changes"].([]any) {
			change := value.(map[string]any)
			if change["actor"] != "user:"+identity.UID {
				t.Fatal("foreign change feed")
			}
		}
	}
	call(a, "POST", "/api/v1/projects", Object{"project": Object{"id": "a-project", "name": "private"}, "expectedRevision": 0}, 200)
	call(b, "GET", "/api/v1/export?projectId=a-project", nil, 404)
	foreign := imageItem("a-project")
	foreign.ID = "foreign-ref"
	call(b, "POST", "/api/v1/items", Object{"item": foreign, "expectedRevision": 0}, 400)
	// Raw owner credentials and user-provided scope headers never select a new
	// user's workspace. Existing owner data remains available under the old key.
	r := httptest.NewRequest("GET", "/api/v1/projects", nil)
	r.Header.Set("Authorization", "Bearer "+legacyTestKey)
	r.Header.Set("X-Fieldwork-Workspace", aID)
	out := httptest.NewRecorder()
	h.ServeHTTP(out, r)
	if out.Code != 200 || !bytes.Contains(out.Body.Bytes(), []byte("legacy-project")) || bytes.Contains(out.Body.Bytes(), []byte("same-project")) {
		t.Fatal("legacy token scope changed")
	}
	// A password user cannot claim the owner's old data by choosing its email.
	imposter := BridgeIdentity{UID: "email-imposter", Email: "owner@example.test", EmailVerified: true, Provider: "password"}
	if workspaceID(imposter) == "legacy" {
		t.Fatal("password identity claimed owner data")
	}
	owner := BridgeIdentity{UID: "real-owner", Email: "owner@example.test", EmailVerified: true, Provider: "google.com"}
	if workspaceID(owner) != "legacy" {
		t.Fatal("existing owner cannot open original workspace")
	}
	if _, _, err = (workspaceStore{root: root}).List(context.Background(), "project", ListOptions{}); err == nil {
		t.Fatal("missing context silently used owner workspace")
	}
	// Reopen registry: this is persisted tenancy, not a per-process map.
	w2, err := NewWorkspaces(context.Background(), root, "owner@example.test")
	if err != nil {
		t.Fatal(err)
	}
	saved, err := w2.resolve(context.Background(), a)
	if err != nil || saved.ID != aID {
		t.Fatal("workspace not persisted")
	}
	if _, err = saved.store.Get(context.Background(), "item", "only-a"); err != nil {
		t.Fatal(err)
	}
	if _, err = root.Get(context.Background(), "project", "same-project"); !errors.Is(err, ErrNotFound) {
		t.Fatal("new data reached legacy tables")
	}
}

func TestIdentityBridgeRejectsAlteredRequests(t *testing.T) {
	identity := BridgeIdentity{UID: "ordinary-user", Provider: "password"}
	for _, mutate := range []func(*http.Request){
		func(r *http.Request) { r.Method = "DELETE" },
		func(r *http.Request) { r.URL.Path = "/api/v1/projects" },
		func(r *http.Request) { r.URL.RawQuery = "workspace=legacy" },
		func(r *http.Request) { r.Body = http.NoBody },
		func(r *http.Request) { r.Header.Set(IdentityHeader, r.Header.Get(IdentityHeader)+"x") },
	} {
		r := signedWorkspaceRequest(t, identity, "POST", "/api/v1/items", []byte(`{"example":true}`))
		mutate(r)
		if _, err := verifyBridge(r, bridgeTestKey, time.Now()); err == nil {
			t.Fatal("altered request accepted")
		}
	}
	r := signedWorkspaceRequest(t, identity, "GET", "/api/v1/info", nil)
	if _, err := verifyBridge(r, bridgeTestKey, time.Now().Add(2*time.Minute)); err == nil {
		t.Fatal("expired assertion accepted")
	}
	r = signedWorkspaceRequest(t, identity, "GET", "/api/v1/info", nil)
	if _, err := verifyBridge(r, legacyTestKey, time.Now()); err == nil {
		t.Fatal("owner token accepted as identity key")
	}
}
