package push

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/benebsworth/addr/internal/annotation"
)

type harness struct {
	t        *testing.T
	ts       *testServer
	root     string
	mirror   string
	receipts *ReceiptIndex
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	h := &harness{t: t, ts: startServer(t), root: t.TempDir(), mirror: t.TempDir()}
	receipts, err := LoadReceipts(filepath.Join(h.mirror, "push"))
	if err != nil {
		t.Fatal(err)
	}
	h.receipts = receipts
	return h
}

func (h *harness) plan(opts func(*Inputs)) *Plan {
	h.t.Helper()
	mirror := h.ts.sync(h.t, h.mirror)
	receipts, err := LoadReceipts(h.receipts.Dir)
	if err != nil {
		h.t.Fatal(err)
	}
	h.receipts = receipts
	in := Inputs{Root: h.root, Project: testProject, Mirror: mirror, Receipts: receipts, Head: h.ts.client.HeadAsset, MarkSuperseded: true}
	if opts != nil {
		opts(&in)
	}
	plan, err := BuildPlan(context.Background(), in)
	if err != nil {
		h.t.Fatal(err)
	}
	return plan
}

func (h *harness) apply(plan *Plan, mark bool) (*Result, error) {
	h.t.Helper()
	mirror := h.ts.sync(h.t, h.mirror)
	return Apply(context.Background(), h.ts.client, plan, h.receipts, io.Discard, ApplyOptions{MarkSuperseded: mark, Concurrency: 2, Root: h.root, Mirror: mirror})
}

func sceneResult(result *Result, sceneID string) SceneResult {
	for _, sr := range result.Scenes {
		if sr.SceneID == sceneID {
			return sr
		}
	}
	return SceneResult{}
}

