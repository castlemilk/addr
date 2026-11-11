package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/benebsworth/addr/internal/annotationsync"
)

func TestSavePreservesRevisionAndConflict(t *testing.T) {
	t.Setenv("ANNOTATION_TOKEN", "private-test-token")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/rpc/SaveItem" || r.Header.Get("Authorization") != "Bearer private-test-token" {
			t.Errorf("incorrect authenticated API request: %s", r.URL.Path)
		}
		var body struct {
			Item struct {
				ID string `json:"id"`
			} `json:"item"`
			ExpectedRevision int `json:"expectedRevision"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body.Item.ID != "test-item" || body.ExpectedRevision != 7 {
			t.Errorf("incorrect save contract: %+v", body)
		}
		w.WriteHeader(http.StatusConflict)
		fmt.Fprint(w, `{"code":"aborted","message":"revision conflict"}`)
	}))
	defer server.Close()
	var output, diagnostics bytes.Buffer
	err := run(context.Background(), []string{"save-item", "--url", server.URL, "--revision", "7"}, strings.NewReader(`{"id":"test-item","title":"Revision test"}`), &output, &diagnostics)
	if err == nil || !strings.Contains(err.Error(), "409") {
		t.Fatalf("expected conflict, got %v", err)
	}
	if output.Len() != 0 {
		t.Fatal("failed write must not emit success JSON")
	}
}

func TestImportStopsWithoutOverwriting(t *testing.T) {
	var methods []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		methods = append(methods, r.URL.Path)
		var body map[string]json.RawMessage
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if string(body["expectedRevision"]) != "0" {
			t.Fatal("imports must only create")
		}
		if len(methods) == 2 {
			w.WriteHeader(409)
			fmt.Fprint(w, `{"message":"revision conflict"}`)
			return
		}
		fmt.Fprint(w, `{"project":{"id":"existing"}}`)
	}))
	defer server.Close()
	data := `{"schemaVersion":1,"project":{"id":"existing"},"labels":[{"id":"existing-label"}],"items":[{"id":"do-not-write"}]}`
	var output bytes.Buffer
	err := run(context.Background(), []string{"import", "--url", server.URL}, strings.NewReader(data), &output, io.Discard)
	if err == nil || !strings.Contains(err.Error(), "after 1 documents") {
		t.Fatalf("expected partial-import error, got %v", err)
	}
	if len(methods) != 2 {
		t.Fatalf("continued after conflict: %v", methods)
	}
}

func TestMCPBridgeNotificationsAndSSE(t *testing.T) {
	t.Setenv("ANNOTATION_TOKEN", "private-test-token")
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.URL.Path != "/mcp" || r.Header.Get("MCP-Protocol-Version") != "2025-11-25" {
			t.Errorf("incorrect MCP request headers or path")
		}
		if r.Header.Get("Authorization") != "Bearer private-test-token" {
			t.Errorf("missing authorization")
		}
		if requests == 1 {
			w.Header().Set("Mcp-Session-Id", "session-one")
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25"}}`)
			return
		}
		if r.Header.Get("Mcp-Session-Id") != "session-one" {
			t.Errorf("session was not retained")
		}
		if requests == 2 {
			w.WriteHeader(http.StatusAccepted)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"tools\":[]}}\n\n")
	}))
	defer server.Close()
	input := "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\"}\n{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\"}\n"
	var output bytes.Buffer
	if err := run(context.Background(), []string{"mcp", "--url", server.URL}, strings.NewReader(input), &output, io.Discard); err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(output.String()), "\n")
	if len(lines) != 2 || !json.Valid([]byte(lines[0])) || !json.Valid([]byte(lines[1])) {
		t.Fatalf("expected two clean JSON messages, got %q", output.String())
	}
}

func TestClientRejectsInsecureRemoteURL(t *testing.T) {
	if _, err := newClient("http://example.com", ""); err == nil {
		t.Fatal("remote token traffic must use HTTPS")
	}
	if _, err := newClient("https://user:password@example.com", ""); err == nil {
		t.Fatal("credentials must not be part of the URL")
	}
}

func TestImportAcceptsHTTPNDJSONExport(t *testing.T) {
	var methods []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		methods = append(methods, r.URL.Path)
		fmt.Fprint(w, `{}`)
	}))
	defer server.Close()
	data := "{\"kind\":\"project\",\"document\":{\"id\":\"project\"}}\n" +
		"{\"kind\":\"label\",\"document\":{\"id\":\"label\"}}\n" +
		"{\"kind\":\"item\",\"document\":{\"id\":\"item\"}}\n" +
		"{\"kind\":\"checkpoint\",\"schemaVersion\":1,\"cursor\":\"7\"}\n"
	if err := run(context.Background(), []string{"import", "--url", server.URL, "--format", "ndjson"}, strings.NewReader(data), io.Discard, io.Discard); err != nil {
		t.Fatal(err)
	}
	want := "/api/v1/rpc/SaveProject,/api/v1/rpc/SaveLabel,/api/v1/rpc/SaveItem"
	if strings.Join(methods, ",") != want {
		t.Fatalf("wrong import order: %v", methods)
	}
}

