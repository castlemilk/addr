package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"text/tabwriter"

	"github.com/benebsworth/addr/internal/annotation/migration"
	"github.com/benebsworth/addr/internal/annotation/push"
	"github.com/benebsworth/addr/internal/annotationsync"
)

// push publishes changed local scenes as new immutable generation items.
// Plans are written before any write; --dry-run stops after planning and only
// reads (ListChanges via sync, HEAD for asset existence). Pushed values are
// model estimates plus local review state as of publication, never human labels.
func (c *client) push(ctx context.Context, o options, output, diagnostics io.Writer) error {
	if o.project == "" || o.directory == "" {
		return errors.New("push requires --project and --dir (mirror directory)")
	}
	if o.cursor != "" {
		return errors.New("push manages its own mirror checkpoint; do not pass --cursor")
	}
	if o.reviewHistory != "omit" && o.reviewHistory != "split" {
		return errors.New("--review-history must be omit or split")
	}
	if o.runID != "" && !push.ValidRunID(o.runID) {
		return errors.New("--run must be a run id of the form yyyymmddTHHMMSSZ-xxxxxxxx")
	}
	root, err := filepath.Abs(o.root)
	if err != nil {
		return err
	}
	directory, err := filepath.Abs(o.directory)
	if err != nil {
		return err
	}
	feedbackRoot := o.feedbackReceipts
	if feedbackRoot == "" {
		feedbackRoot = filepath.Join(root, "data", "reviews", "fieldwork-feedback")
	}
	receiptsDir := filepath.Join(directory, "push")
	unlock, err := push.Lock(receiptsDir)
	if err != nil {
		return err
	}
	defer unlock()
	remote, err := migration.NewClient(c.baseURL, c.token)
	if err != nil {
		return err
	}
	if _, err := annotationsync.Sync(ctx, c.fetchChanges, c.baseURL, o.project, directory, o.limit); err != nil {
		return err
	}
	mirror, err := annotationsync.Load(directory, c.baseURL, o.project)
	if err != nil {
		return err
	}
	receipts, err := push.LoadReceipts(receiptsDir)
	if err != nil {
		return err
	}
	var scenes []string
	if strings.TrimSpace(o.scenes) != "" {
		for _, scene := range strings.Split(o.scenes, ",") {
			if scene = strings.TrimSpace(scene); scene != "" {
				scenes = append(scenes, scene)
			}
		}
	}
	var plan *push.Plan
	if o.runID != "" {
		plan, err = loadPlan(receiptsDir, o.runID)
		if err != nil {
			return err
		}
		if plan.ProjectID != o.project || plan.Origin != c.baseURL {
			return errors.New("saved plan belongs to a different project or server")
		}
		if err := checkPlanHash(plan); err != nil {
			return err
		}
	} else {
		plan, err = push.BuildPlan(ctx, push.Inputs{Root: root, Project: o.project, Mirror: mirror, Receipts: receipts, FeedbackReceiptRoot: feedbackRoot, Scenes: scenes, IncludeCurrent: o.all, SkipOutsideInventory: o.skipOutsideInventory, AllowUnrecovered: o.allowUnrecovered, MarkSuperseded: o.markSuperseded, ReviewHistory: o.reviewHistory, Head: remote.HeadAsset})
		if err != nil {
			return err
		}
		data, err := json.MarshalIndent(plan, "", "  ")
		if err != nil {
			return err
		}
		if err := os.MkdirAll(filepath.Join(receiptsDir, "runs", plan.RunID), 0700); err != nil {
			return err
		}
		if err := os.WriteFile(filepath.Join(receiptsDir, "runs", plan.RunID, "plan.json"), data, 0600); err != nil {
			return err
		}
	}
	planFile := filepath.Join(receiptsDir, "runs", plan.RunID, "plan.json")
	printPlanTable(diagnostics, plan, o.all)
	summary := map[string]any{"runId": plan.RunID, "counts": plan.Counts, "planFile": planFile, "mirrorCursor": mirror.Cursor, "bytesToUpload": plan.BytesToUpload, "labelsToCreate": len(plan.Labels), "dryRun": o.dryRun}
	if blocked := blockedScenes(plan); len(blocked) > 0 {
		summary["blocked"] = blocked
	}
	if o.dryRun {
		return json.NewEncoder(output).Encode(summary)
	}
	result, applyErr := push.Apply(ctx, remote, plan, receipts, diagnostics, push.ApplyOptions{MarkSuperseded: o.markSuperseded, Concurrency: o.concurrency, Root: root, Mirror: mirror, FeedbackReceiptRoot: feedbackRoot})
	if result != nil {
		summary["result"] = map[string]any{"created": result.Created, "skippedIdentical": result.SkippedIdentical, "conflictIdReuse": result.ConflictIDReuse, "marked": result.Marked, "markConflicts": result.MarkConflicts, "markSkipped": result.MarkSkipped, "labelsCreated": result.LabelsCreated, "uploadedAssets": result.UploadedAssets, "existingAssets": result.ExistingAssets, "uploadedBytes": result.UploadedBytes, "counts": result.Counts, "resultFile": filepath.Join(receiptsDir, "runs", plan.RunID, "result.json")}
		summary["receiptsDir"] = receiptsDir
	}
	if applyErr != nil {
		summary["error"] = applyErr.Error()
		summary["resume"] = fmt.Sprintf("annotate push --project %s --dir %s --run %s", o.project, o.directory, plan.RunID)
	}
	if err := json.NewEncoder(output).Encode(summary); err != nil {
		return err
	}
	return applyErr
}