func TestApplyCreatesThenSecondRunIsZeroWrites(t *testing.T) {
	h := newHarness(t)
	frozen := seedFrozen(t, h.ts, h.root, "house-1", 35)
	writeScene(t, h.root, fixtureScene{id: "outside-1", signature: strings.Repeat("d", 64), pixelSeed: 3})
	plan := h.plan(nil)
	if plan.Counts[ActionCreate] != 2 {
		t.Fatalf("plan counts %v", plan.Counts)
	}
	before := h.ts.cursor(t)
	h.ts.rec.reset()
	result, err := h.apply(plan, true)
	if err != nil {
		t.Fatal(err)
	}
	if result.Created != 2 || result.Marked != 1 || result.UploadedAssets == 0 || result.ConflictIDReuse != 0 {
		t.Fatalf("result %+v", result)
	}
	if h.ts.rec.rpc("DeleteItem") != 0 || h.ts.rec.rpc("DeleteAnnotation") != 0 || h.ts.rec.rpc("SaveAnnotation") != 0 {
		t.Fatal("push used a forbidden method")
	}
	house := sceneResult(result, "house-1")
	created, _ := h.ts.getItem(t, house.ItemID)
	if created["revision"] != float64(1) || created["status"] != "in_review" {
		t.Fatalf("created item: %v", created["revision"])
	}
	// The old item is marked by patch, its source byte-identical.
	old, oldRaw := h.ts.getItem(t, "addr-scene-house-1")
	oldSource, _ := json.Marshal(old["source"])
	frozenSource, _ := json.Marshal(frozen.Source)
	if old["revision"] != float64(2) || string(oldSource) != string(frozenSource) || !hasTag(anyStrings(old["tags"]), TagSuperseded) {
		t.Fatalf("old item not marked by patch: %v %v", old["revision"], old["tags"])
	}
	addr := old["metadata"].(map[string]any)["addr"].(map[string]any)
	if addr["supersededBy"].(map[string]any)["itemId"] != house.ItemID || addr["stale"] != true || addr["importRun"] == nil {
		t.Fatalf("supersede metadata: %v", addr)
	}
	if old["status"] != frozen.Status || old["title"] != frozen.Title || len(old["annotations"].([]any)) != len(frozen.Annotations) {
		t.Fatal("mark changed status, title or annotations")
	}
	// Receipts: generation and the marked frozen id (Baseline nil for frozen).
	rec, ok := h.receipts.Items["addr-scene-house-1"]
	if !ok || len(rec.Marks) != 1 || rec.Revision != 2 || rec.Baseline != nil || !h.receipts.OwnWrite("addr-scene-house-1", 2, oldRaw) {
		t.Fatalf("frozen receipt: %+v", rec)
	}
	gen := h.receipts.Items[house.ItemID]
	if gen.Revision != 1 || gen.Baseline == nil || gen.Generation.ReviewRevision != 35 || h.receipts.Scenes["house-1"].LatestItemID != house.ItemID {
		t.Fatalf("generation receipt: %+v", gen)
	}
	for _, name := range []string{"plan.json", "progress.ndjson", "result.json"} {
		if name == "plan.json" {
			continue // written by the CLI
		}
		if _, err := os.Stat(filepath.Join(h.receipts.Dir, "runs", plan.RunID, name)); err != nil {
			t.Fatalf("missing %s: %v", name, err)
		}
	}
	if entries, _ := os.ReadDir(filepath.Join(h.receipts.Dir, "assets")); len(entries) < 3 {
		t.Fatalf("JSON evidence not stored: %d", len(entries))
	}
	after := h.ts.cursor(t)
	if after == before {
		t.Fatal("cursor did not advance after writes")
	}
	// Second run: plan says current, apply performs zero writes, cursor unchanged.
	second := h.plan(nil)
	if second.Counts[ActionCurrent] != 2 || second.Counts[ActionCreate] != 0 {
		t.Fatalf("second plan %v", second.Counts)
	}
	h.ts.rec.reset()
	result2, err := h.apply(second, true)
	if err != nil || result2.Created != 0 || result2.Marked != 0 {
		t.Fatalf("second apply %+v %v", result2, err)
	}
	if h.ts.rec.count("PUT ") != 0 || h.ts.rec.rpc("SaveItem") != 0 || h.ts.rec.rpc("SaveLabel") != 0 {
		t.Fatalf("second run wrote: %v", h.ts.rec.requests)
	}
	if h.ts.cursor(t) != after {
		t.Fatal("cursor moved on a no-op run")
	}
	// Re-applying the ORIGINAL plan converges: identical items, marks skipped.
	h.ts.rec.reset()
	again, err := h.apply(plan, true)
	if err != nil || again.SkippedIdentical != 2 || again.Created != 0 || again.Marked != 0 || again.MarkSkipped != 1 || again.UploadedAssets != 0 {
		t.Fatalf("resume of original plan %+v %v", again, err)
	}
	if h.ts.rec.count("PUT ") != 0 || h.ts.cursor(t) != after {
		t.Fatal("resume wrote assets or documents")
	}
}

func anyStrings(value any) []string {
	out := []string{}
	if list, ok := value.([]any); ok {
		for _, v := range list {
			if s, ok := v.(string); ok {
				out = append(out, s)
			}
		}
	}
	return out
}

