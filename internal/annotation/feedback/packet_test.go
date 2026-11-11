package feedback

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/benebsworth/addr/internal/annotation/migration"
	"github.com/benebsworth/addr/internal/annotation/push"
	"github.com/benebsworth/addr/internal/annotationsync"
)

func packetFixture(t *testing.T) (*migration.Bundle, annotationsync.Mirror) {
	t.Helper()
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, "blobs"), 0700); err != nil {
		t.Fatal(err)
	}
	manifest := migration.Manifest{SchemaVersion: 1, SnapshotID: strings.Repeat("a", 64), CreatedAt: "2026-09-27T01:00:00Z", SceneIDs: []string{"scene"}}
	add := func(path string, data []byte) {
		hash := migration.Digest(data)
		if err := os.WriteFile(filepath.Join(dir, "blobs", hash), data, 0600); err != nil {
			t.Fatal(err)
		}
		manifest.Files = append(manifest.Files, migration.File{Path: path, SHA256: hash, Bytes: int64(len(data)), ContentType: "application/json", Chunks: []migration.Chunk{{SHA256: hash, Bytes: int64(len(data))}}})
	}
	image := []byte("original image")
	imageHash := migration.Digest(image)
	model := map[string]any{"id": "scene", "image_sha256": imageHash, "image_width": 1536, "image_height": 1024, "model_signature": strings.Repeat("b", 64), "objects": []any{map[string]any{"id": "roof", "kind": "building", "status": "candidate", "ring": [][]float64{{10, 10}, {40, 10}, {30, 40}}}}, "pixel_to_meters": [][]float64{{1, 0}, {0, -1}, {0, 0}}, "meters_to_pixel": [][]float64{{1, 0}, {0, -1}, {0, 0}}}
	raw, _ := json.Marshal(model)
	add("data/scenes/scene/latest.json", raw)
	add("data/scenes/scene/"+imageHash+".png", image)
	model["model_signature"] = strings.Repeat("c", 64)
	raw, _ = json.Marshal(model)
	add("data/scenes/scene/models/"+strings.Repeat("c", 64)+".json", raw)
	review, _ := json.Marshal(map[string]any{"revision": 3, "model_signature": strings.Repeat("c", 64), "updated_by": "machine:source", "objects": model["objects"]})
	add("data/scenes/scene/review.json", review)
	for _, name := range []string{"processing.json", "processing-map.geojson", "processing-summary.json", "crm.json"} {
		add("data/fieldwork-derived/mooroolbark/"+name, []byte(`{"features":[]}`))
	}
	add("data/suburbs/mooroolbark/boundary.geojson", []byte(`{"features":[]}`))
	raw, _ = json.Marshal(manifest)
	if err := os.WriteFile(filepath.Join(dir, "manifest.json"), raw, 0600); err != nil {
		t.Fatal(err)
	}
	bundle, err := migration.OpenBundle(dir)
	if err != nil {
		t.Fatal(err)
	}
	plan, err := migration.BuildPlan(context.Background(), bundle, "mooroolbark")
	if err != nil {
		t.Fatal(err)
	}
	mirror := annotationsync.Mirror{Origin: "https://fieldwork.example", ProjectID: "mooroolbark", Snapshot: annotationsync.Snapshot{SchemaVersion: 1, Cursor: "7", Labels: []json.RawMessage{}, Items: []json.RawMessage{}}}
	plan.Project.Revision = 1
	mirror.Project, _ = json.Marshal(plan.Project)
	for _, label := range plan.Labels {
		label.Revision = 1
		raw, _ := json.Marshal(label)
		mirror.Labels = append(mirror.Labels, raw)
	}
	for _, item := range plan.Items {
		item.Revision = 1
		item.UpdatedAt = "2026-09-27T01:02:03Z"
		raw, _ := json.Marshal(item)
		mirror.Items = append(mirror.Items, raw)
	}
	return bundle, mirror
}

func editItem(t *testing.T, mirror *annotationsync.Mirror, fields map[string]json.RawMessage) {
	t.Helper()
	var item map[string]json.RawMessage
	if err := json.Unmarshal(mirror.Items[0], &item); err != nil {
		t.Fatal(err)
	}
	for key, value := range fields {
		item[key] = value
	}
	mirror.Items[0], _ = json.Marshal(item)
}

