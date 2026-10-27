package push

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// The training publisher (scripts/ml/training/publish_artefacts.py) reads
// generation.modelSignature / generation.reviewRevision from push/scenes/<S>.json.
func TestSceneReceiptCarriesLatestGeneration(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "push")
	receipts, err := LoadReceipts(dir)
	if err != nil {
		t.Fatal(err)
	}
	doc := json.RawMessage(`{"id":"gen-1","revision":1}`)
	first := Generation{ModelSignature: strings.Repeat("1", 64), ReviewRevision: 7, ImageSHA256: strings.Repeat("a", 64), Width: 512, Height: 512}
	if err := receipts.RecordCreated(ItemReceipt{ItemID: "gen-1", SceneID: "house-1", RunID: NewRunID(), PushedAt: "t1", Revision: 1, Document: doc, Baseline: doc, Generation: first}); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(filepath.Join(dir, "scenes", "house-1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var onDisk struct {
		LatestItemID string `json:"latestItemId"`
		Generation   struct {
			ModelSignature string `json:"modelSignature"`
			ReviewRevision int    `json:"reviewRevision"`
			ImageSHA256    string `json:"imageSha256"`
		} `json:"generation"`
	}
	if err := json.Unmarshal(raw, &onDisk); err != nil || onDisk.LatestItemID != "gen-1" || onDisk.Generation.ModelSignature != first.ModelSignature || onDisk.Generation.ReviewRevision != 7 || onDisk.Generation.ImageSHA256 != first.ImageSHA256 {
		t.Fatalf("scene receipt lacks the publisher's generation fields: %s (%v)", raw, err)
	}
	second := Generation{ModelSignature: strings.Repeat("2", 64), ReviewRevision: 9, ImageSHA256: strings.Repeat("b", 64), Width: 1024, Height: 1024}
	if err := receipts.RecordCreated(ItemReceipt{ItemID: "gen-2", SceneID: "house-1", RunID: NewRunID(), PushedAt: "t2", Revision: 1, Document: doc, Baseline: doc, Generation: second}); err != nil {
		t.Fatal(err)
	}
	reloaded, err := LoadReceipts(dir)
	if err != nil {
		t.Fatal(err)
	}
	scene, ok := reloaded.Scene("house-1")
	if !ok || scene.LatestItemID != "gen-2" || scene.Generation != second || len(scene.ItemIDs) != 2 {
		t.Fatalf("scene receipt did not follow the latest generation: %+v", scene)
	}
	if _, ok := reloaded.Item("gen-1"); !ok {
		t.Fatal("item receipt lost on reload")
	}
	if items, scenes := reloaded.Len(); items != 2 || scenes != 1 || strings.Join(reloaded.SceneIDs(), ",") != "house-1" || strings.Join(reloaded.ItemIDs(), ",") != "gen-1,gen-2" {
		t.Fatalf("index counts %d/%d %v %v", items, scenes, reloaded.SceneIDs(), reloaded.ItemIDs())
	}
	// A scene receipt written before the field existed still loads (zero Generation).
	legacy := []byte(`{"schemaVersion":1,"sceneId":"legacy","latestItemId":"old-1","itemIds":["old-1"],"updatedAt":"t0"}`)
	if err := os.WriteFile(filepath.Join(dir, "scenes", "legacy.json"), legacy, 0600); err != nil {
		t.Fatal(err)
	}
	reloaded, err = LoadReceipts(dir)
	if err != nil {
		t.Fatal(err)
	}
	if scene, ok := reloaded.Scene("legacy"); !ok || scene.Generation.ModelSignature != "" {
		t.Fatalf("legacy scene receipt: %+v %v", scene, ok)
	}
}

// Apply runs scene workers concurrently (Concurrency > 1): receipt writers and
// OwnWrite/Item readers must not race. Run with -race.
func TestReceiptIndexConcurrentAccessIsGuarded(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "push")
	receipts, err := LoadReceipts(dir)
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for worker := 0; worker < 8; worker++ {
		wg.Add(1)
		go func(worker int) {
			defer wg.Done()
			for i := 0; i < 20; i++ {
				id := fmt.Sprintf("item-%d-%d", worker, i)
				doc := json.RawMessage(fmt.Sprintf(`{"id":%q,"revision":1,"title":"t"}`, id))
				if err := receipts.RecordCreated(ItemReceipt{ItemID: id, SceneID: fmt.Sprintf("scene-%d", worker), RunID: NewRunID(), PushedAt: "now", Revision: 1, Document: doc, Baseline: doc}); err != nil {
					t.Error(err)
					return
				}
				if err := receipts.RecordMark(id, fmt.Sprintf("scene-%d", worker), Mark{RunID: NewRunID(), SupersedeBy: "next", Revision: 2, MarkedAt: "now", Document: doc}); err != nil {
					t.Error(err)
					return
				}
			}
		}(worker)
		wg.Add(1)
		go func(worker int) {
			defer wg.Done()
			for i := 0; i < 20; i++ {
				id := fmt.Sprintf("item-%d-%d", worker, i)
				receipts.OwnWrite(id, 2, json.RawMessage(fmt.Sprintf(`{"id":%q,"revision":2,"title":"t"}`, id)))
				receipts.Item(id)
				receipts.Scene(fmt.Sprintf("scene-%d", worker))
				receipts.ExpectedRevision(id, false)
				receipts.SceneIDs()
				receipts.Len()
			}
		}(worker)
	}
	wg.Wait()
	if items, scenes := receipts.Len(); items != 160 || scenes != 8 {
		t.Fatalf("index counts %d/%d", items, scenes)
	}
}
