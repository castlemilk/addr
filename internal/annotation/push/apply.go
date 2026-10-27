package push

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/benebsworth/addr/internal/annotation"
	"github.com/benebsworth/addr/internal/annotation/migration"
	"github.com/benebsworth/addr/internal/annotationsync"
)

type ApplyOptions struct {
	MarkSuperseded      bool
	Concurrency         int
	Root                string
	Mirror              annotationsync.Mirror // freshly synced before Apply
	FeedbackReceiptRoot string
	Now                 func() time.Time
}

type MarkResult struct {
	ItemID   string `json:"itemId"`
	Outcome  string `json:"outcome"` // marked | mark_conflict | mark_skipped
	Revision int64  `json:"revision,omitempty"`
	Reason   string `json:"reason,omitempty"`
}

type SceneResult struct {
	SceneID        string       `json:"sceneId"`
	ItemID         string       `json:"itemId"`
	Action         Action       `json:"action"`
	Reasons        []string     `json:"reasons"`
	Revision       int64        `json:"revision,omitempty"`
	UploadedAssets int          `json:"uploadedAssets"`
	ExistingAssets int          `json:"existingAssets"`
	Marks          []MarkResult `json:"marks"`
}

type Result struct {
	SchemaVersion    int            `json:"schemaVersion"`
	RunID            string         `json:"runId"`
	Origin           string         `json:"origin"`
	ProjectID        string         `json:"projectId"`
	Counts           map[Action]int `json:"counts"`
	Created          int            `json:"created"`
	SkippedIdentical int            `json:"skippedIdentical"`
	ConflictIDReuse  int            `json:"conflictIdReuse"`
	Marked           int            `json:"marked"`
	MarkConflicts    int            `json:"markConflicts"`
	MarkSkipped      int            `json:"markSkipped"`
	LabelsCreated    int            `json:"labelsCreated"`
	UploadedAssets   int            `json:"uploadedAssets"`
	ExistingAssets   int            `json:"existingAssets"`
	UploadedBytes    int64          `json:"uploadedBytes"`
	Scenes           []SceneResult  `json:"scenes"`
	MirrorCursor     string         `json:"mirrorCursor"`
	StartedAt        string         `json:"startedAt"`
	FinishedAt       string         `json:"finishedAt"`
}

type applier struct {
	client   *migration.Client
	plan     *Plan
	receipts *ReceiptIndex
	opts     ApplyOptions
	idx      *mirrorIndex
	progress io.Writer
	journal  *os.File
	mu       sync.Mutex
	result   *Result
}