func loadPlan(receiptsDir, runID string) (*push.Plan, error) {
	data, err := os.ReadFile(filepath.Join(receiptsDir, "runs", runID, "plan.json"))
	if err != nil {
		return nil, fmt.Errorf("saved plan %s: %w", runID, err)
	}
	var plan push.Plan
	if err := json.Unmarshal(data, &plan); err != nil || plan.SchemaVersion != 1 || plan.RunID != runID {
		return nil, fmt.Errorf("saved plan %s is corrupt or has a different run id", runID)
	}
	return &plan, nil
}

// checkPlanHash recomputes the local input digest of every planned scene.
// A resumed run must apply exactly the frozen request documents it planned.
func checkPlanHash(plan *push.Plan) error {
	for _, sp := range plan.Scenes {
		digest, err := push.InputsDigest(plan.Root, sp.SceneID)
		if err != nil {
			return fmt.Errorf("scene %s: %w", sp.SceneID, err)
		}
		if digest != sp.InputsSHA256 {
			return fmt.Errorf("plan %s is stale: local inputs of scene %s changed since planning; run push again without --run", plan.RunID, sp.SceneID)
		}
	}
	return nil
}

func blockedScenes(plan *push.Plan) []map[string]any {
	blocked := []map[string]any{}
	for _, sp := range plan.Scenes {
		switch sp.Action {
		case push.ActionCreate, push.ActionCurrent:
			continue
		}
		blocked = append(blocked, map[string]any{"sceneId": sp.SceneID, "action": sp.Action, "reasons": sp.Reasons, "documentBytes": sp.DocumentBytes, "annotations": sp.Annotations})
	}
	return blocked
}

func printPlanTable(w io.Writer, plan *push.Plan, all bool) {
	table := tabwriter.NewWriter(w, 0, 4, 2, ' ', 0)
	fmt.Fprintln(table, "SCENE\tACTION\tITEM\tDOC_BYTES\tASSETS_TO_UPLOAD\tSUPERSEDES\tREASONS")
	for _, sp := range plan.Scenes {
		if sp.Action == push.ActionCurrent && !all {
			continue
		}
		var upload int
		var bytes int64
		for _, asset := range sp.Assets {
			if !asset.Exists {
				upload++
				bytes += asset.Bytes
			}
		}
		supersedes := []string{}
		for _, s := range sp.Supersedes {
			supersedes = append(supersedes, fmt.Sprintf("%s@%d", s.ItemID, s.Revision))
		}
		fmt.Fprintf(table, "%s\t%s\t%s\t%d\t%d/%d B\t%s\t%s\n", sp.SceneID, sp.Action, sp.ItemID, sp.DocumentBytes, upload, bytes, strings.Join(supersedes, ","), strings.Join(sp.Reasons, "; "))
	}
	table.Flush()
	keys := make([]string, 0, len(plan.Counts))
	for action := range plan.Counts {
		keys = append(keys, string(action))
	}
	sort.Strings(keys)
	parts := []string{}
	for _, key := range keys {
		parts = append(parts, fmt.Sprintf("%s=%d", key, plan.Counts[push.Action(key)]))
	}
	fmt.Fprintf(w, "run %s: %s; %d labels to create; %d bytes to upload\n", plan.RunID, strings.Join(parts, " "), len(plan.Labels), plan.BytesToUpload)
}

// pushStatus summarises receipts only; it never contacts the server.
func pushStatus(o options, output io.Writer) error {
	if o.directory == "" {
		return errors.New("push-status requires --dir (mirror directory)")
	}
	receiptsDir := filepath.Join(o.directory, "push")
	receipts, err := push.LoadReceipts(receiptsDir)
	if err != nil {
		return err
	}
	scenes := []map[string]any{}
	for _, id := range receipts.SceneIDs() {
		scene, _ := receipts.Scene(id)
		if o.scenes != "" && !strings.Contains(","+o.scenes+",", ","+id+",") {
			continue
		}
		latest, _ := receipts.Item(scene.LatestItemID)
		scenes = append(scenes, map[string]any{"sceneId": id, "latestItemId": scene.LatestItemID, "generations": len(scene.ItemIDs), "modelSignature": latest.Generation.ModelSignature, "reviewRevision": latest.Generation.ReviewRevision, "revision": latest.Revision, "runId": latest.RunID, "pushedAt": latest.PushedAt, "supersedes": latest.Supersedes})
	}
	marked := 0
	for _, id := range receipts.ItemIDs() {
		if rec, _ := receipts.Item(id); len(rec.Marks) > 0 {
			marked++
		}
	}
	items, _ := receipts.Len()
	runs := []string{}
	if entries, err := os.ReadDir(filepath.Join(receiptsDir, "runs")); err == nil {
		for _, entry := range entries {
			if entry.IsDir() {
				runs = append(runs, entry.Name())
			}
		}
	}
	return json.NewEncoder(output).Encode(map[string]any{"receiptsDir": receiptsDir, "scenes": scenes, "items": items, "markedItems": marked, "runs": runs})
}