func TestApplyResumesAfterInterruptedAssetUpload(t *testing.T) {
	h := newHarness(t)
	seedFrozen(t, h.ts, h.root, "house-2", 7)
	plan := h.plan(nil)
	// Fail the first SaveItem so assets are uploaded but no document exists.
	failed := false
	h.ts.rec.fault = func(r *http.Request) (int, string) {
		if strings.HasSuffix(r.URL.Path, "/rpc/SaveItem") && !failed {
			failed = true
			return 400, `{"message":"simulated interruption"}`
		}
		return 0, ""
	}
	result, err := h.apply(plan, true)
	if err == nil || !strings.Contains(err.Error(), "simulated interruption") || result.Created != 0 {
		t.Fatalf("expected interruption error, got %+v %v", result, err)
	}
	uploaded := h.ts.rec.count("PUT ")
	if uploaded == 0 {
		t.Fatal("no assets uploaded before the interruption")
	}
	h.ts.rec.fault = nil
	h.ts.rec.reset()
	resumed, err := h.apply(plan, true)
	if err != nil || resumed.Created != 1 || resumed.Marked != 1 || resumed.UploadedAssets != 0 || resumed.ExistingAssets == 0 {
		t.Fatalf("resume %+v %v", resumed, err)
	}
	if h.ts.rec.count("PUT ") != 0 {
		t.Fatal("resume re-uploaded assets that already existed")
	}
	// Interrupt between create and mark: the mark fails (non-fatal), rerun marks.
	writeScene(t, h.root, fixtureScene{id: "house-2", width: 64, height: 64, signature: strings.Repeat("b", 64), reviewRevision: 8, history: true, expansion: true, pixelSeed: 2})
	plan2 := h.plan(nil)
	if plan2.Counts[ActionCreate] != 1 || len(plan2.Scenes[0].Supersedes) != 1 || plan2.Scenes[0].Supersedes[0].ItemID != sceneResult(resumed, "house-2").ItemID {
		t.Fatalf("second generation plan %+v", plan2.Scenes[0])
	}
	saves := 0
	h.ts.rec.fault = func(r *http.Request) (int, string) {
		if strings.HasSuffix(r.URL.Path, "/rpc/SaveItem") {
			saves++
			if saves == 2 {
				return 503, `{"message":"simulated outage during mark"}`
			}
		}
		return 0, ""
	}
	result2, err := h.apply(plan2, true)
	if err != nil || result2.Created != 1 || result2.MarkConflicts != 1 || result2.Marked != 0 {
		t.Fatalf("mark interruption %+v %v", result2, err)
	}
	h.ts.rec.fault = nil
	result3, err := h.apply(plan2, true)
	if err != nil || result3.SkippedIdentical != 1 || result3.Marked != 1 {
		t.Fatalf("rerun after mark failure %+v %v", result3, err)
	}
	gen1, _ := h.ts.getItem(t, plan2.Scenes[0].Supersedes[0].ItemID)
	if !hasTag(anyStrings(gen1["tags"]), TagSuperseded) || gen1["revision"] != float64(2) {
		t.Fatalf("first generation not marked: %v", gen1["tags"])
	}
	final := h.plan(nil)
	if final.Counts[ActionCurrent] != 1 {
		t.Fatalf("final plan %v", final.Counts)
	}
}

func TestApplyNeverOverwritesExistingDifferentItem(t *testing.T) {
	h := newHarness(t)
	seedFrozen(t, h.ts, h.root, "house-3", 2)
	plan := h.plan(nil)
	itemID := plan.Scenes[0].ItemID
	request, err := ReadRequest(h.receipts.Dir, plan.RunID, itemID)
	if err != nil {
		t.Fatal(err)
	}
	var other annotation.Item
	if err := json.Unmarshal(request, &other); err != nil {
		t.Fatal(err)
	}
	other.Notes = "Someone else created this id first"
	otherRaw, _ := json.Marshal(other)
	if _, err := h.ts.client.Call(context.Background(), "SaveItem", map[string]any{"item": json.RawMessage(otherRaw), "expectedRevision": 0}); err != nil {
		t.Fatal(err)
	}
	h.ts.rec.reset()
	result, err := h.apply(plan, true)
	if err != nil || result.ConflictIDReuse != 1 || result.Created != 0 || result.Marked != 0 {
		t.Fatalf("expected conflict_id_reuse %+v %v", result, err)
	}
	remote, _ := h.ts.getItem(t, itemID)
	if remote["revision"] != float64(1) || remote["notes"] != other.Notes {
		t.Fatal("existing different item was overwritten")
	}
	if _, has := h.receipts.Items[itemID]; has {
		t.Fatal("conflicting item received a receipt")
	}
	old, _ := h.ts.getItem(t, "addr-scene-house-3")
	if old["revision"] != float64(1) {
		t.Fatal("old item marked although the generation was not ours")
	}
}

