package migration

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/benebsworth/addr/internal/annotation"
)

func fixture(t *testing.T) *Bundle {
	t.Helper()
	directory := t.TempDir()
	if err := os.Mkdir(filepath.Join(directory, "blobs"), 0700); err != nil {
		t.Fatal(err)
	}
	manifest := Manifest{SchemaVersion: 1, SnapshotID: strings.Repeat("a", 64), CreatedAt: "2026-09-27T01:00:00Z", SceneIDs: []string{"test-scene"}, Summary: annotation.Object{"canonicalInventoryMatches": true}}
	add := func(name string, data []byte) {
		hash := Digest(data)
		if err := os.WriteFile(filepath.Join(directory, "blobs", hash), data, 0600); err != nil {
			t.Fatal(err)
		}
		manifest.Files = append(manifest.Files, File{Path: name, SHA256: hash, Bytes: int64(len(data)), ContentType: contentTypeFor(name), Chunks: []Chunk{{hash, int64(len(data))}}, MtimeNs: "1760000000123456000"})
	}
	image := []byte("fixture image bytes")
	imageHash := Digest(image)
	signature := strings.Repeat("b", 64)
	objects := []annotation.Object{{"id": "roof", "kind": "building", "status": "candidate", "source": "machine:example", "ring": [][]float64{{0, 0}, {100, 0}, {100, 100}, {0, 100}}, "holes": [][][]float64{{{20, 20}, {30, 20}, {25, 30}}}}, {"id": "rejected-roof", "kind": "building", "status": "rejected", "ring": [][]float64{{50, 50}, {70, 50}, {60, 70}}}}
	model := annotation.Object{"id": "test-scene", "title": "Explicit fixture scene", "image_sha256": imageHash, "image_width": 100, "image_height": 100, "model_signature": signature, "objects": objects, "pixel_to_meters": [][]float64{{1, 0}, {0, -1}, {-50, 50}}, "meters_to_pixel": [][]float64{{1, 0}, {0, -1}, {50, 50}}}
	raw, _ := json.Marshal(model)
	add("data/scenes/test-scene/latest.json", raw)
	add("data/scenes/test-scene/models/"+signature+".json", raw)
	add("data/scenes/test-scene/"+imageHash+".png", image)
	review, _ := json.Marshal(annotation.Object{"revision": 7, "model_signature": signature, "updated_by": "machine:decision-policy-v2", "objects": objects})
	add("data/scenes/test-scene/review.json", review)
	for _, name := range []string{"processing.json", "processing-map.geojson", "processing-summary.json", "crm.json"} {
		add("data/fieldwork-derived/mooroolbark/"+name, []byte(`{"type":"FeatureCollection","features":[]}`))
	}
	add("data/suburbs/mooroolbark/boundary.geojson", []byte(`{"type":"FeatureCollection","features":[]}`))
	add("data/empty.log", []byte{})
	first, second := []byte("first"), []byte("second")
	a, b := Digest(first), Digest(second)
	os.WriteFile(filepath.Join(directory, "blobs", a), first, 0600)
	os.WriteFile(filepath.Join(directory, "blobs", b), second, 0600)
	manifest.Files = append(manifest.Files, File{Path: "data/chunked.bin", SHA256: Digest(append(first, second...)), Bytes: 11, ContentType: "application/octet-stream", Chunks: []Chunk{{a, 5}, {b, 6}}})
	data, _ := json.Marshal(manifest)
	if err := os.WriteFile(filepath.Join(directory, "manifest.json"), data, 0600); err != nil {
		t.Fatal(err)
	}
	bundle, err := OpenBundle(directory)
	if err != nil {
		t.Fatal(err)
	}
	return bundle
}