func TestPacketIncludesEditsAndReversionsWithoutLosingRawEvidence(t *testing.T) {
	bundle, mirror := packetFixture(t)
	p, err := Build(context.Background(), mirror, bundle, "", nil)
	if err != nil || len(p.Entries) != 0 || len(p.Quarantined) != 0 {
		t.Fatalf("pristine packet=%+v %v", p, err)
	}
	editItem(t, &mirror, map[string]json.RawMessage{"revision": []byte("2"), "notes": []byte(`"changed note"`), "metadata": []byte(`{"counter":9007199254740993,"coordinate":0.0016998300169983002}`)})
	mirror.ItemProvenance = map[string]annotationsync.Provenance{"addr-scene-scene": {Actor: "workspace", Timestamp: "2026-09-27T02:00:00Z"}}
	p, err = Build(context.Background(), mirror, bundle, "", nil)
	if err != nil || len(p.Entries) != 1 {
		t.Fatalf("edited packet=%+v %v", p, err)
	}
	e := p.Entries[0]
	if e.Item.JSON != string(mirror.Items[0]) || e.Item.SHA256 != migration.Digest(mirror.Items[0]) || !strings.Contains(e.Item.JSON, "9007199254740993") || !strings.Contains(e.Item.JSON, "0.0016998300169983002") {
		t.Fatal("raw item number precision or exact bytes changed")
	}
	if e.Actor != "workspace" || e.Timestamp != "2026-09-27T02:00:00Z" || e.Review == nil || e.ReviewModel == nil {
		t.Fatal("source review model or observed provenance missing")
	}
	if e.Model.SHA256 != bundle.Files["data/scenes/scene/latest.json"].SHA256 {
		t.Fatal("model evidence is not bound to snapshot")
	}
	again, err := Build(context.Background(), mirror, bundle, "", nil)
	if err != nil || again.Digest != p.Digest || len(again.Entries) != 1 {
		t.Fatal("no-op repeat lost unapplied edit or changed packet digest")
	}
	raw, _ := json.Marshal(p)
	if _, err := Read(raw); err != nil {
		t.Fatal(err)
	}
	p.Entries[0].Item.JSON = `{"tampered":true}`
	raw, _ = json.Marshal(p)
	if _, err := Read(raw); err == nil {
		t.Fatal("accepted corrupt evidence")
	}
	_, reverted := packetFixture(t)
	editItem(t, &reverted, map[string]json.RawMessage{"revision": []byte("3")})
	revertPacket, err := Build(context.Background(), reverted, bundle, "", nil)
	if err != nil || len(revertPacket.Entries) != 1 {
		t.Fatal("reverting to frozen content was incorrectly omitted")
	}
}

func TestPacketQuarantinesBindingsAndSupportsSelection(t *testing.T) {
	bundle, mirror := packetFixture(t)
	var clone map[string]json.RawMessage
	json.Unmarshal(mirror.Items[0], &clone)
	clone["id"] = []byte(`"temporary-clone"`)
	raw, _ := json.Marshal(clone)
	mirror.Items = append(mirror.Items, raw)
	p, err := Build(context.Background(), mirror, bundle, "", nil)
	if err != nil || len(p.Quarantined) != 1 || p.Quarantined[0].Reason != "unknown_or_noncanonical_scene" {
		t.Fatalf("clone not quarantined: %+v %v", p, err)
	}
	selected, err := Build(context.Background(), mirror, bundle, "addr-scene-scene", nil)
	if err != nil || len(selected.Quarantined) != 0 || len(selected.Entries) != 0 || selected.Digest == p.Digest {
		t.Fatal("selection did not isolate and bind packet")
	}
	var source map[string]json.RawMessage
	json.Unmarshal(clone["source"], &source)
	source["width"] = []byte("42")
	changed, _ := json.Marshal(source)
	editItem(t, &mirror, map[string]json.RawMessage{"source": changed})
	p, err = Build(context.Background(), mirror, bundle, "addr-scene-scene", nil)
	if err != nil || len(p.Quarantined) != 1 || p.Quarantined[0].Reason != "changed_immutable_source" {
		t.Fatal("changed frame was accepted")
	}
	mirror.Items = mirror.Items[1:]
	p, err = Build(context.Background(), mirror, bundle, "addr-scene-scene", nil)
	if err != nil || len(p.Quarantined) != 1 || p.Quarantined[0].Reason != "missing_remote_scene" {
		t.Fatal("cloud deletion was silently treated as local acceptance")
	}
}