func TestMarkSupersededIsPatchNotReplaceAndRetriesOnce(t *testing.T) {
	h := newHarness(t)
	frozen := seedFrozen(t, h.ts, h.root, "house-4", 9)
	// A Fieldwork reviewer edits the frozen item before we push: overrides + notes.
	mark := frozen.Annotations[0]
	mark.Properties = annotation.Object{"addr": annotation.Object{"objectId": mark.ID, "layer": "review", "status": "candidate", "geometry_reviewed": false, "source": "machine:sam3-text-onnx", "provenance": map[string]any{"source": "machine:decision-policy-v2"}, "overrides": annotation.Object{"status": "needs_adjustment", "notes": "reviewer: check the eave"}}}
	if _, err := h.ts.client.Call(context.Background(), "SaveAnnotation", annotation.Object{"itemId": "addr-scene-house-4", "annotation": mark, "expectedRevision": 1}); err != nil {
		t.Fatal(err)
	}
	edited, _ := h.ts.getItem(t, "addr-scene-house-4")
	editedItem := annotation.Object{}
	for k, v := range edited {
		editedItem[k] = v
	}
	editedItem["notes"] = "scene-level reviewer note"
	editedItem["status"] = "in_review"
	if _, err := h.ts.client.Call(context.Background(), "SaveItem", annotation.Object{"item": editedItem, "expectedRevision": 2}); err != nil {
		t.Fatal(err)
	}
	blocked := h.plan(nil)
	if blocked.Scenes[0].Action != ActionBlockedRemoteEdits {
		t.Fatalf("unpulled edits did not block: %+v", blocked.Scenes[0])
	}
	plan := h.plan(func(in *Inputs) { in.AllowUnrecovered = true })
	if plan.Scenes[0].Action != ActionCreate || len(plan.Scenes[0].Unrecovered) != 1 {
		t.Fatalf("allow-unrecovered plan %+v", plan.Scenes[0])
	}
	// First mark attempt hits a 409 (someone saved in between), retry succeeds.
	markSaves := 0
	h.ts.rec.fault = func(r *http.Request) (int, string) {
		if strings.HasSuffix(r.URL.Path, "/rpc/SaveItem") {
			body, _ := io.ReadAll(r.Body)
			r.Body = io.NopCloser(strings.NewReader(string(body)))
			if strings.Contains(string(body), `"expectedRevision":3`) {
				markSaves++
				if markSaves == 1 {
					return 409, `{"code":"aborted","message":"revision conflict"}`
				}
			}
		}
		return 0, ""
	}
	result, err := h.apply(plan, true)
	if err != nil || result.Created != 1 || result.Marked != 1 || markSaves != 2 {
		t.Fatalf("retry-once %+v saves=%d %v", result, markSaves, err)
	}
	old, _ := h.ts.getItem(t, "addr-scene-house-4")
	overrides := old["annotations"].([]any)[0].(map[string]any)["properties"].(map[string]any)["addr"].(map[string]any)["overrides"].(map[string]any)
	if old["revision"] != float64(4) || overrides["notes"] != "reviewer: check the eave" || overrides["status"] != "needs_adjustment" || old["notes"] != "scene-level reviewer note" || old["status"] != "in_review" {
		t.Fatalf("mark replaced reviewer state: %v %v", old["revision"], overrides)
	}
	if !hasTag(anyStrings(old["tags"]), TagSuperseded) || old["metadata"].(map[string]any)["addr"].(map[string]any)["stale"] != true {
		t.Fatal("mark missing")
	}
	created, _ := h.ts.getItem(t, plan.Scenes[0].ItemID)
	unrecovered := created["metadata"].(map[string]any)["addr"].(map[string]any)["unrecoveredFieldwork"].([]any)
	if len(unrecovered) != 1 || unrecovered[0].(map[string]any)["revision"] != float64(3) {
		t.Fatalf("unrecoveredFieldwork not recorded: %v", unrecovered)
	}
	// A permanent conflict after the retry is recorded, not fatal.
	writeScene(t, h.root, fixtureScene{id: "house-4", width: 64, height: 64, signature: strings.Repeat("b", 64), reviewRevision: 10, history: true, expansion: true, pixelSeed: 2})
	plan2 := h.plan(nil)
	h.ts.rec.fault = func(r *http.Request) (int, string) {
		if strings.HasSuffix(r.URL.Path, "/rpc/SaveItem") {
			body, _ := io.ReadAll(r.Body)
			r.Body = io.NopCloser(strings.NewReader(string(body)))
			if !strings.Contains(string(body), `"expectedRevision":0`) {
				return 409, `{"code":"aborted","message":"revision conflict"}`
			}
		}
		return 0, ""
	}
	result2, err := h.apply(plan2, true)
	if err != nil || result2.Created != 1 || result2.MarkConflicts != 1 || result2.Marked != 0 {
		t.Fatalf("permanent conflict %+v %v", result2, err)
	}
	h.ts.rec.fault = nil
}

