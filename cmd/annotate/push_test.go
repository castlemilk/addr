package main

import (
	"bytes"
	"context"
	"encoding/json"
	"image"
	"image/png"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/benebsworth/addr/internal/annotation"
	"github.com/benebsworth/addr/internal/annotation/migration"
)

const pushTestToken = "push-cli-test-credential-at-least-24-chars"

type requestLog struct {
	mu      sync.Mutex
	entries []string
}

func (l *requestLog) add(entry string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.entries = append(l.entries, entry)
}

func (l *requestLog) snapshot() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]string(nil), l.entries...)
}

func pushServer(t *testing.T) (*httptest.Server, *requestLog) {
	t.Helper()
	store, err := annotation.OpenStore(context.Background(), "sqlite", filepath.Join(t.TempDir(), "cli.sqlite"), true)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })
	handler, err := annotation.NewHTTPHandler(store, annotation.HTTPConfig{Token: pushTestToken, AssetDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	log := &requestLog{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		log.add(r.Method + " " + r.URL.Path)
		handler.ServeHTTP(w, r)
	}))
	t.Cleanup(server.Close)
	client, err := migration.NewClient(server.URL, pushTestToken)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Call(context.Background(), "SaveProject", annotation.Object{"project": annotation.Object{"id": "mooroolbark", "name": "Mooroolbark"}, "expectedRevision": 0}); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Call(context.Background(), "SaveLabel", annotation.Object{"label": annotation.Object{"id": "mooroolbark-kind-building", "projectId": "mooroolbark", "name": "building", "color": "#D19A69"}, "expectedRevision": 0}); err != nil {
		t.Fatal(err)
	}
	return server, log
}

func writePushScene(t *testing.T, root, id string, revision int) {
	t.Helper()
	dir := filepath.Join(root, "data", "scenes", id)
	if err := os.MkdirAll(filepath.Join(dir, "models"), 0700); err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, image.NewRGBA(image.Rect(0, 0, 16, 16))); err != nil {
		t.Fatal(err)
	}
	sha := migration.Digest(buf.Bytes())
	if err := os.WriteFile(filepath.Join(dir, sha+".png"), buf.Bytes(), 0600); err != nil {
		t.Fatal(err)
	}
	signature := strings.Repeat("b", 64)
	objects := []annotation.Object{{"id": "roof", "kind": "building", "status": "candidate", "ring": [][]float64{{1, 1}, {8, 1}, {8, 8}, {1, 8}}}}
	model := annotation.Object{"id": id, "title": "CLI scene", "image_sha256": sha, "image_width": 16, "image_height": 16, "model_signature": signature, "objects": objects, "pixel_to_meters": [][]float64{{1, 0}, {0, -1}, {0, 0}}, "meters_to_pixel": [][]float64{{1, 0}, {0, -1}, {0, 0}}}
	raw, _ := json.Marshal(model)
	for _, name := range []string{"latest.json", "models/" + signature + ".json"} {
		if err := os.WriteFile(filepath.Join(dir, name), raw, 0600); err != nil {
			t.Fatal(err)
		}
	}
	review, _ := json.Marshal(annotation.Object{"revision": revision, "model_signature": signature, "updated_by": "local", "objects": objects, "history": []any{}})
	if err := os.WriteFile(filepath.Join(dir, "review.json"), review, 0600); err != nil {
		t.Fatal(err)
	}
}

func TestPushDryRunMakesNoWrites(t *testing.T) {
	t.Setenv("ANNOTATION_TOKEN", pushTestToken)
	server, log := pushServer(t)
	root, mirror := t.TempDir(), t.TempDir()
	writePushScene(t, root, "cli-scene", 3)
	seeded := len(log.snapshot())
	var output, diagnostics bytes.Buffer
	err := run(context.Background(), []string{"push", "--project", "mooroolbark", "--dir", mirror, "--root", root, "--url", server.URL, "--dry-run"}, strings.NewReader(""), &output, &diagnostics)
	if err != nil {
		t.Fatalf("%v\n%s", err, diagnostics.String())
	}
	var summary struct {
		RunID    string         `json:"runId"`
		Counts   map[string]int `json:"counts"`
		PlanFile string         `json:"planFile"`
		DryRun   bool           `json:"dryRun"`
	}
	if err := json.Unmarshal(output.Bytes(), &summary); err != nil || !summary.DryRun || summary.Counts["create"] != 1 || summary.RunID == "" {
		t.Fatalf("summary=%s err=%v", output.String(), err)
	}
	if _, err := os.Stat(summary.PlanFile); err != nil {
		t.Fatalf("plan.json not written: %v", err)
	}
	if !strings.Contains(diagnostics.String(), "cli-scene") || !strings.Contains(diagnostics.String(), "create") {
		t.Fatalf("table missing: %s", diagnostics.String())
	}
	for _, entry := range log.snapshot()[seeded:] {
		if strings.HasPrefix(entry, "POST /api/v1/rpc/") {
			method := strings.TrimPrefix(entry, "POST /api/v1/rpc/")
			if method != "ListChanges" && method != "GetItem" && method != "GetInfo" {
				t.Fatalf("dry-run issued %s", method)
			}
			continue
		}
		if !strings.HasPrefix(entry, "HEAD /api/v1/assets/") {
			t.Fatalf("dry-run issued %s", entry)
		}
	}
	items, err := migrationList(server.URL)
	if err != nil || items != 0 {
		t.Fatalf("dry-run created %d items: %v", items, err)
	}
	// push-status needs no network at all.
	statusServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { t.Errorf("push-status contacted %s", r.URL.Path) }))
	defer statusServer.Close()
	output.Reset()
	if err := run(context.Background(), []string{"push-status", "--dir", mirror, "--url", statusServer.URL}, strings.NewReader(""), &output, io.Discard); err != nil || !strings.Contains(output.String(), `"items":0`) {
		t.Fatalf("status: %v %s", err, output.String())
	}
}

