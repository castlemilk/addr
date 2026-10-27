package push

import (
	"bytes"
	"context"
	"encoding/json"
	"image"
	"image/color"
	"image/png"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/benebsworth/addr/internal/annotation"
	"github.com/benebsworth/addr/internal/annotation/migration"
	"github.com/benebsworth/addr/internal/annotationsync"
)

const testProject = "mooroolbark"
const testToken = "push-test-credential-with-at-least-24-chars"

type fixtureScene struct {
	id              string
	width, height   int
	signature       string
	reviewRevision  int
	reviewSignature string
	objects         []annotation.Object
	history         bool
	updatedBy       string
	expansion       bool
	pixelSeed       byte
}

func defaultObjects() []annotation.Object {
	return []annotation.Object{
		{"id": "roof-a", "kind": "building", "status": "candidate", "geometry_reviewed": false, "source": "machine:sam3-text-onnx", "ring": [][]float64{{10, 10}, {40, 10}, {40, 40}, {10, 40}}, "holes": [][][]float64{{{20, 20}, {25, 20}, {22, 25}}}, "replaces": []string{"old-1", "old-2"}, "clipped_by_frame": true, "roof_line_edits": map[string]any{"checked": false, "vertices": []any{}}, "provenance": map[string]any{"source": "machine:decision-policy-v2"}, "model": map[string]any{"height_m": 4.5}},
		{"id": "tree-b", "kind": "tree", "status": "confirmed", "geometry_reviewed": true, "source": "sam3-text-onnx", "ring": [][]float64{{50, 50}, {60, 50}, {55, 60}}, "fieldwork_import": map[string]any{"pending": true, "itemId": "addr-scene-x", "revision": 3}},
	}
}