// A reviewer edit that lands after the mirror sync Apply was given (a long live
// run checks late scenes against that stale mirror, so the mark's GetItem is
// the only fresh read) must not be re-sent inside the supersede mark and then
// claimed as our own write.
func TestMarkRefusesFieldworkEditSincePlanningWithoutAllowUnrecovered(t *testing.T) {
	h := newHarness(t)
	frozen := seedFrozen(t, h.ts, h.root, "house-7", 5)
	plan := h.plan(nil)
	sp := plan.Scenes[0]
	if sp.Action != ActionCreate || len(sp.Supersedes) != 1 || sp.Supersedes[0].ItemID != "addr-scene-house-7" || sp.Supersedes[0].Revision != 1 {
		t.Fatalf("plan %+v", sp)
	}
	stale := h.ts.sync(t, h.mirror)
	mark := frozen.Annotations[0]
	mark.Properties = annotation.Object{"addr": annotation.Object{"objectId": mark.ID, "layer": "review", "status": "candidate", "geometry_reviewed": false, "source": "machine:sam3-text-onnx", "provenance": map[string]any{"source": "machine:decision-policy-v2"}, "overrides": annotation.Object{"status": "needs_adjustment", "notes": "reviewer: landed after planning"}}}
	if _, err := h.ts.client.Call(context.Background(), "SaveAnnotation", annotation.Object{"itemId": "addr-scene-house-7", "annotation": mark, "expectedRevision": 1}); err != nil {
		t.Fatal(err)
	}
	h.ts.rec.reset()
	result, err := Apply(context.Background(), h.ts.client, plan, h.receipts, io.Discard, ApplyOptions{MarkSuperseded: true, Concurrency: 1, Root: h.root, Mirror: stale})
	if err != nil || result.Created != 1 || result.Marked != 0 || result.MarkConflicts != 1 {
		t.Fatalf("late Fieldwork edit was absorbed: %+v %v", result, err)
	}
	if saves := h.ts.rec.rpc("SaveItem"); saves != 1 {
		t.Fatalf("expected only the create SaveItem, saw %d", saves)
	}
	marks := sceneResult(result, "house-7").Marks
	if len(marks) != 1 || marks[0].Outcome != "mark_conflict" || marks[0].Revision != 2 || !strings.Contains(marks[0].Reason, "revision 2") || !strings.Contains(marks[0].Reason, "observed 1") {
		t.Fatalf("mark result %+v", marks)
	}
	old, doc := h.ts.getItem(t, "addr-scene-house-7")
	overrides := old["annotations"].([]any)[0].(map[string]any)["properties"].(map[string]any)["addr"].(map[string]any)["overrides"].(map[string]any)
	if old["revision"] != float64(2) || hasTag(anyStrings(old["tags"]), TagSuperseded) || overrides["notes"] != "reviewer: landed after planning" {
		t.Fatalf("old item changed by the mark: rev=%v tags=%v", old["revision"], old["tags"])
	}
	if _, has := h.receipts.Items["addr-scene-house-7"]; has || h.receipts.OwnWrite("addr-scene-house-7", 2, doc) {
		t.Fatal("foreign Fieldwork edit was recorded as our own write")
	}
	// The next push of a newer local revision blocks on the unpulled edit instead of passing.
	writeScene(t, h.root, fixtureScene{id: "house-7", width: 64, height: 64, signature: strings.Repeat("b", 64), reviewRevision: 6, history: true, expansion: true, pixelSeed: 2})
	next := h.plan(nil)
	if next.Scenes[0].Action != ActionBlockedRemoteEdits || len(next.Scenes[0].Unrecovered) != 1 || next.Scenes[0].Unrecovered[0].Revision != 2 {
		t.Fatalf("next plan did not block on the unpulled edit: %+v", next.Scenes[0])
	}
}

