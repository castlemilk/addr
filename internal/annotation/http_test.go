package annotation

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"connectrpc.com/connect"
	annotationv1 "github.com/benebsworth/addr/gen/go/annotation/v1"
	"github.com/benebsworth/addr/gen/go/annotation/v1/annotationv1connect"
	"google.golang.org/protobuf/types/known/structpb"
)

const testToken = "annotation-test-credential-at-least-24-characters"

func httpTestServer(t *testing.T) *httptest.Server {
	t.Helper()
	handler, err := NewHTTPHandler(openTestStore(t), HTTPConfig{Token: testToken, AllowedOrigins: []string{"https://trusted.example"}})
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	return server
}

func request(t *testing.T, server *httptest.Server, method, path, body, token, origin string) (int, []byte, http.Header) {
	t.Helper()
	req, err := http.NewRequest(method, server.URL+path, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if origin != "" {
		req.Header.Set("Origin", origin)
	}
	res, err := server.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	data, err := io.ReadAll(res.Body)
	if err != nil {
		t.Fatal(err)
	}
	return res.StatusCode, data, res.Header
}

func TestAllDataEndpointsRequireAuthAndOrigin(t *testing.T) {
	server := httpTestServer(t)
	for _, path := range []string{"/api/v1/projects", "/api/v1/changes", "/api/v1/export?projectId=test", "/api/v1/rpc/GetInfo", "/annotation.v1.AnnotationService/GetInfo", "/mcp"} {
		status, _, _ := request(t, server, http.MethodPost, path, `{}`, "", "")
		if status != http.StatusUnauthorized {
			t.Errorf("%s unprotected (%d)", path, status)
		}
		status, _, _ = request(t, server, http.MethodPost, path, `{}`, "wrong", "")
		if status != http.StatusUnauthorized {
			t.Errorf("%s accepts wrong token (%d)", path, status)
		}
	}
	status, _, _ := request(t, server, http.MethodGet, "/healthz", "", "", "")
	if status != 200 {
		t.Fatal("health unavailable")
	}
	status, _, _ = request(t, server, http.MethodGet, "/api/v1/info", "", testToken, "https://evil.example")
	if status != 403 {
		t.Fatalf("invalid origin=%d", status)
	}
	status, _, headers := request(t, server, http.MethodGet, "/api/v1/info", "", testToken, "https://trusted.example")
	if status != 200 || headers.Get("Access-Control-Allow-Origin") != "https://trusted.example" || headers.Get("Cache-Control") != "no-store" {
		t.Fatalf("trusted origin=%d %v", status, headers)
	}
	if _, err := NewHTTPHandler(openTestStore(t), HTTPConfig{}); err == nil {
		t.Fatal("server allowed missing production credential")
	}
	if _, err := NewHTTPHandler(openTestStore(t), HTTPConfig{Token: testToken, AllowedOrigins: []string{"*"}}); err == nil {
		t.Fatal("wildcard origin accepted")
	}
}

func TestConnectRoundTripAndConflict(t *testing.T) {
	server := httpTestServer(t)
	client := annotationv1connect.NewAnnotationServiceClient(server.Client(), server.URL)
	call := func(data Object) *connect.Request[annotationv1.APIRequest] {
		message, err := structpb.NewStruct(data)
		if err != nil {
			t.Fatal(err)
		}
		req := connect.NewRequest(&annotationv1.APIRequest{Data: message})
		req.Header().Set("Authorization", "Bearer "+testToken)
		return req
	}
	res, err := client.SaveProject(context.Background(), call(Object{"project": Object{"id": "project", "name": "Example connect"}, "expectedRevision": 0}))
	if err != nil {
		t.Fatal(err)
	}
	if res.Msg.Data.AsMap()["project"].(map[string]any)["revision"] != float64(1) {
		t.Fatal("Connect response missing revision")
	}
	_, err = client.SaveProject(context.Background(), call(Object{"project": Object{"id": "project", "name": "Stale"}, "expectedRevision": 0}))
	if connect.CodeOf(err) != connect.CodeAborted {
		t.Fatalf("expected conflict, got %v", err)
	}
	_, err = client.ListProjects(context.Background(), connect.NewRequest(&annotationv1.APIRequest{}))
	if connect.CodeOf(err) != connect.CodeUnauthenticated {
		t.Fatalf("Connect auth code=%v", err)
	}
}

func TestRESTAndMCPUseSameStore(t *testing.T) {
	server := httpTestServer(t)
	status, data, _ := request(t, server, http.MethodPost, "/api/v1/projects", `{"project":{"id":"example","name":"Example project"},"expectedRevision":0}`, testToken, "")
	if status != 200 {
		t.Fatalf("save project=%d %s", status, data)
	}
	status, data, _ = request(t, server, http.MethodPost, "/mcp", `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}`, testToken, "")
	if status != 200 || !bytes.Contains(data, []byte(`"protocolVersion":"2025-11-25"`)) {
		t.Fatalf("initialize=%d %s", status, data)
	}
	status, data, _ = request(t, server, http.MethodPost, "/mcp", `{"jsonrpc":"2.0","method":"notifications/initialized"}`, testToken, "")
	if status != 202 || len(data) != 0 {
		t.Fatalf("notification=%d %s", status, data)
	}
	status, data, _ = request(t, server, http.MethodPost, "/mcp", `{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}`, testToken, "")
	if status != 200 || !bytes.Contains(data, []byte(`"readOnlyHint":true`)) {
		t.Fatalf("tools=%d %s", status, data)
	}
	status, data, _ = request(t, server, http.MethodPost, "/mcp", `{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_projects","arguments":{}}}`, testToken, "")
	if status != 200 || !bytes.Contains(data, []byte(`"name":"Example project"`)) {
		t.Fatalf("call=%d %s", status, data)
	}
	status, _, _ = request(t, server, http.MethodGet, "/mcp", "", testToken, "")
	if status != 405 {
		t.Fatalf("SSE unsupported status=%d", status)
	}
	status, data, _ = request(t, server, http.MethodGet, "/api/v1/export?projectId=example&format=ndjson", "", testToken, "")
	if status != 200 || !bytes.Contains(data, []byte(`"kind":"checkpoint"`)) {
		t.Fatalf("ndjson=%d %s", status, data)
	}
	status, data, _ = request(t, server, http.MethodGet, "/api/v1/changes?after=0&limit=1", "", testToken, "")
	if status != 200 {
		t.Fatal(string(data))
	}
	var page ChangePage
	if json.Unmarshal(data, &page) != nil || len(page.Changes) != 1 || page.NextCursor == "" {
		t.Fatalf("change response %s", data)
	}
}

func TestLocalDevelopmentRejectsNonLoopbackAndOversizeBodies(t *testing.T) {
	handler, err := NewHTTPHandler(openTestStore(t), HTTPConfig{Dev: true})
	if err != nil {
		t.Fatal(err)
	}
	for _, pair := range [][2]string{{"evil.example", "127.0.0.1:5000"}, {"127.0.0.1:8080", "192.0.2.1:5000"}} {
		req := httptest.NewRequest(http.MethodGet, "http://"+pair[0]+"/api/v1/info", nil)
		req.RemoteAddr = pair[1]
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, req)
		if w.Code != 403 {
			t.Fatalf("nonloopback accepted: %v", pair)
		}
	}
	server := httpTestServer(t)
	body := `{"project":{"name":"` + strings.Repeat("a", MaxRequestBytes) + `"}}`
	status, _, _ := request(t, server, http.MethodPost, "/api/v1/projects", body, testToken, "")
	if status != 400 && status != 413 {
		t.Fatalf("oversize body status=%d", status)
	}
}