func TestMCPBridgeAcceptsLargeExportReply(t *testing.T) {
	payload, err := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 1,
		"result": map[string]any{"content": []any{map[string]any{"type": "text", "text": strings.Repeat("x", (8<<20)+100)}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, contentType := range []string{"application/json", "text/event-stream"} {
		t.Run(contentType, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", contentType)
				if contentType == "text/event-stream" {
					fmt.Fprint(w, "data: ")
				}
				_, _ = w.Write(payload)
				if contentType == "text/event-stream" {
					fmt.Fprint(w, "\n\n")
				}
			}))
			defer server.Close()
			request := strings.NewReader("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\"}\n")
			var output countWriter
			if err := run(context.Background(), []string{"mcp", "--url", server.URL}, request, &output, io.Discard); err != nil {
				t.Fatal(err)
			}
			if int(output) != len(payload)+1 {
				t.Fatalf("truncated export reply: %d of %d bytes", output, len(payload)+1)
			}
		})
	}
}

type countWriter int

func (w *countWriter) Write(data []byte) (int, error) {
	*w += countWriter(len(data))
	return len(data), nil
}

func TestSyncCommandAndLargeExportFallback(t *testing.T) {
	project := json.RawMessage(`{"id":"project","revision":1,"name":"Example"}`)
	item := json.RawMessage(`{"id":"item","projectId":"project","revision":1,"title":"Example","metadata":{"nested":{"kept":true}}}`)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/rpc/ExportProject" {
			w.WriteHeader(http.StatusBadRequest)
			fmt.Fprint(w, `{"message":"invalid input: export exceeds 32 MiB; use paginated ListChanges from cursor 0 to synchronize this project"}`)
			return
		}
		if r.URL.Path != "/api/v1/rpc/ListChanges" {
			t.Errorf("unexpected request: %s", r.URL.Path)
		}
		var request struct {
			ProjectID string `json:"projectId"`
			Cursor    string `json:"cursor"`
		}
		_ = json.NewDecoder(r.Body).Decode(&request)
		if request.ProjectID != "project" {
			t.Errorf("unbound request: %+v", request)
		}
		page := annotationsync.Page{Changes: []annotationsync.Change{}, NextCursor: request.Cursor}
		if request.Cursor == "0" {
			page.Changes = []annotationsync.Change{
				{Cursor: "1", ProjectID: "project", Kind: "project", ID: "project", Operation: "create", Revision: 1, Document: project},
				{Cursor: "2", ProjectID: "project", Kind: "item", ID: "item", Operation: "create", Revision: 1, Document: item},
			}
			page.NextCursor = "2"
		}
		_ = json.NewEncoder(w).Encode(page)
	}))
	defer server.Close()
	dir := t.TempDir()
	for iteration := 0; iteration < 2; iteration++ {
		var output bytes.Buffer
		if err := run(context.Background(), []string{"sync", "--url", server.URL, "--project", "project", "--dir", dir}, nil, &output, io.Discard); err != nil {
			t.Fatal(err)
		}
		var summary annotationsync.Summary
		if json.Unmarshal(output.Bytes(), &summary) != nil || summary.Updated != (iteration == 0) || summary.Cursor != "2" {
			t.Fatalf("sync CLI result: %s", output.String())
		}
	}
	for _, format := range []string{"json", "ndjson"} {
		var output bytes.Buffer
		if err := run(context.Background(), []string{"export", "--url", server.URL, "--project", "project", "--format", format}, nil, &output, io.Discard); err != nil {
			t.Fatal(err)
		}
		if format == "json" {
			var snapshot exportDocument
			if json.Unmarshal(output.Bytes(), &snapshot) != nil || snapshot.Cursor != "2" || len(snapshot.Items) != 1 || bytes.Count(output.Bytes(), []byte("\n")) != 1 {
				t.Fatalf("large export must be compact and complete: %s", output.String())
			}
		} else if !strings.Contains(output.String(), `"kind":"checkpoint","schemaVersion":1,"cursor":"2"`) {
			t.Fatalf("NDJSON export lacks checkpoint: %s", output.String())
		}
	}
}