func migrationList(url string) (int, error) {
	client, err := migration.NewClient(url, pushTestToken)
	if err != nil {
		return 0, err
	}
	data, err := client.Call(context.Background(), "ListItems", annotation.Object{"projectId": "mooroolbark", "limit": 100})
	if err != nil {
		return 0, err
	}
	items, _ := data["items"].([]any)
	return len(items), nil
}

func TestWriteCommandsRefuseNonLoopbackOriginWithoutAllowLive(t *testing.T) {
	t.Setenv("FIELDWORK_PUSH_ALLOW_LIVE", "")
	t.Setenv("ANNOTATION_TOKEN", pushTestToken)
	mirror, root := t.TempDir(), t.TempDir()
	// .invalid never resolves: the guard has to answer before a request is built,
	// and every spelling of a remote host (case, :443, other port, slash) is remote.
	for _, origin := range []string{"https://fieldwork-annotations.example.invalid", "https://FIELDWORK-ANNOTATIONS.example.invalid:443/", "https://fieldwork-annotations.example.invalid:8443"} {
		var output bytes.Buffer
		commands := [][]string{
			{"push", "--project", "mooroolbark", "--dir", mirror, "--root", root, "--url", origin},
			{"save-item", "--file", "-", "--url", origin},
			{"delete-item", "--id", "x", "--revision", "1", "--url", origin},
			{"rpc", "SaveAnnotation", "--file", "-", "--url", origin},
			{"rpc", "SomeFutureMethod", "--file", "-", "--url", origin},
			{"import", "--file", "-", "--url", origin},
		}
		for _, args := range commands {
			err := run(context.Background(), args, strings.NewReader(`{"id":"x"}`), &output, io.Discard)
			if err == nil || !strings.Contains(err.Error(), "--allow-live") || !strings.Contains(err.Error(), "FIELDWORK_PUSH_ALLOW_LIVE") {
				t.Fatalf("%v against %s: %v", args[:1], origin, err)
			}
		}
		if output.Len() != 0 {
			t.Fatal("a refused command produced output")
		}
		if _, err := os.Stat(filepath.Join(mirror, "push")); err == nil {
			t.Fatal("a refused push touched the mirror")
		}
	}
	for _, tc := range []struct {
		command, method string
		o               options
		want            bool
	}{
		{"push", "", options{dryRun: true}, false}, {"push", "", options{}, true}, {"push-status", "", options{}, false},
		{"rpc", "GetItem", options{}, false}, {"rpc", "ListChanges", options{}, false}, {"rpc", "SaveItem", options{}, true},
		{"items", "", options{}, false}, {"changes", "", options{}, false}, {"sync", "", options{}, false}, {"export", "", options{}, false},
		{"feedback", "", options{}, false}, {"seed", "", options{}, true}, {"save-label", "", options{}, true}, {"delete-label", "", options{}, true},
	} {
		if got := writesRemote(tc.command, tc.method, tc.o); got != tc.want {
			t.Fatalf("writesRemote(%s %s dryRun=%v)=%v", tc.command, tc.method, tc.o.dryRun, got)
		}
	}
	// A loopback server needs no flag (every other push test relies on that); the
	// flag itself is exercised on the shared guard in internal/annotation/migration.
}

func TestPushRefusesStalePlanHash(t *testing.T) {
	t.Setenv("ANNOTATION_TOKEN", pushTestToken)
	server, log := pushServer(t)
	root, mirror := t.TempDir(), t.TempDir()
	writePushScene(t, root, "cli-scene", 3)
	var output bytes.Buffer
	if err := run(context.Background(), []string{"push", "--project", "mooroolbark", "--dir", mirror, "--root", root, "--url", server.URL, "--dry-run"}, strings.NewReader(""), &output, io.Discard); err != nil {
		t.Fatal(err)
	}
	var summary struct {
		RunID string `json:"runId"`
	}
	if err := json.Unmarshal(output.Bytes(), &summary); err != nil {
		t.Fatal(err)
	}
	writePushScene(t, root, "cli-scene", 4) // local inputs changed after planning
	before := len(log.snapshot())
	output.Reset()
	err := run(context.Background(), []string{"push", "--project", "mooroolbark", "--dir", mirror, "--root", root, "--url", server.URL, "--run", summary.RunID}, strings.NewReader(""), &output, io.Discard)
	if err == nil || !strings.Contains(err.Error(), "stale") {
		t.Fatalf("stale plan applied: %v", err)
	}
	for _, entry := range log.snapshot()[before:] {
		if strings.HasPrefix(entry, "POST /api/v1/rpc/Save") || strings.HasPrefix(entry, "PUT ") {
			t.Fatalf("stale plan wrote: %s", entry)
		}
	}
	if err := run(context.Background(), []string{"push", "--project", "mooroolbark", "--dir", mirror, "--url", server.URL, "--run", "not-a-run-id"}, strings.NewReader(""), &output, io.Discard); err == nil || !strings.Contains(err.Error(), "--run") {
		t.Fatalf("invalid run id accepted: %v", err)
	}
	// The real apply creates the item, and a second apply of the same plan is a no-op.
	output.Reset()
	if err := run(context.Background(), []string{"push", "--project", "mooroolbark", "--dir", mirror, "--root", root, "--url", server.URL}, strings.NewReader(""), &output, io.Discard); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(output.String(), `"created":1`) {
		t.Fatalf("apply summary: %s", output.String())
	}
	items, err := migrationList(server.URL)
	if err != nil || items != 1 {
		t.Fatalf("items=%d err=%v", items, err)
	}
}
