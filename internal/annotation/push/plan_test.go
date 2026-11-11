package push

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/benebsworth/addr/internal/annotation"
	"github.com/benebsworth/addr/internal/annotation/migration"
)

func headNever(t *testing.T) func(context.Context, string, int64) (bool, error) {
	return func(context.Context, string, int64) (bool, error) {
		t.Fatal("network HEAD before local gates")
		return false, nil
	}
}

func headAbsent(context.Context, string, int64) (bool, error) { return false, nil }

func requestOf(t *testing.T, receipts *ReceiptIndex, plan *Plan, itemID string) annotation.Item {
	t.Helper()
	raw, err := ReadRequest(receipts.Dir, plan.RunID, itemID)
	if err != nil {
		t.Fatal(err)
	}
	var item annotation.Item
	if err := json.Unmarshal(raw, &item); err != nil {
		t.Fatal(err)
	}
	return item
}

func TestPlanClassifiesCreateCurrentAndOutsideInventory(t *testing.T) {
	root := t.TempDir()
	// frozen-scene: frozen at old signature/review 1, local now at new signature/review 35.
	writeScene(t, root, fixtureScene{id: "frozen-scene", signature: strings.Repeat("0", 64), reviewRevision: 1, updatedBy: "machine:decision-policy-v2", pixelSeed: 1})
	_, frozenRaw := frozenDocument(t, root, "frozen-scene")
	writeScene(t, root, fixtureScene{id: "frozen-scene", signature: strings.Repeat("b", 64), reviewRevision: 35, history: true, expansion: true, pixelSeed: 2})
	// current-scene: frozen equals local.
	writeScene(t, root, fixtureScene{id: "current-scene", signature: strings.Repeat("c", 64), reviewRevision: 4})
	_, currentRaw := frozenDocument(t, root, "current-scene")
	// outside-scene: not in the frozen inventory, no review.
	writeScene(t, root, fixtureScene{id: "outside-scene", signature: strings.Repeat("d", 64)})
	receipts, err := LoadReceipts(filepath.Join(t.TempDir(), "push"))
	if err != nil {
		t.Fatal(err)
	}
	mirror := testMirror(frozenRaw, currentRaw)
	plan, err := BuildPlan(context.Background(), Inputs{Root: root, Project: testProject, Mirror: mirror, Receipts: receipts, Head: headAbsent, MarkSuperseded: true})
	if err != nil {
		t.Fatal(err)
	}
	if plan.Counts[ActionCreate] != 2 || plan.Counts[ActionCurrent] != 1 || len(plan.Scenes) != 3 || plan.PlanHash == "" || plan.MirrorCursor != "9" {
		t.Fatalf("counts=%v scenes=%d", plan.Counts, len(plan.Scenes))
	}
	byScene := map[string]ScenePlan{}
	for _, sp := range plan.Scenes {
		byScene[sp.SceneID] = sp
	}
	frozen := byScene["frozen-scene"]
	if frozen.Action != ActionCreate || frozen.ItemID != "addr-scene-frozen-scene-mbbbbbbbbbbbb-r35" || frozen.Inventory != InventoryFrozen || len(frozen.Supersedes) != 1 || frozen.Supersedes[0].ItemID != "addr-scene-frozen-scene" || frozen.Supersedes[0].Revision != 1 {
		t.Fatalf("frozen scene plan: %+v", frozen)
	}
	if len(frozen.Assets) != 5 || frozen.Assets[0].Exists || frozen.DocumentBytes == 0 || frozen.Annotations != 2 {
		t.Fatalf("frozen assets: %+v", frozen.Assets)
	}
	item := requestOf(t, receipts, plan, frozen.ItemID)
	addr := item.Metadata["addr"].(map[string]any)
	generation := addr["generation"].(map[string]any)
	if addr["schemaVersion"] != float64(2) || addr["kind"] != "generation" || generation["reviewRevision"] != float64(35) || generation["actorKind"] != "local" || generation["reviewActor"] != "local" || generation["contextExpansion"] == nil || generation["inventory"] != InventoryFrozen || addr["sourceReviewRevision"] != float64(35) {
		t.Fatalf("generation metadata: %v", addr)
	}
	if push := addr["push"].(map[string]any); push["runId"] != plan.RunID || push["tool"] != "annotate push" || push["mirrorCursor"] != "9" {
		t.Fatalf("push metadata: %v", push)
	}
	if strings.Join(item.Tags, ",") != "mooroolbark,pushed,gen,review,expanded-1024" || item.Status != "in_review" || item.Source["review"] == nil || item.Source["reviewModel"] == nil || item.Source["confidence"] == nil {
		t.Fatalf("tags/status/source: %v %s %v", item.Tags, item.Status, item.Source)
	}
	if _, has := item.Source["analysisRefs"]; has {
		t.Fatal("frozen-only analysisRefs leaked into a generation")
	}
	if len(item.Source["assets"].([]any)) != 5 {
		t.Fatalf("assets ref list: %v", item.Source["assets"])
	}
	outside := byScene["outside-scene"]
	if outside.Action != ActionCreate || outside.Inventory != InventoryOutside || len(outside.Supersedes) != 0 || outside.ItemID != "addr-scene-outside-scene-mdddddddddddd-r0" {
		t.Fatalf("outside scene plan: %+v", outside)
	}
	outsideItem := requestOf(t, receipts, plan, outside.ItemID)
	if strings.Join(outsideItem.Tags, ",") != "mooroolbark,pushed,gen,model,outside-inventory" || outsideItem.Status != "unreviewed" {
		t.Fatalf("outside tags: %v", outsideItem.Tags)
	}
	current := byScene["current-scene"]
	if current.Action != ActionCurrent || current.ItemID != "addr-scene-current-scene" {
		t.Fatalf("current scene plan: %+v", current)
	}
	if _, err := os.Stat(requestPath(receipts.Dir, plan.RunID, "addr-scene-current-scene")); err == nil {
		t.Fatal("current scene must not produce a request document")
	}
	// --skip-outside-inventory omits the scene entirely.
	skipped, err := BuildPlan(context.Background(), Inputs{Root: root, Project: testProject, Mirror: mirror, Receipts: receipts, Head: headAbsent, SkipOutsideInventory: true})
	if err != nil || len(skipped.Scenes) != 2 || skipped.Counts[ActionCreate] != 1 {
		t.Fatalf("skip outside: %v %v", skipped.Counts, err)
	}
	// --scenes selects a subset.
	subset, err := BuildPlan(context.Background(), Inputs{Root: root, Project: testProject, Mirror: mirror, Receipts: receipts, Head: headAbsent, Scenes: []string{"outside-scene"}})
	if err != nil || len(subset.Scenes) != 1 || subset.Scenes[0].SceneID != "outside-scene" {
		t.Fatalf("scene selection: %v", err)
	}
}