func TestPacketSelectPreservesEvidenceAndRelevantQuarantine(t *testing.T) {
	bundle, mirror := packetFixture(t)
	editItem(t, &mirror, map[string]json.RawMessage{"revision": []byte("2"), "notes": []byte(`"feedback"`)})
	p, err := Build(context.Background(), mirror, bundle, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	p.Quarantined = append(p.Quarantined, Quarantine{"blocked-scene", "changed_immutable_source"})
	p.Digest, _ = p.ComputeDigest()
	before, _ := json.Marshal(p)
	selected, err := p.Select("addr-scene-scene")
	if err != nil || selected.Selection != "addr-scene-scene" || len(selected.Entries) != 1 || len(selected.Quarantined) != 0 || selected.Digest == p.Digest {
		t.Fatalf("selection failed: %+v %v", selected, err)
	}
	if !reflect.DeepEqual(selected.Entries[0], p.Entries[0]) || selected.Origin != p.Origin || selected.ProjectID != p.ProjectID || selected.SnapshotID != p.SnapshotID || selected.ManifestSHA256 != p.ManifestSHA256 || selected.Cursor != p.Cursor {
		t.Fatal("selection changed source evidence or packet bindings")
	}
	raw, _ := json.Marshal(selected)
	if _, err := Read(raw); err != nil {
		t.Fatal(err)
	}
	again, err := selected.Select(selected.Selection)
	if err != nil || again.Digest != selected.Digest {
		t.Fatal("repeated selection changed the plan binding")
	}
	blocked, err := p.Select("blocked-scene")
	if err != nil || len(blocked.Entries) != 0 || !reflect.DeepEqual(blocked.Quarantined, p.Quarantined) {
		t.Fatal("selection hid its own quarantine")
	}
	if _, err := selected.Select("blocked-scene"); err == nil {
		t.Fatal("selection widened an already selected packet")
	}
	if _, err := p.Select("missing-scene"); err == nil {
		t.Fatal("missing item became an empty success")
	}
	after, _ := json.Marshal(p)
	if !bytes.Equal(before, after) {
		t.Fatal("selection changed the original packet")
	}
	_, pristine := packetFixture(t)
	empty, err := Build(context.Background(), pristine, bundle, "addr-scene-scene", nil)
	if err != nil {
		t.Fatal(err)
	}
	selectedEmpty, err := empty.Select(empty.Selection)
	if err != nil || selectedEmpty.Digest != empty.Digest {
		t.Fatal("already bound empty selection could not be reused")
	}
}

func TestPacketRejectsMissingSourceAndPreservesOutputOnCancel(t *testing.T) {
	bundle, mirror := packetFixture(t)
	other := *bundle
	other.ManifestBytes = append(append([]byte(nil), bundle.ManifestBytes...), '\n')
	if _, err := Build(context.Background(), mirror, &other, "", nil); err == nil {
		t.Fatal("accepted a different manifest than the immutable cloud report")
	}
	p, err := Build(context.Background(), mirror, bundle, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "feedback.json")
	if err := Write(context.Background(), path, p); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(path)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	p.Cursor = "9"
	p.Digest, _ = p.ComputeDigest()
	if err := Write(ctx, path, p); err == nil {
		t.Fatal("canceled packet write succeeded")
	}
	after, _ := os.ReadFile(path)
	if !bytes.Equal(before, after) {
		t.Fatal("failed write replaced previous packet")
	}
	model := bundle.Files["data/scenes/scene/latest.json"]
	if err := os.Remove(filepath.Join(bundle.Directory, "blobs", model.SHA256)); err != nil {
		t.Fatal(err)
	}
	if _, err := Build(context.Background(), mirror, bundle, "", nil); err == nil {
		t.Fatal("missing source evidence was accepted")
	}
}

func TestComparisonKeepsExactNumberValues(t *testing.T) {
	if !sameJSON([]byte(`{"n":1.00}`), []byte(`{"n":1e0}`)) {
		t.Fatal("equivalent number spellings differ")
	}
	if sameJSON([]byte(`{"n":9007199254740993}`), []byte(`{"n":9007199254740992}`)) {
		t.Fatal("comparison rounded integer values")
	}
	if sameJSON([]byte(`{"n":0.0016998300169983002}`), []byte(`{"n":0.0016998300169983004}`)) {
		t.Fatal("comparison hid a real decimal change")
	}
}

// receiptFixture builds a push receipt index beside the frozen fixture: the
// frozen item is supersede-marked (revision 2) and one generation item exists
// at revision 1, exactly as after a completed push. It returns the index, the
// generation id and the generation's pristine document.
func receiptFixture(t *testing.T, bundle *migration.Bundle, mirror *annotationsync.Mirror) (*push.ReceiptIndex, string, json.RawMessage) {
	t.Helper()
	receipts, err := push.LoadReceipts(filepath.Join(t.TempDir(), "push"))
	if err != nil {
		t.Fatal(err)
	}
	// Evidence: the same JSON files the push would have uploaded.
	for _, path := range []string{"data/scenes/scene/latest.json", "data/scenes/scene/models/" + strings.Repeat("c", 64) + ".json", "data/scenes/scene/review.json"} {
		data, err := bundle.ReadFile(context.Background(), path)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := receipts.StoreEvidence(data); err != nil {
			t.Fatal(err)
		}
	}
	var frozen map[string]json.RawMessage
	if err := json.Unmarshal(mirror.Items[0], &frozen); err != nil {
		t.Fatal(err)
	}
	var source map[string]json.RawMessage
	json.Unmarshal(frozen["source"], &source)
	generation := push.Generation{ModelSignature: strings.Repeat("b", 64), ReviewRevision: 3, ImageSHA256: migration.Digest([]byte("original image")), Width: 1536, Height: 1024}
	genID := migration.GenerationSceneID("mooroolbark", "scene", generation.ModelSignature, generation.ReviewRevision)
	gen := map[string]json.RawMessage{}
	for k, v := range frozen {
		gen[k] = v
	}
	gen["id"], _ = json.Marshal(genID)
	gen["tags"] = json.RawMessage(`["mooroolbark","pushed","gen","review"]`)
	gen["metadata"] = json.RawMessage(`{"addr":{"schemaVersion":2,"kind":"generation","sceneId":"scene","generation":{"modelSignature":"` + generation.ModelSignature + `","reviewRevision":3},"push":{"runId":"20260929T000000Z-00000001","tool":"annotate push"}}}`)
	gen["createdAt"], gen["updatedAt"] = json.RawMessage(`"2026-09-29T01:00:00Z"`), json.RawMessage(`"2026-09-29T01:00:00Z"`)
	genRaw, _ := json.Marshal(gen)
	runID := "20260929T000000Z-00000001"
	if err := receipts.RecordCreated(push.ItemReceipt{ItemID: genID, SceneID: "scene", RunID: runID, PushedAt: "2026-09-29T01:00:00Z", Revision: 1, Document: genRaw, Baseline: genRaw, Generation: generation, Supersedes: []push.Supersedes{{ItemID: "addr-scene-scene", Revision: 1}}}); err != nil {
		t.Fatal(err)
	}
	// Mark the frozen item the way apply.mark does: tags + metadata only.
	marked := map[string]json.RawMessage{}
	for k, v := range frozen {
		marked[k] = v
	}
	var tags []string
	json.Unmarshal(frozen["tags"], &tags)
	marked["tags"], _ = json.Marshal(append(tags, push.TagSuperseded))
	var meta map[string]any
	json.Unmarshal(frozen["metadata"], &meta)
	addr := meta["addr"].(map[string]any)
	addr["supersededBy"] = map[string]any{"itemId": genID, "runId": runID}
	addr["stale"] = true
	marked["metadata"], _ = json.Marshal(meta)
	marked["revision"] = json.RawMessage("2")
	marked["updatedAt"] = json.RawMessage(`"2026-09-29T01:00:05Z"`)
	markedRaw, _ := json.Marshal(marked)
	if err := receipts.RecordMark("addr-scene-scene", "scene", push.Mark{RunID: runID, SupersedeBy: genID, Revision: 2, MarkedAt: "2026-09-29T01:00:05Z", Document: markedRaw}); err != nil {
		t.Fatal(err)
	}
	// Items[0] is the frozen scene item; the dataset report stays in the mirror.
	mirror.Items = append([]json.RawMessage{markedRaw}, mirror.Items[1:]...)
	mirror.Items = append(mirror.Items, genRaw)
	return receipts, genID, genRaw
}

func editMirrorItem(t *testing.T, mirror *annotationsync.Mirror, id string, fields map[string]json.RawMessage) json.RawMessage {
	t.Helper()
	for i, raw := range mirror.Items {
		var item map[string]json.RawMessage
		if err := json.Unmarshal(raw, &item); err != nil {
			t.Fatal(err)
		}
		if string(item["id"]) != `"`+id+`"` {
			continue
		}
		for key, value := range fields {
			item[key] = value
		}
		mirror.Items[i], _ = json.Marshal(item)
		return mirror.Items[i]
	}
	t.Fatalf("mirror item %s not found", id)
	return nil
}

func TestPacketUsesPushReceiptsAsBaselineAndSkipsOwnWrites(t *testing.T) {
	bundle, mirror := packetFixture(t)
	receipts, genID, genRaw := receiptFixture(t, bundle, &mirror)
	// A push followed by a pull: both own writes are recognised, nothing is delivered.
	p, err := Build(context.Background(), mirror, bundle, "", receipts)
	if err != nil || p.SchemaVersion != SchemaWithReceipts || len(p.Entries) != 0 || len(p.Quarantined) != 0 {
		t.Fatalf("post-push packet=%+v %v", p, err)
	}
	raw, _ := json.Marshal(p)
	if _, err := Read(raw); err != nil {
		t.Fatal(err)
	}
	// A reviewer edit on the generation item is baselined on the receipt document
	// and evidenced from push/assets, with the pushed frame in the binding.
	edited := editMirrorItem(t, &mirror, genID, map[string]json.RawMessage{"revision": []byte("2"), "notes": []byte(`"reviewer note"`)})
	mirror.ItemProvenance = map[string]annotationsync.Provenance{genID: {Actor: "workspace", Timestamp: "2026-09-29T02:00:00Z"}}
	p, err = Build(context.Background(), mirror, bundle, "", receipts)
	if err != nil || len(p.Entries) != 1 || len(p.Quarantined) != 0 {
		t.Fatalf("generation edit packet=%+v %v", p, err)
	}
	e := p.Entries[0]
	if e.ItemID != genID || e.SceneID != "scene" || e.BaseItem.JSON != string(genRaw) || e.Item.JSON != string(edited) || e.Actor != "workspace" {
		t.Fatalf("generation entry not baselined on the receipt: %+v", e)
	}
	if e.Binding == nil || e.Binding.Kind != BindingPush || e.Binding.RunID != "20260929T000000Z-00000001" || e.Binding.ReceiptRevision != 1 || len(e.Binding.ReceiptSHA256) != 64 {
		t.Fatalf("binding: %+v", e.Binding)
	}
	if e.Binding.Generation == nil || e.Binding.Generation.ModelSignature != strings.Repeat("b", 64) || e.Binding.Generation.ImageSHA256 != migration.Digest([]byte("original image")) || e.Binding.Generation.Width != 1536 || e.Binding.Generation.Height != 1024 || e.Binding.Generation.ReviewRevision != 3 {
		t.Fatalf("binding frame: %+v", e.Binding.Generation)
	}
	if e.Model.SHA256 != bundle.Files["data/scenes/scene/latest.json"].SHA256 || e.Review == nil || e.Review.SHA256 != bundle.Files["data/scenes/scene/review.json"].SHA256 || e.ReviewModel == nil {
		t.Fatalf("evidence not read from push receipts: %+v", e)
	}
	raw, _ = json.Marshal(p)
	reread, err := Read(raw)
	if err != nil || reread.Digest != p.Digest || reread.Entries[0].Binding.Kind != BindingPush {
		t.Fatalf("schema-2 packet did not round-trip: %v", err)
	}
	// A reviewer edit on the marked frozen item keeps the frozen projection as
	// baseline (TS strips the mark via its metadata allowlist) and binds to the mark receipt.
	editMirrorItem(t, &mirror, "addr-scene-scene", map[string]json.RawMessage{"revision": []byte("3"), "notes": []byte(`"frozen note"`)})
	p, err = Build(context.Background(), mirror, bundle, "", receipts)
	if err != nil || len(p.Entries) != 2 {
		t.Fatalf("frozen edit packet=%+v %v", p, err)
	}
	frozenEntry := p.Entries[0]
	legacy, err := Build(context.Background(), mirror, bundle, "addr-scene-scene", nil)
	if err != nil || len(legacy.Entries) != 1 {
		t.Fatalf("schema-1 frozen entry: %+v %v", legacy, err)
	}
	if frozenEntry.ItemID != "addr-scene-scene" || frozenEntry.BaseItem != legacy.Entries[0].BaseItem || strings.Contains(frozenEntry.BaseItem.JSON, push.TagSuperseded) || frozenEntry.Binding == nil || frozenEntry.Binding.Kind != BindingPush || frozenEntry.Binding.ReceiptRevision != 2 || frozenEntry.Binding.Generation == nil || frozenEntry.Binding.Generation.ModelSignature != strings.Repeat("b", 64) || frozenEntry.Binding.Generation.ReviewRevision != 3 {
		t.Fatalf("marked frozen entry: %+v", frozenEntry)
	}
	// Selection by generation id works, and the packet is stable across repeats.
	selected, err := Build(context.Background(), mirror, bundle, genID, receipts)
	if err != nil || len(selected.Entries) != 1 || selected.Entries[0].ItemID != genID || len(selected.Quarantined) != 0 {
		t.Fatalf("selection by generation id: %+v %v", selected, err)
	}
	again, err := Build(context.Background(), mirror, bundle, "", receipts)
	if err != nil || again.Digest != p.Digest {
		t.Fatal("repeat build changed the digest")
	}
}

func TestPacketOwnWriteWithEmbeddedUnpulledEditIsDelivered(t *testing.T) {
	bundle, mirror := packetFixture(t)
	receipts, _, _ := receiptFixture(t, bundle, &mirror)
	// --allow-unrecovered: the mark was written over a Fieldwork edit, so our
	// own write at revision 2 embeds "unpulled note". OwnWrite is true, but the
	// document carries more than baseline + mark: it must still be delivered.
	embedded := editMirrorItem(t, &mirror, "addr-scene-scene", map[string]json.RawMessage{"notes": []byte(`"unpulled note"`)})
	if err := receipts.RecordMark("addr-scene-scene", "scene", push.Mark{RunID: "20260929T000000Z-00000002", SupersedeBy: "x", Revision: 2, MarkedAt: "t", Document: embedded}); err != nil {
		t.Fatal(err)
	}
	if !receipts.OwnWrite("addr-scene-scene", 2, embedded) {
		t.Fatal("fixture: the embedded document must be our own write")
	}
	p, err := Build(context.Background(), mirror, bundle, "", receipts)
	if err != nil || len(p.Entries) != 1 || p.Entries[0].ItemID != "addr-scene-scene" || p.Entries[0].Binding.Kind != BindingPush {
		t.Fatalf("embedded Fieldwork edit was swallowed by the own-write skip: %+v %v", p, err)
	}
}

func TestPacketQuarantinesMissingReceiptAndBindingMismatch(t *testing.T) {
	bundle, mirror := packetFixture(t)
	receipts, genID, genRaw := receiptFixture(t, bundle, &mirror)
	// A generation id nobody has a receipt for (other machine, lost receipts).
	var stray map[string]json.RawMessage
	json.Unmarshal(genRaw, &stray)
	strayID := migration.GenerationSceneID("mooroolbark", "scene", strings.Repeat("e", 64), 4)
	stray["id"], _ = json.Marshal(strayID)
	strayRaw, _ := json.Marshal(stray)
	mirror.Items = append(mirror.Items, strayRaw)
	p, err := Build(context.Background(), mirror, bundle, "", receipts)
	if err != nil || len(p.Entries) != 0 || len(p.Quarantined) != 1 || p.Quarantined[0].ItemID != strayID || p.Quarantined[0].Reason != ReasonMissingPushReceipt {
		t.Fatalf("stray generation: %+v %v", p, err)
	}
	// Without receipts the same mirror keeps today's schema-1 behaviour: both
	// generation ids are unknown and the supersede-marked frozen item is an entry.
	legacy, err := Build(context.Background(), mirror, bundle, "", nil)
	if err != nil || legacy.SchemaVersion != SchemaFrozenOnly || len(legacy.Quarantined) != 2 || len(legacy.Entries) != 1 || legacy.Entries[0].ItemID != "addr-scene-scene" || legacy.Entries[0].Binding != nil {
		t.Fatalf("schema-1 packet: entries=%d quarantined=%+v %v", len(legacy.Entries), legacy.Quarantined, err)
	}
	for _, q := range legacy.Quarantined {
		if q.Reason != "unknown_or_noncanonical_scene" {
			t.Fatalf("schema-1 reason changed: %+v", q)
		}
	}
	mirror.Items = mirror.Items[:len(mirror.Items)-1] // drop the stray
	// The remote source of a receipted generation differs from the receipt baseline.
	var source map[string]json.RawMessage
	json.Unmarshal(stray["source"], &source)
	source["width"] = []byte("42")
	changed, _ := json.Marshal(source)
	editMirrorItem(t, &mirror, genID, map[string]json.RawMessage{"source": changed, "revision": []byte("2")})
	p, err = Build(context.Background(), mirror, bundle, "", receipts)
	if err != nil || len(p.Entries) != 0 || len(p.Quarantined) != 1 || p.Quarantined[0].Reason != ReasonReceiptBindingMismatch {
		t.Fatalf("source drift: %+v %v", p, err)
	}
	// A receipt whose scene receipt does not name the item is a binding mismatch too.
	_, mirror2 := packetFixture(t)
	receipts2, genID2, _ := receiptFixture(t, bundle, &mirror2)
	if err := receipts2.WriteScene(push.SceneReceipt{SceneID: "scene", LatestItemID: "other", ItemIDs: []string{"other"}}); err != nil {
		t.Fatal(err)
	}
	editMirrorItem(t, &mirror2, genID2, map[string]json.RawMessage{"revision": []byte("2"), "notes": []byte(`"n"`)})
	p, err = Build(context.Background(), mirror2, bundle, "", receipts2)
	if err != nil || len(p.Quarantined) != 1 || p.Quarantined[0].ItemID != genID2 || p.Quarantined[0].Reason != ReasonReceiptBindingMismatch {
		t.Fatalf("unnamed generation: %+v %v", p, err)
	}
	// Missing push evidence is an error, never silently accepted.
	_, mirror3 := packetFixture(t)
	receipts3, genID3, _ := receiptFixture(t, bundle, &mirror3)
	editMirrorItem(t, &mirror3, genID3, map[string]json.RawMessage{"revision": []byte("2"), "notes": []byte(`"n"`)})
	if err := os.Remove(filepath.Join(receipts3.Dir, "assets", bundle.Files["data/scenes/scene/latest.json"].SHA256)); err != nil {
		t.Fatal(err)
	}
	if _, err := Build(context.Background(), mirror3, bundle, "", receipts3); err == nil || !strings.Contains(err.Error(), "model evidence") {
		t.Fatalf("missing push evidence accepted: %v", err)
	}
}

func TestPacketFrozenIdsUnchangedWithReceiptsPresent(t *testing.T) {
	bundle, mirror := packetFixture(t)
	// Receipts exist for another scene only; this frozen id has none.
	receipts, err := push.LoadReceipts(filepath.Join(t.TempDir(), "push"))
	if err != nil {
		t.Fatal(err)
	}
	other := json.RawMessage(`{"id":"addr-scene-other-m000000000000-r1","revision":1}`)
	if err := receipts.RecordCreated(push.ItemReceipt{ItemID: "addr-scene-other-m000000000000-r1", SceneID: "other", RunID: "20260929T000000Z-00000003", PushedAt: "t", Revision: 1, Document: other, Baseline: other}); err != nil {
		t.Fatal(err)
	}
	p, err := Build(context.Background(), mirror, bundle, "", receipts)
	if err != nil || p.SchemaVersion != SchemaWithReceipts || len(p.Entries) != 0 || len(p.Quarantined) != 0 {
		t.Fatalf("pristine frozen with receipts present: %+v %v", p, err)
	}
	editItem(t, &mirror, map[string]json.RawMessage{"revision": []byte("2"), "notes": []byte(`"changed note"`)})
	p, err = Build(context.Background(), mirror, bundle, "", receipts)
	if err != nil || len(p.Entries) != 1 || p.Entries[0].Binding == nil || p.Entries[0].Binding.Kind != BindingFrozen || p.Entries[0].Binding.RunID != "" || p.Entries[0].Binding.Generation != nil {
		t.Fatalf("frozen entry binding: %+v %v", p.Entries, err)
	}
	legacy, err := Build(context.Background(), mirror, bundle, "", nil)
	if err != nil || legacy.SchemaVersion != SchemaFrozenOnly || len(legacy.Entries) != 1 || legacy.Entries[0].Binding != nil {
		t.Fatalf("schema-1 entry grew a binding: %+v %v", legacy.Entries, err)
	}
	// The frozen evidence and item bytes are identical in both schemas.
	if legacy.Entries[0].Item.SHA256 != p.Entries[0].Item.SHA256 || legacy.Entries[0].BaseItem.SHA256 != p.Entries[0].BaseItem.SHA256 || legacy.Entries[0].Model.SHA256 != p.Entries[0].Model.SHA256 {
		t.Fatal("receipts changed frozen evidence")
	}
	// Revert to frozen content at a later revision is still delivered; frozen
	// quarantines are unchanged.
	_, reverted := packetFixture(t)
	editItem(t, &reverted, map[string]json.RawMessage{"revision": []byte("3")})
	if rp, err := Build(context.Background(), reverted, bundle, "", receipts); err != nil || len(rp.Entries) != 1 {
		t.Fatal("reverting to frozen content was omitted with receipts present")
	}
	var clone map[string]json.RawMessage
	json.Unmarshal(mirror.Items[0], &clone)
	var source map[string]json.RawMessage
	json.Unmarshal(clone["source"], &source)
	source["width"] = []byte("42")
	changed, _ := json.Marshal(source)
	editItem(t, &mirror, map[string]json.RawMessage{"source": changed})
	if qp, err := Build(context.Background(), mirror, bundle, "", receipts); err != nil || len(qp.Quarantined) != 1 || qp.Quarantined[0].Reason != "changed_immutable_source" {
		t.Fatalf("frozen source quarantine changed: %+v %v", qp, err)
	}
	mirror.Items = nil
	if qp, err := Build(context.Background(), mirror, bundle, "", receipts); err == nil && (len(qp.Quarantined) != 1 || qp.Quarantined[0].Reason != "missing_remote_scene") {
		t.Fatalf("missing frozen scene: %+v", qp)
	}
}

func TestDigestIncludesBinding(t *testing.T) {
	bundle, mirror := packetFixture(t)
	receipts, genID, _ := receiptFixture(t, bundle, &mirror)
	editMirrorItem(t, &mirror, genID, map[string]json.RawMessage{"revision": []byte("2"), "notes": []byte(`"n"`)})
	p, err := Build(context.Background(), mirror, bundle, "", receipts)
	if err != nil || len(p.Entries) != 1 {
		t.Fatal(err)
	}
	original := p.Digest
	p.Entries[0].Binding.Kind = BindingFrozen
	kindChanged, _ := p.ComputeDigest()
	p.Entries[0].Binding.Kind = BindingPush
	p.Entries[0].Binding.ReceiptSHA256 = strings.Repeat("0", 64)
	shaChanged, _ := p.ComputeDigest()
	if kindChanged == original || shaChanged == original || kindChanged == shaChanged {
		t.Fatal("binding kind or receipt sha is not part of the schema-2 digest")
	}
	// Run id and frame are informational: not part of the digest.
	p.Entries[0].Binding.ReceiptSHA256 = strings.Repeat("1", 64)
	base, _ := p.ComputeDigest()
	p.Entries[0].Binding.RunID = "20260929T000000Z-ffffffff"
	p.Entries[0].Binding.Generation.Width = 7
	informational, _ := p.ComputeDigest()
	if informational != base {
		t.Fatal("informational binding fields changed the digest")
	}
	// A tampered binding fails Read; a schema-1 packet with a binding fails Read;
	// a schema-2 entry without a binding fails Read.
	raw, _ := json.Marshal(p)
	if _, err := Read(raw); err == nil {
		t.Fatal("digest accepted a changed binding")
	}
	p.Digest, _ = p.ComputeDigest()
	raw, _ = json.Marshal(p)
	if _, err := Read(raw); err != nil {
		t.Fatal(err)
	}
	p.Entries[0].Binding = nil
	p.Digest, _ = p.ComputeDigest()
	raw, _ = json.Marshal(p)
	if _, err := Read(raw); err == nil {
		t.Fatal("schema-2 entry without a binding accepted")
	}
	legacy, err := Build(context.Background(), mirror, bundle, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	// Schema 1 ignores bindings in the digest and rejects them in Read.
	legacy.Entries = append(legacy.Entries, Entry{})
	legacy.Entries = legacy.Entries[:len(legacy.Entries)-1]
	withBinding := *legacy
	withBinding.Entries = append([]Entry{}, legacy.Entries...)
	if len(withBinding.Entries) > 0 {
		withBinding.Entries[0].Binding = &Binding{Kind: BindingFrozen}
		d1, _ := legacy.ComputeDigest()
		d2, _ := withBinding.ComputeDigest()
		if d1 != d2 {
			t.Fatal("schema-1 digest must ignore bindings")
		}
		raw, _ = json.Marshal(&withBinding)
		if _, err := Read(raw); err == nil {
			t.Fatal("schema-1 packet with a binding accepted")
		}
	}
}

func TestStripMarkIsSymmetricAndKeepsNumbers(t *testing.T) {
	marked := json.RawMessage(`{"id":"x","tags":["mooroolbark","imported","review","superseded-frame"],"metadata":{"addr":{"schemaVersion":1,"stale":true,"supersededBy":{"itemId":"y"},"coordinate":0.0016998300169983002}},"notes":""}`)
	base := json.RawMessage(`{"id":"x","tags":["mooroolbark","imported","review"],"metadata":{"addr":{"schemaVersion":1,"stale":false,"coordinate":0.0016998300169983002}},"notes":""}`)
	if !sameDocument(stripMark(marked), stripMark(base)) {
		t.Fatalf("mark not stripped: %s vs %s", stripMark(marked), stripMark(base))
	}
	if !strings.Contains(string(stripMark(marked)), "0.0016998300169983002") {
		t.Fatal("stripMark rewrote a number")
	}
	onlyMark := json.RawMessage(`{"id":"x","tags":["superseded-frame"],"metadata":{"addr":{"stale":true,"supersededBy":{"itemId":"y"}}}}`)
	if !sameDocument(stripMark(onlyMark), stripMark(json.RawMessage(`{"id":"x","tags":[]}`))) {
		t.Fatalf("empty containers not normalised: %s", stripMark(onlyMark))
	}
	edited := json.RawMessage(`{"id":"x","tags":["mooroolbark","imported","review","superseded-frame"],"metadata":{"addr":{"schemaVersion":1,"stale":true,"supersededBy":{"itemId":"y"}}},"notes":"fieldwork note"}`)
	if sameDocument(stripMark(edited), stripMark(base)) {
		t.Fatal("stripMark hid a real edit")
	}
}