func TestProjectionAndRoundTripPreserveEvidence(t *testing.T) {
	ctx := context.Background()
	b := fixture(t)
	if err := b.Verify(ctx); err != nil {
		t.Fatal(err)
	}
	p, err := BuildPlan(ctx, b, "mooroolbark")
	if err != nil {
		t.Fatal(err)
	}
	item := p.Items[0]
	if len(item.Annotations) != 2 || item.Status != "in_review" || item.Annotations[1].Properties["addr"].(annotation.Object)["status"] != "rejected" {
		t.Fatal("source statuses lost or promoted")
	}
	var rings [][][]float64
	if err := json.Unmarshal(item.Annotations[0].Geometry.Coordinates, &rings); err != nil {
		t.Fatal(err)
	}
	if len(rings) != 2 || len(rings[0]) != 5 || rings[0][1][0] != 1 || rings[1][0][0] != 0.2 {
		t.Fatalf("wrong normalized polygon: %v", rings)
	}
	restore := filepath.Join(t.TempDir(), "restored")
	if err := b.Restore(ctx, restore); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(filepath.Join(restore, "data/chunked.bin"))
	if err != nil || string(raw) != "firstsecond" {
		t.Fatal("chunk reconstruction failed")
	}
	stat, _ := os.Stat(filepath.Join(restore, "data/scenes/test-scene/latest.json"))
	if stat.ModTime().UnixNano() != 1760000000123456000 {
		t.Fatal("mtime not restored")
	}
	if err := b.Restore(ctx, restore); err == nil {
		t.Fatal("restore overwrote an existing destination")
	}
	if _, err := normalizedRing([]any{[]any{nil, float64(0)}, []any{float64(1), float64(0)}, []any{float64(1), float64(1)}}, 1, 1); err == nil {
		t.Fatal("null coordinate silently accepted")
	}
}