func TestApplyNeverCallsDeleteItem(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}
	forbidden := regexp.MustCompile(`"Delete(Item|Label|Annotation)"`)
	for _, entry := range entries {
		if !strings.HasSuffix(entry.Name(), ".go") || strings.HasSuffix(entry.Name(), "_test.go") {
			continue
		}
		data, err := os.ReadFile(entry.Name())
		if err != nil {
			t.Fatal(err)
		}
		if forbidden.Match(data) {
			t.Fatalf("%s references a delete RPC", entry.Name())
		}
	}
	h := newHarness(t)
	seedFrozen(t, h.ts, h.root, "house-5", 3)
	plan := h.plan(nil)
	h.ts.rec.reset()
	if _, err := h.apply(plan, true); err != nil {
		t.Fatal(err)
	}
	for _, method := range h.ts.rec.methods {
		if strings.HasPrefix(method, "Delete") {
			t.Fatalf("push called %s", method)
		}
	}
}

func TestAssetMismatchAbortsBeforeSaveItem(t *testing.T) {
	h := newHarness(t)
	seedFrozen(t, h.ts, h.root, "house-6", 3)
	plan := h.plan(nil)
	target := plan.Scenes[0].Assets[0].SHA256 // the scene PNG (unique bytes)
	putSeen := false
	h.ts.rec.tamper = func(r *http.Request, w http.ResponseWriter) bool {
		if r.URL.Path != "/api/v1/assets/"+target {
			return false
		}
		if r.Method == http.MethodPut {
			putSeen = true
			return false
		}
		if r.Method == http.MethodHead && putSeen {
			// Stored bytes advertise a different length after the upload.
			w.Header().Set("Content-Length", "1")
			w.Header().Set("X-Content-SHA256", target)
			w.WriteHeader(http.StatusOK)
			return true
		}
		return false
	}
	h.ts.rec.reset()
	result, err := h.apply(plan, true)
	if err != nil || result.Counts[ActionAssetMismatch] != 1 || result.Created != 0 || result.Marked != 0 {
		t.Fatalf("expected asset_mismatch %+v %v", result, err)
	}
	if h.ts.rec.rpc("SaveItem") != 0 {
		t.Fatal("SaveItem attempted after an asset mismatch")
	}
	if !strings.Contains(strings.Join(result.Scenes[0].Reasons, " "), "SaveItem not attempted") {
		t.Fatalf("reason: %v", result.Scenes[0].Reasons)
	}
	h.ts.rec.tamper = nil
	fixed, err := h.apply(plan, true)
	if err != nil || fixed.Created != 1 {
		t.Fatalf("rerun after mismatch %+v %v", fixed, err)
	}
}

func TestApplyStalePlanAndRemoteChangeSincePlanning(t *testing.T) {
	h := newHarness(t)
	seedFrozen(t, h.ts, h.root, "house-7", 3)
	plan := h.plan(nil)
	// Local input changes after planning -> stale_plan, nothing written.
	writeScene(t, h.root, fixtureScene{id: "house-7", width: 64, height: 64, signature: strings.Repeat("b", 64), reviewRevision: 4, history: true, expansion: true, pixelSeed: 2})
	h.ts.rec.reset()
	result, err := h.apply(plan, true)
	if err != nil || result.Counts[ActionStalePlan] != 1 || h.ts.rec.rpc("SaveItem") != 0 || h.ts.rec.count("PUT ") != 0 {
		t.Fatalf("stale plan %+v %v %v", result, err, h.ts.rec.requests)
	}
	// Remote edit after planning -> blocked at apply, nothing written for that scene.
	plan2 := h.plan(nil)
	old, _ := h.ts.getItem(t, "addr-scene-house-7")
	old["notes"] = "edited after planning"
	if _, err := h.ts.client.Call(context.Background(), "SaveItem", annotation.Object{"item": old, "expectedRevision": 1}); err != nil {
		t.Fatal(err)
	}
	h.ts.rec.reset()
	result2, err := h.apply(plan2, true)
	if err != nil || result2.Counts[ActionBlockedRemoteEdits] != 1 || h.ts.rec.rpc("SaveItem") != 0 {
		t.Fatalf("remote change since planning %+v %v", result2, err)
	}
}