func TestPlanBlocksRemoteEditsUnlessLocalFeedbackReceiptMatches(t *testing.T) {
	root := t.TempDir()
	writeScene(t, root, fixtureScene{id: "edited", signature: strings.Repeat("0", 64), reviewRevision: 1, pixelSeed: 1})
	frozen, _ := frozenDocument(t, root, "edited")
	frozen.Revision = 2
	frozen.Notes = "A Fieldwork reviewer note"
	editedRaw, _ := json.Marshal(frozen)
	writeScene(t, root, fixtureScene{id: "edited", signature: strings.Repeat("b", 64), reviewRevision: 2, pixelSeed: 2})
	receipts, _ := LoadReceipts(filepath.Join(t.TempDir(), "push"))
	mirror := testMirror(editedRaw)
	feedbackRoot := t.TempDir()
	in := Inputs{Root: root, Project: testProject, Mirror: mirror, Receipts: receipts, FeedbackReceiptRoot: feedbackRoot, Head: headNever(t)}
	plan, err := BuildPlan(context.Background(), in)
	if err != nil {
		t.Fatal(err)
	}
	sp := plan.Scenes[0]
	if sp.Action != ActionBlockedRemoteEdits || !strings.Contains(strings.Join(sp.Reasons, " "), "addr-scene-edited is at revision 2") || !strings.Contains(strings.Join(sp.Reasons, " "), RemoteEditsHint) {
		t.Fatalf("expected blocked_remote_edits: %+v", sp)
	}
	if _, err := os.Stat(requestPath(receipts.Dir, plan.RunID, sp.ItemID)); err == nil {
		t.Fatal("blocked scene wrote a request document")
	}
	// A local FeedbackReceipt for exactly this mirror document unblocks the push.
	receiptPath := filepath.Join(feedbackRoot, migration.Digest([]byte(mirror.Origin)), testProject, "receipts", migration.Digest([]byte("addr-scene-edited"))+".json")
	writeJSON(t, receiptPath, annotation.Object{"schemaVersion": 1, "itemId": "addr-scene-edited", "remote": annotation.Object{"sha256": migration.Digest(editedRaw)}})
	in.Head = headAbsent
	plan, err = BuildPlan(context.Background(), in)
	if err != nil || plan.Scenes[0].Action != ActionCreate || plan.Scenes[0].LiveItems[0].Recovered != "feedback-receipt" || len(plan.Scenes[0].Unrecovered) != 0 {
		t.Fatalf("feedback receipt did not unblock: %+v %v", plan.Scenes[0], err)
	}
	// A receipt for a different document does not.
	writeJSON(t, receiptPath, annotation.Object{"itemId": "addr-scene-edited", "remote": annotation.Object{"sha256": strings.Repeat("f", 64)}})
	plan, err = BuildPlan(context.Background(), in)
	if err != nil || plan.Scenes[0].Action != ActionBlockedRemoteEdits {
		t.Fatalf("stale receipt unblocked: %+v", plan.Scenes[0])
	}
	// --allow-unrecovered pushes anyway and records the unpulled edit.
	in.AllowUnrecovered = true
	plan, err = BuildPlan(context.Background(), in)
	if err != nil || plan.Scenes[0].Action != ActionCreate || len(plan.Scenes[0].Unrecovered) != 1 || plan.Scenes[0].Unrecovered[0].Revision != 2 {
		t.Fatalf("allow-unrecovered: %+v", plan.Scenes[0])
	}
	item := requestOf(t, receipts, plan, plan.Scenes[0].ItemID)
	unrecovered := item.Metadata["addr"].(map[string]any)["unrecoveredFieldwork"].([]any)
	if len(unrecovered) != 1 || unrecovered[0].(map[string]any)["itemId"] != "addr-scene-edited" {
		t.Fatalf("unrecoveredFieldwork missing: %v", item.Metadata)
	}
}