func TestImportIdempotenceConflictAndRemotePull(t *testing.T) {
	ctx := context.Background()
	b := fixture(t)
	p, err := BuildPlan(ctx, b, "mooroolbark")
	if err != nil {
		t.Fatal(err)
	}
	store, err := annotation.OpenStore(ctx, "sqlite", filepath.Join(t.TempDir(), "test.sqlite"), true)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	token := "explicit-local-fixture-token-at-least24"
	handler, err := annotation.NewHTTPHandler(store, annotation.HTTPConfig{Token: token, AssetDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(handler)
	defer server.Close()
	client, err := NewClient(server.URL, token)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Upload(ctx, b, 2, io.Discard); err != nil {
		t.Fatal(err)
	}
	first, err := client.Apply(ctx, p)
	if err != nil {
		t.Fatal(err)
	}
	if first.CreatedDocuments != 4 {
		t.Fatalf("created=%d", first.CreatedDocuments)
	}
	again, err := client.Apply(ctx, p)
	if err != nil || again.ExistingDocuments != 4 {
		t.Fatalf("repeat import=%+v %v", again, err)
	}
	if _, err := client.Verify(ctx, b, p, 2, true); err != nil {
		t.Fatal(err)
	}
	item := p.Items[0]
	item.Notes = "A later reviewer edit"
	data, _ := json.Marshal(item)
	if _, err := store.Save(ctx, "item", item.ID, 1, "test-reviewer", data); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Apply(ctx, p); err == nil {
		t.Fatal("import overwrote edited document")
	}
	// A post-import scene has assets absent from the immutable import manifest.
	// Removed evidence must also survive because its earlier revision is backed up.
	laterAssets := map[string][]byte{}
	uploadLater := func(payload string, includeSize bool) annotation.Object {
		t.Helper()
		content := []byte(payload)
		hash := Digest(content)
		laterAssets[hash] = content
		_, err := client.upload(ctx, hash, int64(len(content)), "application/octet-stream", func() (io.ReadCloser, error) { return io.NopCloser(bytes.NewReader(content)), nil })
		if err != nil {
			t.Fatal(err)
		}
		ref := annotation.Object{"url": "/api/v1/assets/" + hash, "sha256": hash}
		if includeSize {
			ref["bytes"] = len(content)
		}
		return ref
	}
	imageRef := uploadLater("later image", false) // Optional length is resolved by HEAD.
	modelRef := uploadLater(`{"id":"later-scene","preserved":true}`, true)
	chunkOne, chunkTwo := uploadLater("earlier ", true), uploadLater("evidence", true)
	var later annotation.Item
	copyData, _ := json.Marshal(p.Items[0])
	if err := json.Unmarshal(copyData, &later); err != nil {
		t.Fatal(err)
	}
	later.ID = "addr-scene-later"
	later.Source["sceneId"] = "later"
	later.Source["image"], later.Source["model"] = imageRef, modelRef
	later.Metadata["earlierEvidence"] = annotation.Object{"sha256": Digest([]byte("earlier evidence")), "bytes": 16, "chunks": []annotation.Object{chunkOne, chunkTwo}}
	laterData, _ := json.Marshal(later)
	if _, err := store.Save(ctx, "item", later.ID, 0, "later-reviewer", laterData); err != nil {
		t.Fatal(err)
	}
	delete(later.Metadata, "earlierEvidence")
	laterData, _ = json.Marshal(later)
	if _, err := store.Save(ctx, "item", later.ID, 1, "later-reviewer", laterData); err != nil {
		t.Fatal(err)
	}
	deleted := later
	deleted.ID = "addr-scene-deleted"
	deleted.Metadata = annotation.Object{"evidence": uploadLater("deleted item evidence", true)}
	deletedData, _ := json.Marshal(deleted)
	if _, err := store.Save(ctx, "item", deleted.ID, 0, "later-reviewer", deletedData); err != nil {
		t.Fatal(err)
	}
	if err := store.Delete(ctx, "item", deleted.ID, 1, "later-reviewer"); err != nil {
		t.Fatal(err)
	}
	dest := filepath.Join(t.TempDir(), "pulled")
	result, err := client.Pull(ctx, "mooroolbark", dest, 2)
	if err != nil {
		t.Fatal(err)
	}
	if result["documents"] != 5 || result["additionalBlobs"] != len(laterAssets) {
		t.Fatalf("pull=%v", result)
	}
	for hash, want := range laterAssets {
		got, err := os.ReadFile(filepath.Join(dest, "blobs", hash))
		if err != nil || !bytes.Equal(got, want) {
			t.Fatalf("later/historical asset missing: %s: %v", hash, err)
		}
	}
	catalog, err := os.ReadFile(filepath.Join(dest, "fieldwork-assets.json"))
	if err != nil || !bytes.Contains(catalog, []byte(Digest([]byte("earlier evidence")))) {
		t.Fatalf("asset catalog lost historical chunk ordering: %v", err)
	}
	state, _ := os.ReadFile(filepath.Join(dest, "fieldwork-state.ndjson"))
	if !strings.Contains(string(state), "A later reviewer edit") {
		t.Fatal("pull lost later editable state")
	}
	pulled, err := OpenBundle(dest)
	if err != nil {
		t.Fatal(err)
	}
	if err := pulled.Verify(ctx); err != nil {
		t.Fatal(err)
	}
	// The documented recovery sequence must work against an empty installation,
	// including historical evidence no longer referenced by any current item.
	recoveryStore, err := annotation.OpenStore(ctx, "sqlite", filepath.Join(t.TempDir(), "recovery.sqlite"), true)
	if err != nil {
		t.Fatal(err)
	}
	defer recoveryStore.Close()
	recoveryHandler, err := annotation.NewHTTPHandler(recoveryStore, annotation.HTTPConfig{Token: token, AssetDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	recoveryServer := httptest.NewServer(recoveryHandler)
	defer recoveryServer.Close()
	recoveryClient, err := NewClient(recoveryServer.URL, token)
	if err != nil {
		t.Fatal(err)
	}
	wantAssets := len(b.Chunks) + 1 + len(laterAssets)
	uploaded, err := recoveryClient.Upload(ctx, pulled, 2, io.Discard)
	if err != nil || uploaded.UploadedAssets != wantAssets || uploaded.ExistingAssets != 0 {
		t.Fatalf("recovery upload=%+v err=%v", uploaded, err)
	}
	if len(pulled.Chunks) != len(b.Chunks) {
		t.Fatal("recovery upload mutated the immutable snapshot inventory")
	}
	imported := 0
	for _, line := range bytes.Split(bytes.TrimSpace(state), []byte("\n")) {
		var entry struct {
			Kind     string            `json:"kind"`
			Document annotation.Object `json:"document"`
		}
		if err := json.Unmarshal(line, &entry); err != nil {
			t.Fatal(err)
		}
		method := map[string]string{"project": "SaveProject", "label": "SaveLabel", "item": "SaveItem"}[entry.Kind]
		if method == "" {
			continue
		}
		if _, err := recoveryClient.call(ctx, method, annotation.Object{entry.Kind: entry.Document, "expectedRevision": 0}); err != nil {
			t.Fatalf("recovery import %s: %v", entry.Kind, err)
		}
		imported++
	}
	if imported != 5 {
		t.Fatalf("recovered %d current documents, want 5", imported)
	}
	for hash, want := range laterAssets {
		var got bytes.Buffer
		if err := recoveryClient.download(ctx, hash, int64(len(want)), &got); err != nil || !bytes.Equal(got.Bytes(), want) {
			t.Fatalf("recovered later/historical asset unavailable: %s: %v", hash, err)
		}
	}
	recovered, err := recoveryClient.call(ctx, "GetItem", annotation.Object{"id": item.ID})
	if err != nil || recovered["item"].(map[string]any)["notes"] != item.Notes {
		t.Fatalf("recovery lost editable state: %v", err)
	}
	if _, err := recoveryClient.call(ctx, "GetItem", annotation.Object{"id": deleted.ID}); err == nil {
		t.Fatal("recovery resurrected deleted item")
	}
	repeated, err := recoveryClient.Upload(ctx, pulled, 2, io.Discard)
	if err != nil || repeated.UploadedAssets != 0 || repeated.ExistingAssets != wantAssets {
		t.Fatalf("recovery resume=%+v err=%v", repeated, err)
	}
	// Immutable source frames may not be rebound by an ordinary item save.
	item.Source["width"] = float64(200)
	data, _ = json.Marshal(item)
	if _, err := store.Save(ctx, "item", item.ID, 2, "test-reviewer", data); err == nil {
		t.Fatal("source frame rebound")
	}
}

func TestUploadRejectsInvalidPullCatalogBeforeNetwork(t *testing.T) {
	for _, damage := range []string{"corrupt", "missing", "foreign-snapshot", "missing-size", "reversed-chunks"} {
		t.Run(damage, func(t *testing.T) {
			b := fixture(t)
			first, second := []byte("later "), []byte("evidence")
			chunks := []Chunk{{Digest(first), int64(len(first))}, {Digest(second), int64(len(second))}}
			assets := []map[string]any{}
			for i, content := range [][]byte{first, second} {
				chunk := chunks[i]
				if err := os.WriteFile(filepath.Join(b.Directory, "blobs", chunk.SHA256), content, 0600); err != nil {
					t.Fatal(err)
				}
				assets = append(assets, map[string]any{"url": "/api/v1/assets/" + chunk.SHA256, "sha256": chunk.SHA256, "bytes": chunk.Bytes})
			}
			file := File{SHA256: Digest([]byte("later evidence")), Bytes: 14, Chunks: chunks}
			catalog := annotation.Object{"schemaVersion": 1, "snapshotId": b.Manifest.SnapshotID, "assets": assets}
			switch damage {
			case "corrupt":
				if err := os.WriteFile(filepath.Join(b.Directory, "blobs", chunks[0].SHA256), []byte("broken"), 0600); err != nil {
					t.Fatal(err)
				}
			case "missing":
				if err := os.Remove(filepath.Join(b.Directory, "blobs", chunks[0].SHA256)); err != nil {
					t.Fatal(err)
				}
			case "foreign-snapshot":
				catalog["snapshotId"] = strings.Repeat("f", 64)
			case "missing-size":
				delete(assets[0], "bytes")
			case "reversed-chunks":
				file.Chunks = []Chunk{chunks[1], chunks[0]}
			}
			catalog["chunkedFiles"] = map[string]File{file.SHA256: file}
			raw, err := json.Marshal(catalog)
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(b.Directory, "fieldwork-assets.json"), raw, 0600); err != nil {
				t.Fatal(err)
			}
			var requests atomic.Int64
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				w.WriteHeader(http.StatusInternalServerError)
			}))
			defer server.Close()
			client, err := NewClient(server.URL, "explicit-local-fixture-token-at-least24")
			if err != nil {
				t.Fatal(err)
			}
			if _, err := client.Upload(context.Background(), b, 2, io.Discard); err == nil {
				t.Fatal("damaged catalog accepted")
			}
			if requests.Load() != 0 {
				t.Fatal("upload began before catalog verification completed")
			}
		})
	}
}

