package push

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/benebsworth/addr/internal/annotation"
	"github.com/benebsworth/addr/internal/annotation/migration"
	"github.com/benebsworth/addr/internal/annotationsync"
)

// Action classifies what push will do (or refuse to do) for one scene.
type Action string

const (
	ActionCreate             Action = "create"
	ActionCurrent            Action = "current"
	ActionSkipIdentical      Action = "skip_identical"
	ActionConflictIDReuse    Action = "conflict_id_reuse"
	ActionBlockedRemoteEdits Action = "blocked_remote_edits"
	ActionBlockedStaleReview Action = "blocked_stale_review"
	ActionBlockedReviewLock  Action = "blocked_review_lock"
	ActionTooLarge           Action = "too_large"
	ActionInvalidFrame       Action = "invalid_frame"
	ActionAssetMismatch      Action = "asset_mismatch"
	ActionStalePlan          Action = "stale_plan"
	ActionNeedsLabel         Action = "needs_label"
)

const (
	InventoryFrozen  = "frozen"
	InventoryOutside = "outside-frozen-inventory"
	TagSuperseded    = "superseded-frame"
	RemoteEditsHint  = "task pull, resolve conflicts, then task push"
	StaleReviewHint  = "carry the review forward to the latest model (editor \"Carry edits forward\" or scripts/carry-forward-reviews.mjs)"
)

// MaxAnnotations is the server's per-document annotation bound.
const MaxAnnotations = 5000

type AssetPlan struct {
	Path        string   `json:"path"`
	SHA256      string   `json:"sha256"`
	ContentType string   `json:"contentType"`
	Bytes       int64    `json:"bytes"`
	Exists      bool     `json:"exists"`
	Chunks      int      `json:"chunks"`
	ChunkSHA256 []string `json:"chunkSha256,omitempty"`
	ChunkBytes  []int64  `json:"chunkBytes,omitempty"`
	Evidence    bool     `json:"evidence"` // JSON kept under push/assets after upload
}

// LiveItem is an existing Fieldwork item of the scene and the revision the
// plan observed it at. Expected is what our receipts (or frozen import) say it
// should be; a difference means someone edited it in Fieldwork.
type LiveItem struct {
	ItemID         string `json:"itemId"`
	Kind           string `json:"kind"` // frozen | generation
	Revision       int64  `json:"revision"`
	Expected       int64  `json:"expected"`
	Marked         bool   `json:"marked"`
	Missing        bool   `json:"missing,omitempty"`
	ModelSignature string `json:"modelSignature,omitempty"`
	ImageSHA256    string `json:"imageSha256,omitempty"`
	Recovered      string `json:"recovered,omitempty"` // feedback-receipt | own-write | ""
}

type Unrecovered struct {
	ItemID   string `json:"itemId"`
	Revision int64  `json:"revision"`
}

type ScenePlan struct {
	SceneID       string        `json:"sceneId"`
	ItemID        string        `json:"itemId"`
	Action        Action        `json:"action"`
	Reasons       []string      `json:"reasons"`
	DocumentBytes int           `json:"documentBytes"`
	Annotations   int           `json:"annotations"`
	Assets        []AssetPlan   `json:"assets"`
	Supersedes    []Supersedes  `json:"supersedes"`
	Inventory     string        `json:"inventory"`
	Generation    Generation    `json:"generation"`
	LiveItems     []LiveItem    `json:"liveItems"`
	Unrecovered   []Unrecovered `json:"unrecoveredFieldwork,omitempty"`
	InputsSHA256  string        `json:"inputsSha256"`
	Tags          []string      `json:"tags,omitempty"`
}

type PlanOptions struct {
	ReviewHistory        string `json:"reviewHistory"`
	SkipOutsideInventory bool   `json:"skipOutsideInventory"`
	AllowUnrecovered     bool   `json:"allowUnrecovered"`
	MarkSuperseded       bool   `json:"markSuperseded"`
}