func TestPlanBlocksStaleReviewAndLock(t *testing.T) {
	root := t.TempDir()
	writeScene(t, root, fixtureScene{id: "stale", signature: strings.Repeat("b", 64), reviewSignature: strings.Repeat("e", 64), reviewRevision: 3})
	writeScene(t, root, fixtureScene{id: "locked", signature: strings.Repeat("b", 64), reviewRevision: 3})
	if err := os.WriteFile(filepath.Join(root, "data", "scenes", "locked", "review.lock"), []byte("pid"), 0600); err != nil {
		t.Fatal(err)
	}
	receipts, _ := LoadReceipts(filepath.Join(t.TempDir(), "push"))
	plan, err := BuildPlan(context.Background(), Inputs{Root: root, Project: testProject, Mirror: testMirror(), Receipts: receipts, Head: headNever(t)})
	if err != nil {
		t.Fatal(err)
	}
	if plan.Counts[ActionBlockedStaleReview] != 1 || plan.Counts[ActionBlockedReviewLock] != 1 {
		t.Fatalf("counts=%v", plan.Counts)
	}
	for _, sp := range plan.Scenes {
		if sp.SceneID == "stale" && !strings.Contains(sp.Reasons[0], "Carry edits forward") {
			t.Fatalf("stale hint missing: %v", sp.Reasons)
		}
	}
}