// After a push the scene is current at the new generation; a reviewer edit on
// that generation in Fieldwork must block the next plan (naming the item and
// its live revision) even though the local review has not advanced. The
// properly marked frozen item, sitting at its receipt revision, is not reported.
func TestPlanBlocksFieldworkEditOnCurrentGeneration(t *testing.T) {
	h := newHarness(t)
	seedFrozen(t, h.ts, h.root, "house-8", 5)
	plan := h.plan(nil)
	if plan.Scenes[0].Action != ActionCreate {
		t.Fatalf("first plan %+v", plan.Scenes[0])
	}
	genID := plan.Scenes[0].ItemID
	if result, err := h.apply(plan, true); err != nil || result.Created != 1 || result.Marked != 1 {
		t.Fatalf("apply: %+v %v", result, err)
	}
	again := h.plan(nil)
	if again.Scenes[0].Action != ActionCurrent || again.Scenes[0].ItemID != genID || len(again.Scenes[0].Unrecovered) != 0 || len(again.Scenes[0].LiveItems) != 2 {
		t.Fatalf("second plan should be current at %s: %+v", genID, again.Scenes[0])
	}
	// Reviewer edit on the current generation via SaveAnnotation (revision 1 -> 2).
	_, doc := h.ts.getItem(t, genID)
	var live annotation.Item
	if err := json.Unmarshal(doc, &live); err != nil {
		t.Fatal(err)
	}
	if live.Revision != 1 || len(live.Annotations) == 0 {
		t.Fatalf("generation item revision %d annotations %d", live.Revision, len(live.Annotations))
	}
	edit := live.Annotations[0]
	edit.Properties["addr"].(map[string]any)["overrides"] = annotation.Object{"status": "needs_adjustment", "notes": "reviewer: edited the current generation"}
	if _, err := h.ts.client.Call(context.Background(), "SaveAnnotation", annotation.Object{"itemId": genID, "annotation": edit, "expectedRevision": 1}); err != nil {
		t.Fatal(err)
	}
	blocked := h.plan(nil)
	sp := blocked.Scenes[0]
	reasons := strings.Join(sp.Reasons, " ")
	if sp.Action != ActionBlockedRemoteEdits || len(sp.Unrecovered) != 1 || sp.Unrecovered[0].ItemID != genID || sp.Unrecovered[0].Revision != 2 {
		t.Fatalf("edit on the current generation did not block: %+v", sp)
	}
	if !strings.Contains(reasons, genID+" is at revision 2 in Fieldwork but our receipts expect 1") || !strings.Contains(reasons, RemoteEditsHint) || strings.Contains(reasons, "already carries") {
		t.Fatalf("reasons: %q", reasons)
	}
	for _, item := range sp.LiveItems {
		if item.Kind == "frozen" && (item.Revision != 2 || item.Expected != 2 || !item.Marked) {
			t.Fatalf("marked frozen item must sit at its receipt revision: %+v", item)
		}
	}
	if _, err := os.Stat(requestPath(h.receipts.Dir, blocked.RunID, sp.ItemID)); err == nil {
		t.Fatal("blocked scene wrote a request document")
	}
	// --allow-unrecovered: still nothing new to publish, so the scene stays current with the edit on record.
	allowed := h.plan(func(in *Inputs) { in.AllowUnrecovered = true })
	if allowed.Scenes[0].Action != ActionCurrent || allowed.Scenes[0].ItemID != genID || len(allowed.Scenes[0].Unrecovered) != 1 || allowed.Scenes[0].Unrecovered[0].Revision != 2 {
		t.Fatalf("allow-unrecovered on a current generation: %+v", allowed.Scenes[0])
	}
}