func TestPullReferenceCollectionRejectsIncompleteAssetRefs(t *testing.T) {
	hash := strings.Repeat("c", 64)
	for _, ref := range []map[string]any{
		{"sha256": hash, "url": "https://other.example/api/v1/assets/" + hash},
		{"sha256": hash, "url": "/api/v1/assets/" + hash, "bytes": float64(1.5)},
		{"sha256": hash, "chunks": []any{}},
	} {
		refs := &protectedReferences{assets: map[string]*protectedAsset{}, files: map[string]File{}}
		if err := refs.collect(ref); err == nil {
			t.Fatalf("incomplete reference accepted: %v", ref)
		}
	}
}

func TestRejectsManifestTraversalAndCorruption(t *testing.T) {
	b := fixture(t)
	file := b.Manifest.Files[0]
	if err := os.WriteFile(filepath.Join(b.Directory, "blobs", file.SHA256), []byte("corrupt"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := b.Verify(context.Background()); err == nil {
		t.Fatal("corrupt asset accepted")
	}
	b.Manifest.Files[0].Path = "../outside"
	raw, _ := json.Marshal(b.Manifest)
	os.WriteFile(filepath.Join(b.Directory, "manifest.json"), raw, 0600)
	if _, err := OpenBundle(b.Directory); err == nil {
		t.Fatal("unsafe restoration path accepted")
	}
}

func TestProjectSceneFromLocalFilesMatchesBundleProjection(t *testing.T) {
	ctx := context.Background()
	b := fixture(t)
	restored := filepath.Join(t.TempDir(), "restored")
	if err := b.Restore(ctx, restored); err != nil {
		t.Fatal(err)
	}
	frozen, _, err := b.sceneItem(ctx, "test-scene", "mooroolbark")
	if err != nil {
		t.Fatal(err)
	}
	assets := []string{}
	for _, file := range b.Manifest.Files {
		if strings.HasPrefix(file.Path, "data/scenes/test-scene/") {
			assets = append(assets, file.Path)
		}
	}
	meta := annotation.Object{"schemaVersion": 1, "sceneId": "test-scene", "snapshotId": b.Manifest.SnapshotID, "importRun": b.Manifest.SnapshotID, "importedAt": b.Manifest.CreatedAt, "stale": false}
	local, hasReview, err := ProjectScene(ctx, NewLocalSceneFiles(restored, "test-scene", nil), "mooroolbark", "test-scene", ProjectOptions{Metadata: meta, Tags: []string{"mooroolbark", "imported"}, AssetPaths: assets})
	if err != nil || !hasReview {
		t.Fatalf("local projection: %v", err)
	}
	want, _ := json.Marshal(frozen)
	got, _ := json.Marshal(local)
	if !bytes.Equal(want, got) {
		t.Fatalf("local and frozen projections differ:\n%s\n%s", want, got)
	}
	// Extra bindings stay outside overrides and never touch the frozen shape.
	bound, _, err := ProjectScene(ctx, NewLocalSceneFiles(restored, "test-scene", nil), "mooroolbark", "test-scene", ProjectOptions{ItemID: "custom-id", ExtraBindings: true, Metadata: annotation.Object{"schemaVersion": 2}, Tags: []string{"mooroolbark", "pushed"}})
	if err != nil || bound.ID != "custom-id" || len(bound.Tags) != 3 || bound.Tags[2] != "review" || bound.Metadata["addr"].(annotation.Object)["schemaVersion"] != 2 || len(bound.Source["assets"].([]annotation.Object)) != 0 {
		t.Fatalf("custom projection options ignored: %+v %v", bound, err)
	}
	if _, hasOverrides := bound.Annotations[0].Properties["addr"].(annotation.Object)["overrides"]; hasOverrides {
		t.Fatal("projection wrote overrides")
	}
	if bound.Annotations[0].Properties["addr"].(annotation.Object)["source"] != "machine:example" {
		t.Fatal("machine provenance not copied verbatim")
	}
}

// The real frozen bundle and live mirror, when present, prove the refactored
// projection still reproduces the imported revision-1 documents exactly.
func TestProjectionReproducesLiveFrozenItems(t *testing.T) {
	root := filepath.Join("..", "..", "..")
	bundleDir := filepath.Join(root, "data", "annotation-snapshots", "mooroolbark-23591d058ab7")
	mirrorPath := filepath.Join(root, "data", "fieldwork-sync", "mooroolbark", "mirror.json")
	if _, err := os.Stat(bundleDir); err != nil {
		t.Skip("frozen bundle not present")
	}
	raw, err := os.ReadFile(mirrorPath)
	if err != nil {
		t.Skip("live mirror not present")
	}
	var mirror struct {
		Items []json.RawMessage `json:"items"`
	}
	if err := json.Unmarshal(raw, &mirror); err != nil {
		t.Fatal(err)
	}
	remote := map[string]annotation.Object{}
	for _, item := range mirror.Items {
		var doc annotation.Object
		if err := json.Unmarshal(item, &doc); err != nil {
			t.Fatal(err)
		}
		if doc["revision"] == float64(1) {
			remote[doc["id"].(string)] = doc
		}
	}
	b, err := OpenBundle(bundleDir)
	if err != nil {
		t.Fatal(err)
	}
	checked := 0
	for _, scene := range []string{"house-25822", "mooroolbark-25020", b.Manifest.SceneIDs[0], b.Manifest.SceneIDs[len(b.Manifest.SceneIDs)-1]} {
		if !b.Has("data/scenes/" + scene + "/latest.json") {
			continue
		}
		item, _, err := b.sceneItem(context.Background(), scene, "mooroolbark")
		if err != nil {
			t.Fatal(err)
		}
		want, ok := remote[item.ID]
		if !ok {
			continue
		}
		got, _ := document(item)
		if !equalJSON(comparable(want), comparable(got)) {
			t.Fatalf("scene %s projection differs from the imported revision 1", scene)
		}
		checked++
	}
	if checked == 0 {
		t.Skip("no revision-1 items to compare")
	}
}

func TestGenerationSceneIDShape(t *testing.T) {
	id := GenerationSceneID("mooroolbark", "house-25822", "8657343a6e239a49e35e7da57df4ae804170550cc0ab3cdc0821de63095d5589", 35)
	if id != "addr-scene-house-25822-m8657343a6e23-r35" || !idPattern.MatchString(id) {
		t.Fatalf("id=%s", id)
	}
	if other := GenerationSceneID("other", "s", "abc", 0); other != "other-scene-s-mabc-r0" {
		t.Fatalf("id=%s", other)
	}
}