func TestPlanRejectsInvalidFrameAndOversizeBeforeNetwork(t *testing.T) {
	root := t.TempDir()
	objects := []annotation.Object{}
	for i := 0; i < 6000; i++ {
		objects = append(objects, annotation.Object{"id": "o" + string(rune('a'+i%26)) + itoa(i), "kind": "tree", "status": "candidate", "ring": [][]float64{{1, 1}, {2, 1}, {2, 2}}})
	}
	writeScene(t, root, fixtureScene{id: "huge", objects: objects})
	sha := writeScene(t, root, fixtureScene{id: "tampered"})
	png := filepath.Join(root, "data", "scenes", "tampered", sha+".png")
	data, _ := os.ReadFile(png)
	data[len(data)-1] ^= 0xff
	if err := os.WriteFile(png, data, 0600); err != nil {
		t.Fatal(err)
	}
	writeScene(t, root, fixtureScene{id: "wrong-size", width: 64, height: 64})
	// Claim a different frame than the PNG carries.
	latest := filepath.Join(root, "data", "scenes", "wrong-size", "latest.json")
	raw, _ := os.ReadFile(latest)
	raw = []byte(strings.Replace(string(raw), `"image_width":64`, `"image_width":128`, 1))
	if err := os.WriteFile(latest, raw, 0600); err != nil {
		t.Fatal(err)
	}
	receipts, _ := LoadReceipts(filepath.Join(t.TempDir(), "push"))
	plan, err := BuildPlan(context.Background(), Inputs{Root: root, Project: testProject, Mirror: testMirror(), Receipts: receipts, Head: headNever(t)})
	if err != nil {
		t.Fatal(err)
	}
	if plan.Counts[ActionTooLarge] != 1 || plan.Counts[ActionInvalidFrame] != 2 {
		t.Fatalf("counts=%v", plan.Counts)
	}
	for _, sp := range plan.Scenes {
		switch sp.SceneID {
		case "huge":
			if sp.Annotations != 6000 || sp.DocumentBytes == 0 || !strings.Contains(sp.Reasons[0], "6000 annotations") {
				t.Fatalf("too_large report: %+v", sp)
			}
		case "tampered":
			if !strings.Contains(sp.Reasons[0], "do not hash") {
				t.Fatalf("tampered reason: %v", sp.Reasons)
			}
		case "wrong-size":
			if !strings.Contains(sp.Reasons[0], "64x64") {
				t.Fatalf("size reason: %v", sp.Reasons)
			}
		}
		if _, err := os.Stat(requestPath(receipts.Dir, plan.RunID, sp.ItemID)); err == nil {
			t.Fatalf("gated scene %s wrote a request document", sp.SceneID)
		}
	}
}

func itoa(i int) string {
	data, _ := json.Marshal(i)
	return string(data)
}

func TestReviewHeadSplitsHistoryAndChunksOver64MiB(t *testing.T) {
	raw := []byte(`{"revision":35,"model_signature":"abc","objects":[{"id":"x","ring":[[1.10,2.000]]}],"history":[{"revision":1},{"revision":2}],"updated_by":"local"}`)
	head, history, err := SplitReviewHistory(raw)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(head), "history") || string(history) != `[{"revision":1},{"revision":2}]` || !strings.Contains(string(head), `[[1.10,2.000]]`) {
		t.Fatalf("head=%s history=%s", head, history)
	}
	head2, _, _ := SplitReviewHistory(raw)
	if string(head) != string(head2) {
		t.Fatal("head bytes are not deterministic")
	}
	if _, _, err := SplitReviewHistory([]byte(`[1]`)); err == nil {
		t.Fatal("accepted a non-object review")
	}
	big := make([]byte, annotation.MaxAssetBytes+3)
	for i := range big {
		big[i] = 'x'
	}
	files := migration.NewLocalSceneFiles(t.TempDir(), "s", map[string][]byte{"data/scenes/s/review.history.json": big})
	ref, err := files.Ref("data/scenes/s/review.history.json")
	if err != nil {
		t.Fatal(err)
	}
	chunks, ok := ref["chunks"].([]annotation.Object)
	if !ok || len(chunks) != 2 || chunks[0]["bytes"] != annotation.MaxAssetBytes || chunks[1]["bytes"] != int64(3) || ref["sha256"] != migration.Digest(big) || ref["url"] != nil {
		t.Fatalf("chunked ref: %v", ref)
	}
	if chunks[0]["sha256"] != migration.Digest(big[:annotation.MaxAssetBytes]) || chunks[1]["sha256"] != migration.Digest(big[annotation.MaxAssetBytes:]) {
		t.Fatal("chunk digests wrong")
	}
	small, err := files.Ref("data/scenes/s/review.history.json")
	if err != nil || small["sha256"] != ref["sha256"] {
		t.Fatal("ref cache lost the hash")
	}
}