type Plan struct {
	SchemaVersion int                `json:"schemaVersion"`
	RunID         string             `json:"runId"`
	Origin        string             `json:"origin"`
	ProjectID     string             `json:"projectId"`
	Root          string             `json:"root"`
	MirrorCursor  string             `json:"mirrorCursor"`
	PlanHash      string             `json:"planHash"`
	CreatedAt     string             `json:"createdAt"`
	Options       PlanOptions        `json:"options"`
	Scenes        []ScenePlan        `json:"scenes"`
	Labels        []annotation.Label `json:"labels"`
	Counts        map[Action]int     `json:"counts"`
	BytesToUpload int64              `json:"bytesToUpload"`
}

type Inputs struct {
	Root                 string
	Project              string
	RunID                string
	Mirror               annotationsync.Mirror
	Receipts             *ReceiptIndex
	FeedbackReceiptRoot  string
	Scenes               []string
	IncludeCurrent       bool
	SkipOutsideInventory bool
	AllowUnrecovered     bool
	MarkSuperseded       bool
	ReviewHistory        string
	Head                 func(ctx context.Context, sha string, size int64) (bool, error)
	Now                  func() time.Time
}

var labelColors = map[string]string{"building": "#D19A69", "tree": "#58A675", "solar_panel": "#588BD2", "road": "#8A96A4", "car": "#D4676D", "pool": "#51B5C5", "driveway": "#ABA08F", "footpath": "#BCAC93", "vegetation": "#91B85C", "water": "#538AB0"}

// LabelFor builds the project label for one object kind with the shared colour map.
func LabelFor(project, kind string) annotation.Label {
	color := labelColors[kind]
	if color == "" {
		color = "#9A93C8"
	}
	return annotation.Label{Base: annotation.Base{ID: project + "-kind-" + kind}, ProjectID: project, Name: strings.ReplaceAll(kind, "_", " "), Color: color}
}

type localModel struct {
	ID               string          `json:"id"`
	Title            string          `json:"title"`
	ImageSHA256      string          `json:"image_sha256"`
	Width            float64         `json:"image_width"`
	Height           float64         `json:"image_height"`
	Signature        string          `json:"model_signature"`
	ContextExpansion json.RawMessage `json:"context_expansion"`
}

type localReview struct {
	Revision  int    `json:"revision"`
	Signature string `json:"model_signature"`
	UpdatedBy string `json:"updated_by"`
	UpdatedAt string `json:"updated_at"`
}

type mirrorItem struct {
	ID             string
	Revision       int64
	Raw            json.RawMessage
	SceneID        string
	ModelSignature string
	ImageSHA256    string
	ReviewRevision int
	HasReview      bool
	Kind           string
	Marked         bool
}

type mirrorIndex struct {
	items  map[string]mirrorItem
	labels map[string]bool
}

func indexMirror(m annotationsync.Mirror) (*mirrorIndex, error) {
	idx := &mirrorIndex{items: map[string]mirrorItem{}, labels: map[string]bool{}}
	for _, raw := range m.Labels {
		var label struct {
			ID string `json:"id"`
		}
		if json.Unmarshal(raw, &label) == nil {
			idx.labels[label.ID] = true
		}
	}
	for _, raw := range m.Items {
		var doc struct {
			ID       string `json:"id"`
			Revision int64  `json:"revision"`
			Adapter  string `json:"adapter"`
			Source   struct {
				SceneID        string `json:"sceneId"`
				ModelSignature string `json:"modelSignature"`
				ImageSHA256    string `json:"imageSha256"`
				Review         any    `json:"review"`
			} `json:"source"`
			Metadata struct {
				Addr struct {
					Kind                 string `json:"kind"`
					SourceReviewRevision *int   `json:"sourceReviewRevision"`
					SupersededBy         any    `json:"supersededBy"`
					Generation           *struct {
						ReviewRevision int `json:"reviewRevision"`
					} `json:"generation"`
				} `json:"addr"`
			} `json:"metadata"`
		}
		if err := json.Unmarshal(raw, &doc); err != nil {
			return nil, fmt.Errorf("invalid mirror item: %w", err)
		}
		if doc.Adapter != "addr-scene" {
			continue
		}
		item := mirrorItem{ID: doc.ID, Revision: doc.Revision, Raw: raw, SceneID: doc.Source.SceneID, ModelSignature: doc.Source.ModelSignature, ImageSHA256: doc.Source.ImageSHA256, Kind: doc.Metadata.Addr.Kind, Marked: doc.Metadata.Addr.SupersededBy != nil, HasReview: doc.Source.Review != nil}
		if doc.Metadata.Addr.SourceReviewRevision != nil {
			item.ReviewRevision = *doc.Metadata.Addr.SourceReviewRevision
		} else if doc.Metadata.Addr.Generation != nil {
			item.ReviewRevision = doc.Metadata.Addr.Generation.ReviewRevision
		}
		idx.items[doc.ID] = item
	}
	return idx, nil
}