func encodePNG(t *testing.T, width, height int, seed byte) []byte {
	t.Helper()
	img := image.NewRGBA(image.Rect(0, 0, width, height))
	for i := 0; i < width && i < height; i++ {
		img.Set(i, i, color.RGBA{seed, byte(i), 128, 255})
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func writeJSON(t *testing.T, path string, value any) []byte {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	return data
}

// writeScene materialises one local scene under root/data/scenes and returns its image sha.
func writeScene(t *testing.T, root string, fs fixtureScene) string {
	t.Helper()
	if fs.width == 0 {
		fs.width, fs.height = 64, 64
	}
	if fs.signature == "" {
		fs.signature = strings.Repeat("b", 64)
	}
	if fs.reviewSignature == "" {
		fs.reviewSignature = fs.signature
	}
	if fs.objects == nil {
		fs.objects = defaultObjects()
	}
	if fs.updatedBy == "" {
		fs.updatedBy = "local"
	}
	dir := filepath.Join(root, "data", "scenes", fs.id)
	if err := os.RemoveAll(dir); err != nil {
		t.Fatal(err)
	}
	image := encodePNG(t, fs.width, fs.height, fs.pixelSeed)
	imageSHA := migration.Digest(image)
	if err := os.MkdirAll(filepath.Join(dir, "models"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, imageSHA+".png"), image, 0600); err != nil {
		t.Fatal(err)
	}
	model := annotation.Object{"id": fs.id, "title": "Fixture " + fs.id, "image_sha256": imageSHA, "image_width": fs.width, "image_height": fs.height, "model_signature": fs.signature, "objects": fs.objects, "pixel_to_meters": [][]float64{{1, 0}, {0, -1}, {0, 0}}, "meters_to_pixel": [][]float64{{1, 0}, {0, -1}, {0, 0}}}
	if fs.expansion {
		model["context_expansion"] = annotation.Object{"offset_pixels": []int{256, 256}, "source_size": []int{512, 512}}
	}
	writeJSON(t, filepath.Join(dir, "latest.json"), model)
	reviewModel := annotation.Object{}
	for k, v := range model {
		reviewModel[k] = v
	}
	reviewModel["model_signature"] = fs.reviewSignature
	writeJSON(t, filepath.Join(dir, "models", fs.reviewSignature+".json"), reviewModel)
	writeJSON(t, filepath.Join(dir, "confidence.json"), annotation.Object{"scene": fs.id, "mean": 0.7})
	if fs.reviewRevision > 0 {
		review := annotation.Object{"revision": fs.reviewRevision, "model_signature": fs.reviewSignature, "updated_by": fs.updatedBy, "updated_at": "2026-09-29T04:58:21.460Z", "objects": fs.objects, "groups": []any{}, "reviewed_classes": []any{}}
		if fs.history {
			review["history"] = []annotation.Object{{"revision": 1, "objects": fs.objects}}
		}
		writeJSON(t, filepath.Join(dir, "review.json"), review)
	}
	return imageSHA
}

// frozenDocument projects a scene as the frozen generation-0 item at revision 1.
func frozenDocument(t *testing.T, root, sceneID string) (annotation.Item, json.RawMessage) {
	t.Helper()
	files := migration.NewLocalSceneFiles(root, sceneID, nil)
	meta := annotation.Object{"schemaVersion": 1, "sceneId": sceneID, "snapshotId": strings.Repeat("a", 64), "importRun": strings.Repeat("a", 64), "importedAt": "2026-09-27T01:00:00Z", "stale": false}
	item, _, err := migration.ProjectScene(context.Background(), files, testProject, sceneID, migration.ProjectOptions{Metadata: meta, Tags: []string{testProject, "imported"}})
	if err != nil {
		t.Fatal(err)
	}
	item.Revision, item.CreatedAt, item.UpdatedAt = 1, "2026-09-27T01:00:00Z", "2026-09-27T01:00:00Z"
	raw, _ := json.Marshal(item)
	return item, raw
}

func testMirror(items ...json.RawMessage) annotationsync.Mirror {
	project, _ := json.Marshal(annotation.Object{"id": testProject, "revision": 1, "name": "Mooroolbark"})
	labels := []json.RawMessage{}
	for _, kind := range []string{"building", "tree"} {
		raw, _ := json.Marshal(LabelFor(testProject, kind))
		labels = append(labels, raw)
	}
	if items == nil {
		items = []json.RawMessage{}
	}
	return annotationsync.Mirror{Origin: "https://fieldwork.example", ProjectID: testProject, Snapshot: annotationsync.Snapshot{SchemaVersion: 1, Project: project, Labels: labels, Items: items, Cursor: "9"}}
}

// recorder wraps the real handler, logs every request and lets tests inject faults.
type recorder struct {
	mu       sync.Mutex
	requests []string // "METHOD path"
	methods  []string // RPC method names
	fault    func(r *http.Request) (int, string)
	tamper   func(r *http.Request, w http.ResponseWriter) bool
}

func (rec *recorder) reset() {
	rec.mu.Lock()
	defer rec.mu.Unlock()
	rec.requests, rec.methods = nil, nil
}

func (rec *recorder) count(prefix string) int {
	rec.mu.Lock()
	defer rec.mu.Unlock()
	n := 0
	for _, entry := range rec.requests {
		if strings.HasPrefix(entry, prefix) {
			n++
		}
	}
	return n
}

func (rec *recorder) rpc(method string) int {
	rec.mu.Lock()
	defer rec.mu.Unlock()
	n := 0
	for _, entry := range rec.methods {
		if entry == method {
			n++
		}
	}
	return n
}

type testServer struct {
	url    string
	client *migration.Client
	rec    *recorder
	assets string
}

func startServer(t *testing.T) *testServer {
	t.Helper()
	ctx := context.Background()
	store, err := annotation.OpenStore(ctx, "sqlite", filepath.Join(t.TempDir(), "push.sqlite"), true)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })
	assets := t.TempDir()
	handler, err := annotation.NewHTTPHandler(store, annotation.HTTPConfig{Token: testToken, AssetDir: assets})
	if err != nil {
		t.Fatal(err)
	}
	rec := &recorder{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec.mu.Lock()
		rec.requests = append(rec.requests, r.Method+" "+r.URL.Path)
		if strings.HasPrefix(r.URL.Path, "/api/v1/rpc/") {
			rec.methods = append(rec.methods, strings.TrimPrefix(r.URL.Path, "/api/v1/rpc/"))
		}
		fault, tamper := rec.fault, rec.tamper
		rec.mu.Unlock()
		if fault != nil {
			if status, body := fault(r); status != 0 {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(status)
				_, _ = w.Write([]byte(body))
				return
			}
		}
		if tamper != nil && tamper(r, w) {
			return
		}
		handler.ServeHTTP(w, r)
	}))
	t.Cleanup(server.Close)
	client, err := migration.NewClient(server.URL, testToken)
	if err != nil {
		t.Fatal(err)
	}
	ts := &testServer{url: server.URL, client: client, rec: rec, assets: assets}
	if _, err := client.Call(ctx, "SaveProject", annotation.Object{"project": annotation.Object{"id": testProject, "name": "Mooroolbark"}, "expectedRevision": 0}); err != nil {
		t.Fatal(err)
	}
	for _, kind := range []string{"building", "tree"} {
		if _, err := client.Call(ctx, "SaveLabel", annotation.Object{"label": LabelFor(testProject, kind), "expectedRevision": 0}); err != nil {
			t.Fatal(err)
		}
	}
	return ts
}