func TestPlanPreservesMachineProvenanceReplacesAndFieldworkImport(t *testing.T) {
	root := t.TempDir()
	writeScene(t, root, fixtureScene{id: "bindings", reviewRevision: 5, history: true})
	receipts, _ := LoadReceipts(filepath.Join(t.TempDir(), "push"))
	plan, err := BuildPlan(context.Background(), Inputs{Root: root, Project: testProject, Mirror: testMirror(), Receipts: receipts, Head: headAbsent, ReviewHistory: "split"})
	if err != nil {
		t.Fatal(err)
	}
	sp := plan.Scenes[0]
	if sp.Action != ActionCreate {
		t.Fatalf("plan: %+v", sp)
	}
	item := requestOf(t, receipts, plan, sp.ItemID)
	roof := item.Annotations[0].Properties["addr"].(map[string]any)
	if roof["source"] != "machine:sam3-text-onnx" || roof["provenance"].(map[string]any)["source"] != "machine:decision-policy-v2" || roof["status"] != "candidate" || roof["geometry_reviewed"] != false {
		t.Fatalf("provenance not verbatim: %v", roof)
	}
	if replaces, _ := roof["replaces"].([]any); len(replaces) != 2 || roof["clipped_by_frame"] != true || roof["has_roof_line_edits"] != true {
		t.Fatalf("extra bindings missing: %v", roof)
	}
	if _, has := roof["overrides"]; has {
		t.Fatal("push wrote overrides")
	}
	tree := item.Annotations[1].Properties["addr"].(map[string]any)
	marker, _ := tree["fieldwork_import"].(map[string]any)
	if marker["pending"] != true || marker["itemId"] != "addr-scene-x" {
		t.Fatalf("fieldwork_import marker not verbatim: %v", tree)
	}
	if _, has := tree["has_roof_line_edits"]; has {
		t.Fatal("has_roof_line_edits set without roof_line_edits")
	}
	// Status counts are carried as they are; nothing is promoted.
	summary := item.Metadata["addr"].(map[string]any)["summary"].(map[string]any)
	if summary["statuses"].(map[string]any)["confirmed"] != float64(1) || summary["layer"] != "review" {
		t.Fatalf("summary: %v", summary)
	}
	// Review history split: head is source.review, history only in assets.
	if item.Source["review"].(map[string]any)["sha256"] == item.Source["model"].(map[string]any)["sha256"] {
		t.Fatal("review head not distinct")
	}
	found := false
	for _, ref := range item.Source["assets"].([]any) {
		if ref.(map[string]any)["path"] == "data/scenes/bindings/review.history.json" {
			found = true
		}
	}
	if !found || item.Metadata["addr"].(map[string]any)["generation"].(map[string]any)["reviewHistorySha256"] == nil {
		t.Fatal("split history not listed in assets")
	}
	for key := range item.Source {
		if key == "history" || key == "reviewHistory" {
			t.Fatal("history must not be a source binding")
		}
	}
}