// ListScenes enumerates local scene directories that hold a latest.json.
func ListScenes(root string) ([]string, error) {
	entries, err := os.ReadDir(filepath.Join(root, "data", "scenes"))
	if err != nil {
		return nil, err
	}
	scenes := []string{}
	for _, entry := range entries {
		if !entry.IsDir() || !idPattern.MatchString(entry.Name()) {
			continue
		}
		if _, err := os.Stat(filepath.Join(root, "data", "scenes", entry.Name(), "latest.json")); err == nil {
			scenes = append(scenes, entry.Name())
		}
	}
	sort.Strings(scenes)
	return scenes, nil
}

// InputsDigest hashes the local inputs of one scene: latest.json bytes,
// review.json bytes (if any) and the PNG filename. It detects local changes
// between planning and applying and feeds PlanHash.
func InputsDigest(root, sceneID string) (string, error) {
	dir := filepath.Join(root, "data", "scenes", sceneID)
	hash := sha256.New()
	for _, name := range []string{"latest.json", "review.json"} {
		file, err := os.Open(filepath.Join(dir, name))
		if errors.Is(err, os.ErrNotExist) {
			fmt.Fprintf(hash, "%s:absent\n", name)
			continue
		}
		if err != nil {
			return "", err
		}
		part := sha256.New()
		_, copyErr := copyTo(part, file)
		file.Close()
		if copyErr != nil {
			return "", copyErr
		}
		fmt.Fprintf(hash, "%s:%s\n", name, hex.EncodeToString(part.Sum(nil)))
	}
	var model struct {
		ImageSHA256 string `json:"image_sha256"`
	}
	if data, err := os.ReadFile(filepath.Join(dir, "latest.json")); err == nil {
		_ = json.Unmarshal(data, &model)
	}
	fmt.Fprintf(hash, "png:%s.png\n", model.ImageSHA256)
	return hex.EncodeToString(hash.Sum(nil)), nil
}

// FeedbackReceiptMatches reports whether the local app accepted exactly this
// mirror document (feedback-store.ts receipt: remote.sha256 == sha256(raw)).
func FeedbackReceiptMatches(feedbackRoot, origin, project, itemID string, raw json.RawMessage) bool {
	if feedbackRoot == "" {
		return false
	}
	path := filepath.Join(feedbackRoot, migration.Digest([]byte(origin)), project, "receipts", migration.Digest([]byte(itemID))+".json")
	data, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	var receipt struct {
		ItemID string `json:"itemId"`
		Remote struct {
			SHA256 string `json:"sha256"`
		} `json:"remote"`
	}
	return json.Unmarshal(data, &receipt) == nil && receipt.ItemID == itemID && receipt.Remote.SHA256 == migration.Digest(raw)
}