func (ts *testServer) fetch(ctx context.Context, project, cursor string, limit int) (annotationsync.Page, error) {
	raw, err := ts.client.CallRaw(ctx, "ListChanges", map[string]any{"projectId": project, "cursor": cursor, "limit": limit})
	if err != nil {
		return annotationsync.Page{}, err
	}
	var page annotationsync.Page
	err = json.Unmarshal(raw, &page)
	return page, err
}

func (ts *testServer) sync(t *testing.T, dir string) annotationsync.Mirror {
	t.Helper()
	ctx := context.Background()
	if _, err := annotationsync.Sync(ctx, ts.fetch, ts.url, testProject, dir, 100); err != nil {
		t.Fatal(err)
	}
	mirror, err := annotationsync.Load(dir, ts.url, testProject)
	if err != nil {
		t.Fatal(err)
	}
	return mirror
}

func (ts *testServer) getItem(t *testing.T, id string) (annotation.Object, json.RawMessage) {
	t.Helper()
	raw, err := ts.client.CallRaw(context.Background(), "GetItem", annotation.Object{"id": id})
	if err != nil {
		t.Fatalf("GetItem %s: %v", id, err)
	}
	doc, _, err := itemDocument(raw)
	if err != nil {
		t.Fatal(err)
	}
	var item annotation.Object
	if err := json.Unmarshal(doc, &item); err != nil {
		t.Fatal(err)
	}
	return item, doc
}

func (ts *testServer) cursor(t *testing.T) string {
	t.Helper()
	page, err := ts.fetch(context.Background(), testProject, "0", 500)
	if err != nil {
		t.Fatal(err)
	}
	for page.HasMore {
		next, err := ts.fetch(context.Background(), testProject, page.NextCursor, 500)
		if err != nil {
			t.Fatal(err)
		}
		page = next
	}
	return page.NextCursor
}

// seedFrozen writes an older frame locally, imports it as the frozen item, then
// advances the local scene (new signature, review revision) like the real repo.
func seedFrozen(t *testing.T, ts *testServer, root, sceneID string, newRevision int) annotation.Item {
	t.Helper()
	writeScene(t, root, fixtureScene{id: sceneID, width: 64, height: 64, signature: strings.Repeat("0", 64), reviewRevision: 1, updatedBy: "machine:decision-policy-v2", pixelSeed: 1})
	frozen, raw := frozenDocument(t, root, sceneID)
	if _, err := ts.client.Call(context.Background(), "SaveItem", map[string]any{"item": json.RawMessage(raw), "expectedRevision": 0}); err != nil {
		t.Fatal(err)
	}
	writeScene(t, root, fixtureScene{id: sceneID, width: 64, height: 64, signature: strings.Repeat("b", 64), reviewRevision: newRevision, history: true, expansion: true, pixelSeed: 2})
	return frozen
}