func TestReceiptsOwnWriteAndEvidenceRoundTrip(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "push")
	receipts, err := LoadReceipts(dir)
	if err != nil {
		t.Fatal(err)
	}
	doc := json.RawMessage(`{"id":"item-1","revision":1,"createdAt":"a","updatedAt":"b","title":"x","tags":[]}`)
	if err := receipts.RecordCreated(ItemReceipt{ItemID: "item-1", SceneID: "s", RunID: NewRunID(), PushedAt: "now", Revision: 1, Document: doc, Baseline: doc}); err != nil {
		t.Fatal(err)
	}
	later := json.RawMessage(`{"id":"item-1","revision":1,"createdAt":"a","updatedAt":"later","tags":[],"title":"x"}`)
	if !receipts.OwnWrite("item-1", 1, later) || receipts.OwnWrite("item-1", 2, later) || receipts.OwnWrite("item-1", 1, json.RawMessage(`{"id":"item-1","revision":1,"title":"y","tags":[]}`)) {
		t.Fatal("own-write detection wrong")
	}
	sha, err := receipts.StoreEvidence([]byte(`{"a":1}`))
	if err != nil {
		t.Fatal(err)
	}
	data, err := receipts.Evidence(sha)
	if err != nil || string(data) != `{"a":1}` {
		t.Fatalf("evidence: %s %v", data, err)
	}
	if err := os.WriteFile(filepath.Join(dir, "assets", sha), []byte(`{"a":2}`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := receipts.Evidence(sha); err == nil {
		t.Fatal("tampered evidence accepted")
	}
	reloaded, err := LoadReceipts(dir)
	if err != nil || reloaded.Scenes["s"].LatestItemID != "item-1" || reloaded.Items["item-1"].Revision != 1 {
		t.Fatalf("reload: %v %+v", err, reloaded.Scenes)
	}
	if err := os.WriteFile(filepath.Join(dir, "items", "item-1.json"), []byte(`{"schemaVersion":1,"itemId":"other"}`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadReceipts(dir); err == nil {
		t.Fatal("corrupt receipt accepted")
	}
	if !ValidRunID(NewRunID()) || ValidRunID("nope") {
		t.Fatal("run id shape")
	}
}

// A scene whose local model/review already matches the published item is
// "current" only while nothing moved in Fieldwork: the remote-edit check runs
// first, so an unpulled Fieldwork edit blocks even a scene with nothing to push.
func TestPlanRemoteEditsAreEvaluatedBeforeCurrent(t *testing.T) {
	root := t.TempDir()
	writeScene(t, root, fixtureScene{id: "current", signature: strings.Repeat("c", 64), reviewRevision: 4})
	frozen, frozenRaw := frozenDocument(t, root, "current")
	receipts, err := LoadReceipts(filepath.Join(t.TempDir(), "push"))
	if err != nil {
		t.Fatal(err)
	}
	feedbackRoot := t.TempDir()
	in := Inputs{Root: root, Project: testProject, Mirror: testMirror(frozenRaw), Receipts: receipts, FeedbackReceiptRoot: feedbackRoot, Head: headNever(t)}
	plan, err := BuildPlan(context.Background(), in)
	if err != nil || plan.Scenes[0].Action != ActionCurrent || plan.Scenes[0].ItemID != "addr-scene-current" || len(plan.Scenes[0].Unrecovered) != 0 {
		t.Fatalf("untouched frozen item must be current: %+v %v", plan.Scenes[0], err)
	}
	// A reviewer edits the frozen item in Fieldwork (revision 1 -> 2); the local scene does not change.
	frozen.Revision = 2
	frozen.Notes = "A Fieldwork reviewer note"
	editedRaw, _ := json.Marshal(frozen)
	in.Mirror = testMirror(editedRaw)
	plan, err = BuildPlan(context.Background(), in)
	if err != nil {
		t.Fatal(err)
	}
	sp := plan.Scenes[0]
	reasons := strings.Join(sp.Reasons, " ")
	if sp.Action != ActionBlockedRemoteEdits || len(sp.Unrecovered) != 1 || sp.Unrecovered[0].ItemID != "addr-scene-current" || sp.Unrecovered[0].Revision != 2 {
		t.Fatalf("expected blocked_remote_edits with addr-scene-current@2: %+v", sp)
	}
	if !strings.Contains(reasons, "addr-scene-current is at revision 2 in Fieldwork but our receipts expect 1") || !strings.Contains(reasons, RemoteEditsHint) || strings.Contains(reasons, "already carries") {
		t.Fatalf("reasons: %q", reasons)
	}
	if plan.Counts[ActionCurrent] != 0 || plan.Counts[ActionBlockedRemoteEdits] != 1 {
		t.Fatalf("counts: %v", plan.Counts)
	}
	if _, err := os.Stat(requestPath(receipts.Dir, plan.RunID, sp.ItemID)); err == nil {
		t.Fatal("blocked current scene wrote a request document")
	}
	// Once the local app has pulled exactly that document, nothing is unrecovered and the scene is current again.
	receiptPath := filepath.Join(feedbackRoot, migration.Digest([]byte(in.Mirror.Origin)), testProject, "receipts", migration.Digest([]byte("addr-scene-current"))+".json")
	writeJSON(t, receiptPath, annotation.Object{"schemaVersion": 1, "itemId": "addr-scene-current", "remote": annotation.Object{"sha256": migration.Digest(editedRaw)}})
	plan, err = BuildPlan(context.Background(), in)
	if err != nil || plan.Scenes[0].Action != ActionCurrent || plan.Scenes[0].LiveItems[0].Recovered != "feedback-receipt" || len(plan.Scenes[0].Unrecovered) != 0 {
		t.Fatalf("feedback receipt did not restore current: %+v %v", plan.Scenes[0], err)
	}
	// --allow-unrecovered has nothing to publish here: the scene stays current, but the unpulled edit remains on record.
	if err := os.Remove(receiptPath); err != nil {
		t.Fatal(err)
	}
	in.AllowUnrecovered = true
	plan, err = BuildPlan(context.Background(), in)
	if err != nil || plan.Scenes[0].Action != ActionCurrent || len(plan.Scenes[0].Unrecovered) != 1 || plan.Scenes[0].Unrecovered[0].Revision != 2 || !strings.Contains(strings.Join(plan.Scenes[0].Reasons, " "), "Fieldwork edits not yet pulled") {
		t.Fatalf("allow-unrecovered on a current scene: %+v %v", plan.Scenes[0], err)
	}
	if _, err := os.Stat(requestPath(receipts.Dir, plan.RunID, plan.Scenes[0].ItemID)); err == nil {
		t.Fatal("current scene wrote a request document")
	}
}