// BuildPlan classifies every selected scene without writing to Fieldwork. The
// only network use is the optional Head probe for asset existence.
func BuildPlan(ctx context.Context, in Inputs) (*Plan, error) {
	if in.Root == "" || in.Project == "" || in.Receipts == nil {
		return nil, errors.New("push plan requires a root, project and receipts index")
	}
	if in.ReviewHistory == "" {
		in.ReviewHistory = "omit"
	}
	if in.ReviewHistory != "omit" && in.ReviewHistory != "split" {
		return nil, errors.New("--review-history must be omit or split")
	}
	if in.RunID == "" {
		in.RunID = NewRunID()
	}
	if !ValidRunID(in.RunID) {
		return nil, errors.New("invalid run id")
	}
	if in.Now == nil {
		in.Now = time.Now
	}
	if in.Mirror.ProjectID != in.Project {
		return nil, errors.New("mirror belongs to a different project")
	}
	idx, err := indexMirror(in.Mirror)
	if err != nil {
		return nil, err
	}
	scenes := in.Scenes
	if len(scenes) == 0 {
		scenes, err = ListScenes(in.Root)
		if err != nil {
			return nil, err
		}
	}
	plan := &Plan{SchemaVersion: 1, RunID: in.RunID, Origin: in.Mirror.Origin, ProjectID: in.Project, Root: in.Root, MirrorCursor: in.Mirror.Cursor, CreatedAt: in.Now().UTC().Format(time.RFC3339Nano), Options: PlanOptions{ReviewHistory: in.ReviewHistory, SkipOutsideInventory: in.SkipOutsideInventory, AllowUnrecovered: in.AllowUnrecovered, MarkSuperseded: in.MarkSuperseded}, Scenes: []ScenePlan{}, Labels: []annotation.Label{}, Counts: map[Action]int{}}
	planHash := sha256.New()
	headCache := map[string]bool{}
	neededLabels := map[string]bool{}
	for _, sceneID := range scenes {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if !idPattern.MatchString(sceneID) {
			return nil, fmt.Errorf("invalid scene id %q", sceneID)
		}
		digest, err := InputsDigest(in.Root, sceneID)
		if err != nil {
			return nil, fmt.Errorf("scene %s: %w", sceneID, err)
		}
		fmt.Fprintf(planHash, "%s %s\n", sceneID, digest)
		sp, item, err := planScene(ctx, in, idx, sceneID, plan)
		if err != nil {
			return nil, fmt.Errorf("scene %s: %w", sceneID, err)
		}
		if sp == nil {
			continue
		}
		sp.InputsSHA256 = digest
		if sp.Action == ActionCreate {
			for i := range sp.Assets {
				asset := &sp.Assets[i]
				exists := true
				for c, chunk := range asset.ChunkSHA256 {
					known, seen := headCache[chunk]
					if !seen && in.Head != nil {
						known, err = in.Head(ctx, chunk, asset.ChunkBytes[c])
						if err != nil {
							return nil, fmt.Errorf("scene %s asset %s: %w", sceneID, asset.Path, err)
						}
						headCache[chunk] = known
					} else if !seen {
						known = false
					}
					if !known {
						exists = false
						plan.BytesToUpload += asset.ChunkBytes[c]
						headCache[chunk] = true // counted once across scenes
					}
				}
				asset.Exists = exists
			}
			for _, a := range item.Annotations {
				if !idx.labels[a.LabelID] && !neededLabels[a.LabelID] {
					neededLabels[a.LabelID] = true
					plan.Labels = append(plan.Labels, LabelFor(in.Project, strings.TrimPrefix(a.LabelID, in.Project+"-kind-")))
				}
			}
			if err := writeRequest(in.Receipts.Dir, in.RunID, sp.ItemID, item); err != nil {
				return nil, err
			}
		}
		plan.Counts[sp.Action]++
		plan.Scenes = append(plan.Scenes, *sp)
	}
	sort.Slice(plan.Labels, func(i, j int) bool { return plan.Labels[i].ID < plan.Labels[j].ID })
	if len(plan.Labels) > 0 {
		plan.Counts[ActionNeedsLabel] = len(plan.Labels)
	}
	plan.PlanHash = hex.EncodeToString(planHash.Sum(nil))
	return plan, nil
}

func requestPath(dir, runID, itemID string) string {
	return filepath.Join(dir, "runs", runID, "items", itemID+".request.json")
}

func writeRequest(dir, runID, itemID string, item *annotation.Item) error {
	data, err := json.Marshal(item)
	if err != nil {
		return err
	}
	return writeAtomic(requestPath(dir, runID, itemID), data)
}

// ReadRequest loads the frozen request document of a planned item.
func ReadRequest(dir, runID, itemID string) (json.RawMessage, error) {
	data, err := os.ReadFile(requestPath(dir, runID, itemID))
	if err != nil {
		return nil, err
	}
	if !json.Valid(data) {
		return nil, fmt.Errorf("request document %s is not valid JSON", itemID)
	}
	return data, nil
}

func actorKind(updatedBy string) string {
	switch {
	case updatedBy == "local":
		return "local"
	case strings.HasPrefix(updatedBy, "machine:"):
		return "machine"
	}
	return "unknown"
}