// Apply publishes the create scenes of a plan. Every step is journaled to
// runs/<runId>/progress.ndjson before the next; re-running the same plan
// converges (assets HEAD-skip, identical items resolve to skip_identical,
// unmarked old items get marked). It never calls DeleteItem.
func Apply(ctx context.Context, c *migration.Client, plan *Plan, receipts *ReceiptIndex, progress io.Writer, opts ApplyOptions) (*Result, error) {
	if plan == nil || receipts == nil || c == nil {
		return nil, errors.New("apply requires a client, plan and receipts")
	}
	if !ValidRunID(plan.RunID) {
		return nil, errors.New("plan has an invalid run id")
	}
	if opts.Concurrency < 1 {
		opts.Concurrency = 1
	}
	if opts.Concurrency > 16 {
		opts.Concurrency = 16
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Root == "" {
		opts.Root = plan.Root
	}
	if opts.Mirror.ProjectID != plan.ProjectID || opts.Mirror.Origin != plan.Origin {
		return nil, errors.New("apply mirror does not match the plan's project or origin")
	}
	if progress == nil {
		progress = io.Discard
	}
	idx, err := indexMirror(opts.Mirror)
	if err != nil {
		return nil, err
	}
	runDir := filepath.Join(receipts.Dir, "runs", plan.RunID)
	if err := os.MkdirAll(filepath.Join(runDir, "items"), 0700); err != nil {
		return nil, err
	}
	journal, err := os.OpenFile(filepath.Join(runDir, "progress.ndjson"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		return nil, err
	}
	defer journal.Close()
	a := &applier{client: c, plan: plan, receipts: receipts, opts: opts, idx: idx, progress: progress, journal: journal}
	a.result = &Result{SchemaVersion: 1, RunID: plan.RunID, Origin: plan.Origin, ProjectID: plan.ProjectID, Counts: map[Action]int{}, Scenes: []SceneResult{}, MirrorCursor: opts.Mirror.Cursor, StartedAt: opts.Now().UTC().Format(time.RFC3339Nano)}
	a.log("", "", "start", "run", map[string]any{"scenes": len(plan.Scenes), "cursor": opts.Mirror.Cursor})
	if err := a.ensureLabels(ctx); err != nil {
		return a.result, err
	}
	pending := []ScenePlan{}
	for _, sp := range plan.Scenes {
		if sp.Action == ActionCreate {
			pending = append(pending, sp)
		} else {
			a.record(SceneResult{SceneID: sp.SceneID, ItemID: sp.ItemID, Action: sp.Action, Reasons: sp.Reasons, Marks: []MarkResult{}})
		}
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	jobs := make(chan ScenePlan)
	var wg sync.WaitGroup
	var firstErr error
	var once sync.Once
	for i := 0; i < opts.Concurrency; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for sp := range jobs {
				if ctx.Err() != nil {
					return
				}
				if err := a.scene(ctx, sp); err != nil {
					once.Do(func() { firstErr = err; cancel() })
					return
				}
			}
		}()
	}
loop:
	for _, sp := range pending {
		select {
		case jobs <- sp:
		case <-ctx.Done():
			break loop
		}
	}
	close(jobs)
	wg.Wait()
	a.result.FinishedAt = opts.Now().UTC().Format(time.RFC3339Nano)
	if firstErr == nil {
		firstErr = ctx.Err()
	}
	if data, err := json.MarshalIndent(a.result, "", "  "); err == nil {
		_ = writeAtomic(filepath.Join(runDir, "result.json"), data)
	}
	a.log("", "", "finish", "run", map[string]any{"created": a.result.Created, "skippedIdentical": a.result.SkippedIdentical, "marked": a.result.Marked, "error": errString(firstErr)})
	return a.result, firstErr
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func (a *applier) log(sceneID, itemID, step, outcome string, detail any) {
	entry := map[string]any{"ts": a.opts.Now().UTC().Format(time.RFC3339Nano), "runId": a.plan.RunID, "step": step, "outcome": outcome}
	if sceneID != "" {
		entry["sceneId"] = sceneID
	}
	if itemID != "" {
		entry["itemId"] = itemID
	}
	if detail != nil {
		entry["detail"] = detail
	}
	data, _ := json.Marshal(entry)
	a.mu.Lock()
	defer a.mu.Unlock()
	_, _ = a.journal.Write(append(data, '\n'))
	_ = a.journal.Sync()
}

func (a *applier) record(result SceneResult) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.result.Counts[result.Action]++
	a.result.Scenes = append(a.result.Scenes, result)
	a.result.UploadedAssets += result.UploadedAssets
	a.result.ExistingAssets += result.ExistingAssets
	for _, mark := range result.Marks {
		switch mark.Outcome {
		case "marked":
			a.result.Marked++
		case "mark_conflict":
			a.result.MarkConflicts++
		default:
			a.result.MarkSkipped++
		}
	}
	switch result.Action {
	case ActionCreate:
		a.result.Created++
	case ActionSkipIdentical:
		a.result.SkippedIdentical++
	case ActionConflictIDReuse:
		a.result.ConflictIDReuse++
	}
	fmt.Fprintf(a.progress, "%-40s %-22s %s\n", result.SceneID, result.Action, result.ItemID)
}

func (a *applier) ensureLabels(ctx context.Context) error {
	for _, label := range a.plan.Labels {
		if a.idx.labels[label.ID] {
			continue
		}
		_, err := a.client.Call(ctx, "SaveLabel", annotation.Object{"label": label, "expectedRevision": 0})
		var api *migration.APIError
		if errors.As(err, &api) && api.Status == 409 {
			existing, listErr := a.labelExists(ctx, label.ID)
			if listErr != nil {
				return listErr
			}
			if !existing {
				return fmt.Errorf("label %s: %w", label.ID, err)
			}
			a.log("", label.ID, "label", "existing", nil)
			continue
		}
		if err != nil {
			return fmt.Errorf("create label %s: %w", label.ID, err)
		}
		a.mu.Lock()
		a.result.LabelsCreated++
		a.mu.Unlock()
		a.log("", label.ID, "label", "created", nil)
	}
	return nil
}

func (a *applier) labelExists(ctx context.Context, id string) (bool, error) {
	cursor := ""
	for pages := 0; pages < 1000; pages++ {
		data, err := a.client.Call(ctx, "ListLabels", annotation.Object{"projectId": a.plan.ProjectID, "cursor": cursor, "limit": 500})
		if err != nil {
			return false, err
		}
		labels, _ := data["labels"].([]any)
		for _, raw := range labels {
			if doc, ok := raw.(map[string]any); ok && doc["id"] == id {
				return true, nil
			}
		}
		next, _ := data["nextCursor"].(string)
		if next == "" || next == cursor {
			return false, nil
		}
		cursor = next
	}
	return false, errors.New("label listing did not terminate")
}

// sceneFiles rebuilds the same override view the planner used.
func sceneFiles(root, sceneID, reviewHistory string) (*migration.LocalSceneFiles, error) {
	prefix := "data/scenes/" + sceneID + "/"
	overrides := map[string][]byte{}
	raw, err := os.ReadFile(filepath.Join(root, "data", "scenes", sceneID, "review.json"))
	if err == nil {
		head, history, err := SplitReviewHistory(raw)
		if err != nil {
			return nil, err
		}
		overrides[prefix+"review.json"] = head
		if reviewHistory == "split" && len(history) > 0 {
			overrides[prefix+"review.history.json"] = history
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	return migration.NewLocalSceneFiles(root, sceneID, overrides), nil
}

func (a *applier) scene(ctx context.Context, sp ScenePlan) error {
	result := SceneResult{SceneID: sp.SceneID, ItemID: sp.ItemID, Action: sp.Action, Reasons: []string{}, Marks: []MarkResult{}}
	finish := func(action Action, reason string) error {
		result.Action = action
		if reason != "" {
			result.Reasons = append(result.Reasons, reason)
		}
		a.log(sp.SceneID, sp.ItemID, "scene", string(action), reason)
		a.record(result)
		return nil
	}
	digest, err := InputsDigest(a.opts.Root, sp.SceneID)
	if err != nil {
		return err
	}
	if digest != sp.InputsSHA256 {
		return finish(ActionStalePlan, "local latest.json, review.json or image changed since planning; plan again")
	}
	// Re-evaluate remote edits against the fresh mirror.
	for _, live := range sp.LiveItems {
		current, ok := a.idx.items[live.ItemID]
		if !ok {
			if !live.Missing {
				return finish(ActionBlockedRemoteEdits, fmt.Sprintf("%s disappeared from Fieldwork since planning; %s", live.ItemID, RemoteEditsHint))
			}
			continue
		}
		if current.Revision == live.Revision || a.receipts.OwnWrite(live.ItemID, current.Revision, current.Raw) || FeedbackReceiptMatches(a.opts.FeedbackReceiptRoot, a.plan.Origin, a.plan.ProjectID, live.ItemID, current.Raw) {
			continue
		}
		return finish(ActionBlockedRemoteEdits, fmt.Sprintf("%s moved from revision %d to %d since planning; %s", live.ItemID, live.Revision, current.Revision, RemoteEditsHint))
	}
	request, err := ReadRequest(a.receipts.Dir, a.plan.RunID, sp.ItemID)
	if err != nil {
		return fmt.Errorf("scene %s: %w", sp.SceneID, err)
	}
	files, err := sceneFiles(a.opts.Root, sp.SceneID, a.plan.Options.ReviewHistory)
	if err != nil {
		return err
	}
	// (b) upload missing assets, chunk by chunk; HEAD-skip is inside UploadAsset.
	for _, asset := range sp.Assets {
		file, err := files.File(asset.Path)
		if err != nil {
			return err
		}
		if file.SHA256 != asset.SHA256 {
			return finish(ActionStalePlan, fmt.Sprintf("%s hashes to %s now, plan recorded %s", asset.Path, short(file.SHA256), short(asset.SHA256)))
		}
		var offset int64
		for _, chunk := range file.Chunks {
			start := offset
			open := func() (io.ReadCloser, error) { return openRange(files, asset.Path, start, chunk.Bytes) }
			contentType := file.ContentType
			if len(file.Chunks) > 1 {
				contentType = "application/octet-stream"
			}
			created, err := a.client.UploadAsset(ctx, chunk.SHA256, chunk.Bytes, contentType, open)
			if err != nil && strings.Contains(err.Error(), "metadata mismatch") {
				return finish(ActionAssetMismatch, fmt.Sprintf("%s chunk %s: %v; SaveItem not attempted", asset.Path, short(chunk.SHA256), err))
			}
			if err != nil {
				return fmt.Errorf("scene %s upload %s: %w", sp.SceneID, asset.Path, err)
			}
			if created {
				result.UploadedAssets++
				a.mu.Lock()
				a.result.UploadedBytes += chunk.Bytes
				a.mu.Unlock()
			} else {
				result.ExistingAssets++
			}
			a.log(sp.SceneID, sp.ItemID, "asset", map[bool]string{true: "created", false: "existing"}[created], map[string]any{"path": asset.Path, "sha256": chunk.SHA256, "bytes": chunk.Bytes})
			offset += chunk.Bytes
		}
		if asset.Evidence {
			data, err := files.ReadFile(ctx, asset.Path)
			if err != nil {
				return err
			}
			if _, err := a.receipts.StoreEvidence(data); err != nil {
				return err
			}
		}
	}
	// (c) re-HEAD every ref: the server accepts dangling refs, so this is the only gate.
	for _, asset := range sp.Assets {
		for c, chunk := range asset.ChunkSHA256 {
			exists, err := a.client.HeadAsset(ctx, chunk, asset.ChunkBytes[c])
			if err != nil || !exists {
				return finish(ActionAssetMismatch, fmt.Sprintf("%s chunk %s did not verify after upload (%v); SaveItem not attempted", asset.Path, short(chunk), errString(err)))
			}
		}
	}
	a.log(sp.SceneID, sp.ItemID, "head", "verified", len(sp.Assets))
	// (d) create with expectedRevision 0; never overwrite.
	response, err := a.client.CallRaw(ctx, "SaveItem", map[string]any{"item": request, "expectedRevision": 0})
	now := a.opts.Now().UTC().Format(time.RFC3339Nano)
	var api *migration.APIError
	if errors.As(err, &api) && api.Status == 409 {
		remote, getErr := a.client.CallRaw(ctx, "GetItem", annotation.Object{"id": sp.ItemID})
		if getErr != nil {
			return fmt.Errorf("scene %s: %s exists but could not be read: %w", sp.SceneID, sp.ItemID, getErr)
		}
		doc, revision, docErr := itemDocument(remote)
		if docErr != nil {
			return docErr
		}
		_ = writeAtomic(filepath.Join(a.receipts.Dir, "runs", a.plan.RunID, "items", sp.ItemID+".response.json"), doc)
		if !identicalIgnoringPush(doc, request) {
			return finish(ActionConflictIDReuse, fmt.Sprintf("%s already exists with different content at revision %d; not overwritten", sp.ItemID, revision))
		}
		result.Revision = revision
		if _, ok := a.receipts.Item(sp.ItemID); !ok {
			if err := a.writeCreateReceipt(sp, doc, revision, now); err != nil {
				return err
			}
		}
		a.log(sp.SceneID, sp.ItemID, "save", "skip_identical", revision)
		result.Action = ActionSkipIdentical
		result.Reasons = append(result.Reasons, fmt.Sprintf("%s already exists with identical content at revision %d", sp.ItemID, revision))
	} else if err != nil {
		return fmt.Errorf("scene %s: SaveItem %s: %w", sp.SceneID, sp.ItemID, err)
	} else {
		doc, revision, docErr := itemDocument(response)
		if docErr != nil {
			return docErr
		}
		_ = writeAtomic(filepath.Join(a.receipts.Dir, "runs", a.plan.RunID, "items", sp.ItemID+".response.json"), doc)
		result.Revision = revision
		if err := a.writeCreateReceipt(sp, doc, revision, now); err != nil {
			return err
		}
		a.log(sp.SceneID, sp.ItemID, "save", "created", revision)
	}
	// (f) mark superseded, patch-not-replace, revision-checked.
	if a.opts.MarkSuperseded {
		for _, old := range sp.Supersedes {
			mark := a.mark(ctx, sp.SceneID, old, sp.ItemID)
			result.Marks = append(result.Marks, mark)
			a.log(sp.SceneID, old.ItemID, "mark", mark.Outcome, mark.Reason)
		}
	}
	a.record(result)
	return nil
}

// writeCreateReceipt records the created item; ReceiptIndex guards its own maps.
func (a *applier) writeCreateReceipt(sp ScenePlan, doc json.RawMessage, revision int64, now string) error {
	return a.receipts.RecordCreated(ItemReceipt{ItemID: sp.ItemID, SceneID: sp.SceneID, RunID: a.plan.RunID, PushedAt: now, Revision: revision, Document: doc, Baseline: doc, Generation: sp.Generation, Supersedes: sp.Supersedes})
}

func openRange(files *migration.LocalSceneFiles, path string, offset, length int64) (io.ReadCloser, error) {
	reader, err := files.Open(path)
	if err != nil {
		return nil, err
	}
	if offset > 0 {
		if seeker, ok := reader.(io.Seeker); ok {
			if _, err := seeker.Seek(offset, io.SeekStart); err != nil {
				reader.Close()
				return nil, err
			}
		} else if _, err := io.CopyN(io.Discard, reader, offset); err != nil {
			reader.Close()
			return nil, err
		}
	}
	return struct {
		io.Reader
		io.Closer
	}{io.LimitReader(reader, length), reader}, nil
}

func itemDocument(response json.RawMessage) (json.RawMessage, int64, error) {
	var envelope struct {
		Item json.RawMessage `json:"item"`
	}
	if err := json.Unmarshal(response, &envelope); err != nil || len(envelope.Item) == 0 {
		return nil, 0, errors.New("SaveItem/GetItem response lacks an item document")
	}
	var base struct {
		Revision int64 `json:"revision"`
	}
	if err := json.Unmarshal(envelope.Item, &base); err != nil {
		return nil, 0, err
	}
	return envelope.Item, base.Revision, nil
}

// identicalIgnoringPush compares a remote document with our request ignoring
// server fields and the run bookkeeping under metadata.addr.push, so a crash
// between SaveItem and the receipt write converges on the next run.
func identicalIgnoringPush(remote, request json.RawMessage) bool {
	strip := func(raw json.RawMessage) any {
		var doc map[string]any
		if json.Unmarshal(raw, &doc) != nil {
			return nil
		}
		for _, key := range []string{"revision", "createdAt", "updatedAt"} {
			delete(doc, key)
		}
		if meta, ok := doc["metadata"].(map[string]any); ok {
			if addr, ok := meta["addr"].(map[string]any); ok {
				delete(addr, "push")
			}
		}
		if doc["notes"] == nil {
			doc["notes"] = ""
		}
		return doc
	}
	left, right := strip(remote), strip(request)
	return left != nil && right != nil && sameJSON(left, right)
}

// mark adds the supersede tag and metadata to an old item, keeping every other
// field byte-identical, with the revision just read. The fresh document must
// still be at the revision the plan observed (old.Revision), at our own later
// write, or a document the local app already accepted; anything else is a
// Fieldwork edit since planning and yields mark_conflict instead of being
// re-sent inside the mark and recorded as our write. A 409 is retried once.
func (a *applier) mark(ctx context.Context, sceneID string, old Supersedes, newID string) MarkResult {
	oldID := old.ItemID
	for attempt := 0; attempt < 2; attempt++ {
		remote, err := a.client.CallRaw(ctx, "GetItem", annotation.Object{"id": oldID})
		if err != nil {
			return MarkResult{ItemID: oldID, Outcome: "mark_conflict", Reason: "GetItem: " + err.Error()}
		}
		doc, revision, err := itemDocument(remote)
		if err != nil {
			return MarkResult{ItemID: oldID, Outcome: "mark_conflict", Reason: err.Error()}
		}
		var fields map[string]json.RawMessage
		if err := json.Unmarshal(doc, &fields); err != nil {
			return MarkResult{ItemID: oldID, Outcome: "mark_conflict", Reason: err.Error()}
		}
		var tags []string
		_ = json.Unmarshal(fields["tags"], &tags)
		var metadata map[string]any
		_ = json.Unmarshal(fields["metadata"], &metadata)
		if metadata == nil {
			metadata = map[string]any{}
		}
		addr, _ := metadata["addr"].(map[string]any)
		if addr == nil {
			addr = map[string]any{}
			metadata["addr"] = addr
		}
		if existing, ok := addr["supersededBy"].(map[string]any); ok && existing["itemId"] == newID {
			return MarkResult{ItemID: oldID, Outcome: "mark_skipped", Revision: revision, Reason: "already marked superseded by " + newID}
		}
		receipt, _ := a.receipts.Item(oldID)
		receiptRevision := receipt.Revision
		ownWrite := a.receipts.OwnWrite(oldID, revision, doc)
		if _, ok := addr["supersededBy"]; ok && receiptRevision == revision {
			return MarkResult{ItemID: oldID, Outcome: "mark_skipped", Revision: revision, Reason: "already marked superseded"}
		}
		if revision != old.Revision && !ownWrite && !FeedbackReceiptMatches(a.opts.FeedbackReceiptRoot, a.plan.Origin, a.plan.ProjectID, oldID, doc) {
			return MarkResult{ItemID: oldID, Outcome: "mark_conflict", Revision: revision, Reason: fmt.Sprintf("%s is at revision %d in Fieldwork but the plan observed %d; the Fieldwork edit since planning was left untouched and not marked, %s", oldID, revision, old.Revision, RemoteEditsHint)}
		}
		hasTag := false
		for _, tag := range tags {
			hasTag = hasTag || tag == TagSuperseded
		}
		if !hasTag {
			tags = append(tags, TagSuperseded)
		}
		addr["supersededBy"] = map[string]any{"itemId": newID, "runId": a.plan.RunID}
		addr["stale"] = true
		fields["tags"], _ = json.Marshal(tags)
		fields["metadata"], _ = json.Marshal(metadata)
		patched, _ := json.Marshal(fields)
		_ = writeAtomic(filepath.Join(a.receipts.Dir, "runs", a.plan.RunID, "items", oldID+".mark.request.json"), patched)
		response, err := a.client.CallRaw(ctx, "SaveItem", map[string]any{"item": json.RawMessage(patched), "expectedRevision": revision})
		var api *migration.APIError
		if errors.As(err, &api) && api.Status == 409 {
			if attempt == 0 {
				continue
			}
			return MarkResult{ItemID: oldID, Outcome: "mark_conflict", Revision: revision, Reason: "revision conflict twice; Fieldwork edits in flight, " + RemoteEditsHint}
		}
		if err != nil {
			return MarkResult{ItemID: oldID, Outcome: "mark_conflict", Revision: revision, Reason: err.Error()}
		}
		saved, savedRevision, err := itemDocument(response)
		if err != nil {
			return MarkResult{ItemID: oldID, Outcome: "mark_conflict", Reason: err.Error()}
		}
		_ = writeAtomic(filepath.Join(a.receipts.Dir, "runs", a.plan.RunID, "items", oldID+".mark.response.json"), saved)
		err = a.receipts.RecordMark(oldID, sceneID, Mark{RunID: a.plan.RunID, SupersedeBy: newID, Revision: savedRevision, MarkedAt: a.opts.Now().UTC().Format(time.RFC3339Nano), Document: saved})
		if err != nil {
			return MarkResult{ItemID: oldID, Outcome: "mark_conflict", Revision: savedRevision, Reason: "marked in Fieldwork but the receipt could not be written: " + err.Error()}
		}
		return MarkResult{ItemID: oldID, Outcome: "marked", Revision: savedRevision}
	}
	return MarkResult{ItemID: oldID, Outcome: "mark_conflict"}
}

// Tags helper for tests and status output.
func hasTag(tags []string, want string) bool {
	for _, tag := range tags {
		if strings.EqualFold(tag, want) {
			return true
		}
	}
	return false
}