func planScene(ctx context.Context, in Inputs, idx *mirrorIndex, sceneID string, plan *Plan) (*ScenePlan, *annotation.Item, error) {
	sp := &ScenePlan{SceneID: sceneID, Reasons: []string{}, Assets: []AssetPlan{}, Supersedes: []Supersedes{}, LiveItems: []LiveItem{}}
	prefix := "data/scenes/" + sceneID + "/"
	sceneDir := filepath.Join(in.Root, "data", "scenes", sceneID)
	frozenID := migration.SceneID(in.Project, sceneID)
	frozen, inInventory := idx.items[frozenID]
	sp.Inventory = InventoryOutside
	if inInventory {
		sp.Inventory = InventoryFrozen
	} else if in.SkipOutsideInventory {
		return nil, nil, nil
	}
	fail := func(action Action, reason string) (*ScenePlan, *annotation.Item, error) {
		sp.Action = action
		sp.Reasons = append(sp.Reasons, reason)
		return sp, nil, nil
	}
	if _, err := os.Stat(filepath.Join(sceneDir, "review.lock")); err == nil {
		return fail(ActionBlockedReviewLock, "review.lock exists; finish or release the local review first")
	}
	latestRaw, err := os.ReadFile(filepath.Join(sceneDir, "latest.json"))
	if err != nil {
		return fail(ActionInvalidFrame, "latest.json unreadable: "+err.Error())
	}
	var model localModel
	if err := json.Unmarshal(latestRaw, &model); err != nil || model.ID != sceneID || !digestPattern.MatchString(model.Signature) || !digestPattern.MatchString(model.ImageSHA256) || model.Width <= 0 || model.Height <= 0 {
		return fail(ActionInvalidFrame, "latest.json has an invalid id, signature or frame")
	}
	overrides := map[string][]byte{}
	var review *localReview
	var headBytes, historyBytes []byte
	reviewPath := filepath.Join(sceneDir, "review.json")
	if raw, err := os.ReadFile(reviewPath); err == nil {
		headBytes, historyBytes, err = SplitReviewHistory(raw)
		if err != nil {
			return fail(ActionInvalidFrame, "review.json: "+err.Error())
		}
		review = &localReview{}
		if err := json.Unmarshal(headBytes, review); err != nil {
			return fail(ActionInvalidFrame, "review.json head: "+err.Error())
		}
		if review.Signature != model.Signature {
			return fail(ActionBlockedStaleReview, fmt.Sprintf("review.json model_signature %s differs from latest.json %s; %s", short(review.Signature), short(model.Signature), StaleReviewHint))
		}
		overrides[prefix+"review.json"] = headBytes
		if in.ReviewHistory == "split" && len(historyBytes) > 0 {
			overrides[prefix+"review.history.json"] = historyBytes
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, nil, err
	}
	reviewRevision := 0
	if review != nil {
		reviewRevision = review.Revision
	}
	sp.Generation = Generation{ModelSignature: model.Signature, ReviewRevision: reviewRevision, ImageSHA256: model.ImageSHA256, Width: model.Width, Height: model.Height}
	sp.ItemID = migration.GenerationSceneID(in.Project, sceneID, model.Signature, reviewRevision)

	// Live items: the frozen id plus every generation our receipts created.
	if inInventory {
		expected, _ := in.Receipts.ExpectedRevision(frozenID, true)
		sp.LiveItems = append(sp.LiveItems, LiveItem{ItemID: frozenID, Kind: "frozen", Revision: frozen.Revision, Expected: expected, Marked: frozen.Marked, ModelSignature: frozen.ModelSignature, ImageSHA256: frozen.ImageSHA256})
	}
	sceneReceipt, _ := in.Receipts.Scene(sceneID)
	for _, id := range sceneReceipt.ItemIDs {
		rec, _ := in.Receipts.Item(id)
		live := LiveItem{ItemID: id, Kind: "generation", Expected: rec.Revision, ModelSignature: rec.Generation.ModelSignature, ImageSHA256: rec.Generation.ImageSHA256}
		if m, ok := idx.items[id]; ok {
			live.Revision, live.Marked = m.Revision, m.Marked
		} else {
			live.Missing = true
		}
		sp.LiveItems = append(sp.LiveItems, live)
	}

	// Remote edits since our last own write block the push unless the local
	// app already accepted exactly that document (FeedbackReceipt). This runs
	// before the current check: live items that moved past their receipts must
	// be pulled first even when the local model/review has nothing new.
	for i := range sp.LiveItems {
		live := &sp.LiveItems[i]
		if live.Missing {
			sp.Unrecovered = append(sp.Unrecovered, Unrecovered{ItemID: live.ItemID, Revision: live.Expected})
			sp.Reasons = append(sp.Reasons, fmt.Sprintf("%s is missing from the mirror although receipts record revision %d", live.ItemID, live.Expected))
			continue
		}
		if live.Revision == live.Expected {
			continue
		}
		raw := idx.items[live.ItemID].Raw
		if in.Receipts.OwnWrite(live.ItemID, live.Revision, raw) {
			live.Recovered = "own-write"
			continue
		}
		if FeedbackReceiptMatches(in.FeedbackReceiptRoot, in.Mirror.Origin, in.Project, live.ItemID, raw) {
			live.Recovered = "feedback-receipt"
			continue
		}
		sp.Unrecovered = append(sp.Unrecovered, Unrecovered{ItemID: live.ItemID, Revision: live.Revision})
		sp.Reasons = append(sp.Reasons, fmt.Sprintf("%s is at revision %d in Fieldwork but our receipts expect %d (Fieldwork edits not yet pulled)", live.ItemID, live.Revision, live.Expected))
	}
	if len(sp.Unrecovered) > 0 && !in.AllowUnrecovered {
		sp.Action = ActionBlockedRemoteEdits
		sp.Reasons = append(sp.Reasons, RemoteEditsHint)
		return sp, nil, nil
	}

	// Current: the latest published generation already carries this model and
	// review revision. Evaluated only after the remote-edit check above, so a
	// Fieldwork edit on a scene with nothing new to publish still surfaces as
	// blocked_remote_edits (with --allow-unrecovered the scene stays current and
	// the moved items are recorded in unrecoveredFieldwork/reasons instead).
	var latest *LiveItem
	if sceneReceipt.LatestItemID != "" {
		for i := range sp.LiveItems {
			if sp.LiveItems[i].ItemID == sceneReceipt.LatestItemID {
				latest = &sp.LiveItems[i]
			}
		}
	} else if inInventory {
		latest = &sp.LiveItems[0]
	}
	if latest != nil {
		publishedSig, publishedRev := latest.ModelSignature, 0
		if latest.Kind == "frozen" {
			publishedRev = frozen.ReviewRevision
			if !frozen.HasReview {
				publishedRev = 0
			}
		} else {
			latestReceipt, _ := in.Receipts.Item(latest.ItemID)
			publishedRev = latestReceipt.Generation.ReviewRevision
		}
		if publishedSig == model.Signature && publishedRev == reviewRevision {
			sp.ItemID = latest.ItemID
			sp.Action = ActionCurrent
			sp.Reasons = append(sp.Reasons, fmt.Sprintf("%s already carries model %s at review revision %d", latest.ItemID, short(model.Signature), reviewRevision))
			return sp, nil, nil
		}
	}

	if err := CheckFrame(in.Root, sceneID, Frame{ImageSHA256: model.ImageSHA256, Width: model.Width, Height: model.Height}); err != nil {
		return fail(ActionInvalidFrame, err.Error())
	}

	files := migration.NewLocalSceneFiles(in.Root, sceneID, overrides)
	assetPaths := []string{prefix + model.ImageSHA256 + ".png", prefix + "latest.json"}
	if review != nil {
		assetPaths = append(assetPaths, prefix+"models/"+review.Signature+".json", prefix+"review.json")
		if _, ok := overrides[prefix+"review.history.json"]; ok {
			assetPaths = append(assetPaths, prefix+"review.history.json")
		}
	}
	if files.Has(prefix + "confidence.json") {
		assetPaths = append(assetPaths, prefix+"confidence.json")
	}
	for _, live := range sp.LiveItems {
		if !live.Marked && !live.Missing {
			sp.Supersedes = append(sp.Supersedes, Supersedes{ItemID: live.ItemID, Revision: live.Revision, ModelSignature: live.ModelSignature, ImageSHA256: live.ImageSHA256})
		}
	}
	generation := annotation.Object{"modelSignature": model.Signature, "reviewRevision": reviewRevision, "imageSha256": model.ImageSHA256, "width": model.Width, "height": model.Height, "supersedes": sp.Supersedes, "inventory": sp.Inventory, "contextExpansion": nil}
	if len(model.ContextExpansion) > 0 && string(model.ContextExpansion) != "null" {
		generation["contextExpansion"] = model.ContextExpansion
	}
	if review != nil {
		headRef, err := files.Ref(prefix + "review.json")
		if err != nil {
			return nil, nil, err
		}
		sp.Generation.ReviewHeadSHA256 = headRef["sha256"].(string)
		generation["reviewHeadSha256"] = sp.Generation.ReviewHeadSHA256
		generation["reviewActor"] = review.UpdatedBy
		generation["actorKind"] = actorKind(review.UpdatedBy)
		generation["reviewUpdatedAt"] = review.UpdatedAt
		if _, ok := overrides[prefix+"review.history.json"]; ok {
			historyRef, err := files.Ref(prefix + "review.history.json")
			if err != nil {
				return nil, nil, err
			}
			sp.Generation.ReviewHistorySHA256 = historyRef["sha256"].(string)
			generation["reviewHistorySha256"] = sp.Generation.ReviewHistorySHA256
		}
	}
	meta := annotation.Object{"schemaVersion": 2, "kind": "generation", "sceneId": sceneID, "generation": generation, "push": annotation.Object{"runId": in.RunID, "pushedAt": plan.CreatedAt, "tool": "annotate push", "mirrorCursor": in.Mirror.Cursor}}
	if len(sp.Unrecovered) > 0 {
		meta["unrecoveredFieldwork"] = sp.Unrecovered
	}
	item, _, err := migration.ProjectScene(ctx, files, in.Project, sceneID, migration.ProjectOptions{ItemID: sp.ItemID, ExtraBindings: true, Metadata: meta, Tags: []string{in.Project, "pushed", "gen"}, AssetPaths: assetPaths})
	if err != nil {
		if strings.Contains(err.Error(), "different image frame") {
			return fail(ActionBlockedStaleReview, err.Error()+"; "+StaleReviewHint)
		}
		return fail(ActionInvalidFrame, err.Error())
	}
	if generation["contextExpansion"] != nil {
		item.Tags = append(item.Tags, "expanded-1024")
	}
	if sp.Inventory == InventoryOutside {
		item.Tags = append(item.Tags, "outside-inventory")
	}
	if len(item.Title) > 300 {
		item.Title = item.Title[:300]
	}
	sp.Tags = item.Tags
	data, err := json.Marshal(item)
	if err != nil {
		return nil, nil, err
	}
	sp.DocumentBytes = len(data)
	sp.Annotations = len(item.Annotations)
	if len(data) > annotation.MaxDocumentBytes-512 || len(item.Annotations) > MaxAnnotations {
		return fail(ActionTooLarge, fmt.Sprintf("document is %d bytes with %d annotations; limits are %d bytes and %d annotations (not trimmed)", len(data), len(item.Annotations), annotation.MaxDocumentBytes-512, MaxAnnotations))
	}
	for _, path := range assetPaths {
		file, err := files.File(path)
		if err != nil {
			return nil, nil, err
		}
		asset := AssetPlan{Path: path, SHA256: file.SHA256, ContentType: file.ContentType, Bytes: file.Bytes, Chunks: len(file.Chunks), Evidence: strings.HasSuffix(path, ".json") && file.Bytes <= annotation.MaxAssetBytes}
		for _, chunk := range file.Chunks {
			asset.ChunkSHA256 = append(asset.ChunkSHA256, chunk.SHA256)
			asset.ChunkBytes = append(asset.ChunkBytes, chunk.Bytes)
		}
		sp.Assets = append(sp.Assets, asset)
	}
	sp.Action = ActionCreate
	return sp, &item, nil
}

func short(sig string) string {
	if len(sig) > 12 {
		return sig[:12]
	}
	return sig
}
