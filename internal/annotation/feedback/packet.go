// Package feedback prepares verified cloud feedback for explicit local review.
// It never writes scene files, accepts training labels, or mutates the cloud.
package feedback

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"

	"github.com/benebsworth/addr/internal/annotation/migration"
	"github.com/benebsworth/addr/internal/annotation/push"
	"github.com/benebsworth/addr/internal/annotationsync"
)

const MaxPacketBytes = 64 << 20

// Packet schema versions. Schema 1 compares every mirror item with the frozen
// bundle only. Schema 2 is emitted when push receipts are consulted: entries
// carry a Binding and generation items are baselined on their receipt.
const (
	SchemaFrozenOnly   = 1
	SchemaWithReceipts = 2
)

// Binding kinds.
const (
	BindingFrozen = "frozen" // baseline is the frozen bundle projection; no push receipt
	BindingPush   = "push"   // a push receipt exists: generation baseline or a supersede-marked frozen id
)

// Quarantine reasons added by schema 2.
const (
	ReasonMissingPushReceipt     = "missing_push_receipt"
	ReasonReceiptBindingMismatch = "receipt_binding_mismatch"
)

// Evidence keeps exact JSON bytes in a string across Go/JavaScript transports.
// Hashes cover the UTF-8 string, not a parsed/re-serialized JSON document.
type Evidence struct {
	SHA256 string `json:"sha256"`
	JSON   string `json:"json"`
}

// Binding says which baseline an entry was compared against. Kind and
// ReceiptSHA256 are part of the packet digest. ReceiptSHA256 is the SHA-256 of
// the compact JSON of the push receipt consulted (so a later mark changes the
// digest and forces a rebuild); ReceiptRevision is the revision our token last
// wrote. Generation carries the pushed frame (model signature, image sha,
// width/height, review revision) so the local app can recover edits made on an
// older frame. Nothing here is a human decision.
type Binding struct {
	Kind            string           `json:"kind"`
	RunID           string           `json:"runId,omitempty"`
	ReceiptSHA256   string           `json:"receiptSha256,omitempty"`
	ReceiptRevision int64            `json:"receiptRevision,omitempty"`
	Generation      *push.Generation `json:"generation,omitempty"`
}

type Entry struct {
	ItemID      string    `json:"itemId"`
	SceneID     string    `json:"sceneId"`
	BaseItem    Evidence  `json:"baseItem"`
	Item        Evidence  `json:"item"`
	Model       Evidence  `json:"model"`
	Review      *Evidence `json:"review,omitempty"`
	ReviewModel *Evidence `json:"reviewModel,omitempty"`
	Actor       string    `json:"actor,omitempty"`
	Timestamp   string    `json:"timestamp,omitempty"`
	Binding     *Binding  `json:"binding,omitempty"` // schema 2 only
}

type Quarantine struct {
	ItemID string `json:"itemId"`
	Reason string `json:"reason"`
}

type Packet struct {
	SchemaVersion  int          `json:"schemaVersion"`
	Origin         string       `json:"origin"`
	ProjectID      string       `json:"projectId"`
	SnapshotID     string       `json:"snapshotId"`
	ManifestSHA256 string       `json:"manifestSha256"`
	Cursor         string       `json:"cursor"`
	Selection      string       `json:"selection,omitempty"`
	Entries        []Entry      `json:"entries"`
	Quarantined    []Quarantine `json:"quarantined"`
	Digest         string       `json:"digest"`
}

func evidence(raw []byte) Evidence { return Evidence{migration.Digest(raw), string(raw)} }

// Build always compares the current mirror with the frozen baseline, not the
// last sync cursor. Unapplied edits therefore remain available after a no-op pull.
//
// With receipts (nil keeps schema-1 behaviour) every item our push wrote is
// compared against what we wrote: a generation id is baselined on its receipt's
// pristine document and evidenced from push/assets; a supersede-marked frozen id
// keeps the frozen projection as baseline (the mark is stripped for the
// nothing-new check). An item is skipped only when it is exactly our last write
// and carries nothing beyond the baseline plus the mark; a Fieldwork edit that a
// push embedded (--allow-unrecovered) is therefore still delivered.
func Build(ctx context.Context, mirror annotationsync.Mirror, bundle *migration.Bundle, selection string, receipts *push.ReceiptIndex) (*Packet, error) {
	plan, err := migration.BuildPlan(ctx, bundle, mirror.ProjectID)
	if err != nil {
		return nil, fmt.Errorf("frozen projection: %w", err)
	}
	schema := SchemaFrozenOnly
	if receipts != nil {
		schema = SchemaWithReceipts
	}
	p := &Packet{SchemaVersion: schema, Origin: mirror.Origin, ProjectID: mirror.ProjectID, SnapshotID: bundle.Manifest.SnapshotID, ManifestSHA256: migration.Digest(bundle.ManifestBytes), Cursor: mirror.Cursor, Selection: selection, Entries: []Entry{}, Quarantined: []Quarantine{}}
	bound := false
	for _, raw := range mirror.Items {
		var report struct {
			ID      string `json:"id"`
			Adapter string `json:"adapter"`
			Source  struct {
				Manifest struct {
					SHA256 string `json:"sha256"`
					URL    string `json:"url"`
					Bytes  int64  `json:"bytes"`
				} `json:"manifest"`
			} `json:"source"`
		}
		if json.Unmarshal(raw, &report) == nil && report.ID == migration.ReportID(mirror.ProjectID) && report.Adapter == "addr-report" {
			ref := report.Source.Manifest
			bound = ref.SHA256 == p.ManifestSHA256 && ref.URL == "/api/v1/assets/"+p.ManifestSHA256 && ref.Bytes == int64(len(bundle.ManifestBytes))
			break
		}
	}
	if !bound {
		return nil, errors.New("mirror dataset report does not bind this frozen manifest")
	}
	baseline := map[string]json.RawMessage{}
	for _, item := range plan.Items {
		if item.Adapter == "addr-scene" {
			raw, err := json.Marshal(item)
			if err != nil {
				return nil, err
			}
			baseline[item.ID] = raw
		}
	}
	seen := map[string]bool{}
	for _, raw := range mirror.Items {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		var item struct {
			ID        string                     `json:"id"`
			Revision  int64                      `json:"revision"`
			Adapter   string                     `json:"adapter"`
			Source    map[string]json.RawMessage `json:"source"`
			UpdatedAt string                     `json:"updatedAt"`
			Metadata  struct {
				Addr struct {
					Kind string `json:"kind"`
				} `json:"addr"`
			} `json:"metadata"`
		}
		if err := json.Unmarshal(raw, &item); err != nil {
			return nil, fmt.Errorf("invalid mirror item: %w", err)
		}
		if selection != "" && item.ID != selection {
			continue
		}
		base, exists := baseline[item.ID]
		if exists {
			seen[item.ID] = true
		}
		if item.Adapter != "addr-scene" {
			if exists {
				p.Quarantined = append(p.Quarantined, Quarantine{item.ID, "changed_adapter"})
			}
			continue
		}
		var scene string
		_ = json.Unmarshal(item.Source["sceneId"], &scene)
		frozenID := migration.SceneID(mirror.ProjectID, scene)
		var receipt *push.ItemReceipt
		if receipts != nil {
			if rec, ok := receipts.Item(item.ID); ok {
				receipt = &rec
			}
		}
		source, _ := json.Marshal(item.Source)
		var binding *Binding
		var evidenceFrom func(ref json.RawMessage) (Evidence, error)
		switch {
		case exists && item.ID == frozenID:
			// Frozen id: the frozen projection stays the baseline.
			var baseFields map[string]json.RawMessage
			if err := json.Unmarshal(base, &baseFields); err != nil {
				return nil, err
			}
			if !sameJSON(source, baseFields["source"]) {
				p.Quarantined = append(p.Quarantined, Quarantine{item.ID, "changed_immutable_source"})
				continue
			}
			// A later revision can intentionally revert an earlier accepted edit to
			// the frozen content. Deliver it so local receipts can undo that edit.
			if item.Revision == 1 && sameDocument(base, raw) {
				continue
			}
			if receipt != nil {
				if receipts.OwnWrite(item.ID, item.Revision, raw) && sameDocument(stripMark(raw), stripMark(base)) {
					continue
				}
				binding = pushBinding(*receipt, base)
			} else if schema == SchemaWithReceipts {
				binding = &Binding{Kind: BindingFrozen}
			}
			evidenceFrom = func(ref json.RawMessage) (Evidence, error) { return sourceEvidence(ctx, bundle, ref) }
		case receipt != nil:
			// Generation id our push created: the receipt's pristine document is the baseline.
			seen[item.ID] = true
			sceneReceipt, _ := receipts.Scene(scene)
			if len(receipt.Baseline) == 0 || receipt.SceneID != scene || !containsString(sceneReceipt.ItemIDs, item.ID) {
				p.Quarantined = append(p.Quarantined, Quarantine{item.ID, ReasonReceiptBindingMismatch})
				continue
			}
			base = receipt.Baseline
			var baseFields map[string]json.RawMessage
			if err := json.Unmarshal(base, &baseFields); err != nil {
				return nil, fmt.Errorf("push receipt %s baseline: %w", item.ID, err)
			}
			if !sameJSON(source, baseFields["source"]) {
				p.Quarantined = append(p.Quarantined, Quarantine{item.ID, ReasonReceiptBindingMismatch})
				continue
			}
			if receipts.OwnWrite(item.ID, item.Revision, raw) && sameDocument(stripMark(raw), stripMark(base)) {
				continue
			}
			binding = pushBinding(*receipt, base)
			evidenceFrom = func(ref json.RawMessage) (Evidence, error) { return receiptEvidence(receipts, ref) }
		default:
			reason := "unknown_or_noncanonical_scene"
			if schema == SchemaWithReceipts && (item.Metadata.Addr.Kind == "generation" || scene != "" && strings.HasPrefix(item.ID, frozenID+"-m")) {
				reason = ReasonMissingPushReceipt
			}
			p.Quarantined = append(p.Quarantined, Quarantine{item.ID, reason})
			continue
		}
		entry := Entry{ItemID: item.ID, SceneID: scene, BaseItem: evidence(base), Item: evidence(raw), Timestamp: item.UpdatedAt, Binding: binding}
		if provenance, ok := mirror.ItemProvenance[item.ID]; ok {
			entry.Actor, entry.Timestamp = provenance.Actor, provenance.Timestamp
		}
		var baseFields map[string]json.RawMessage
		if err := json.Unmarshal(base, &baseFields); err != nil {
			return nil, err
		}
		var sourceRefs map[string]json.RawMessage
		if err := json.Unmarshal(baseFields["source"], &sourceRefs); err != nil {
			return nil, err
		}
		entry.Model, err = evidenceFrom(sourceRefs["model"])
		if err != nil {
			return nil, fmt.Errorf("scene %s model evidence: %w", scene, err)
		}
		for key, destination := range map[string]**Evidence{"review": &entry.Review, "reviewModel": &entry.ReviewModel} {
			if len(sourceRefs[key]) == 0 {
				continue
			}
			value, err := evidenceFrom(sourceRefs[key])
			if err != nil {
				return nil, fmt.Errorf("scene %s %s evidence: %w", scene, key, err)
			}
			*destination = &value
		}
		p.Entries = append(p.Entries, entry)
	}
	for id := range baseline {
		if !seen[id] && (selection == "" || id == selection) {
			p.Quarantined = append(p.Quarantined, Quarantine{id, "missing_remote_scene"})
		}
	}
	if selection != "" && len(p.Entries) == 0 && len(p.Quarantined) == 0 && !seen[selection] {
		p.Quarantined = append(p.Quarantined, Quarantine{selection, "unknown_or_noncanonical_scene"})
	}
	sort.Slice(p.Entries, func(i, j int) bool { return p.Entries[i].ItemID < p.Entries[j].ItemID })
	sort.Slice(p.Quarantined, func(i, j int) bool {
		a, b := p.Quarantined[i], p.Quarantined[j]
		return a.ItemID < b.ItemID || a.ItemID == b.ItemID && a.Reason < b.Reason
	})
	p.Digest, err = p.ComputeDigest()
	return p, err
}

func containsString(list []string, want string) bool {
	for _, value := range list {
		if value == want {
			return true
		}
	}
	return false
}

// pushBinding describes the receipt an entry was compared against. The frame
// comes from the receipt's Generation when the push recorded one (generation
// ids) and from the baseline document's immutable source otherwise (frozen ids
// that only carry a supersede mark).
func pushBinding(receipt push.ItemReceipt, base json.RawMessage) *Binding {
	canonical, _ := json.Marshal(receipt)
	generation := receipt.Generation
	if generation.ModelSignature == "" {
		generation = documentGeneration(base)
	}
	return &Binding{Kind: BindingPush, RunID: receipt.RunID, ReceiptSHA256: migration.Digest(canonical), ReceiptRevision: receipt.Revision, Generation: &generation}
}

// documentGeneration reads the immutable frame of an addr-scene document.
func documentGeneration(raw json.RawMessage) push.Generation {
	var doc struct {
		Source struct {
			ModelSignature string  `json:"modelSignature"`
			ImageSHA256    string  `json:"imageSha256"`
			Width          float64 `json:"width"`
			Height         float64 `json:"height"`
		} `json:"source"`
		Metadata struct {
			Addr struct {
				SourceReviewRevision *int `json:"sourceReviewRevision"`
				Generation           *struct {
					ReviewRevision int `json:"reviewRevision"`
				} `json:"generation"`
			} `json:"addr"`
		} `json:"metadata"`
	}
	_ = json.Unmarshal(raw, &doc)
	generation := push.Generation{ModelSignature: doc.Source.ModelSignature, ImageSHA256: doc.Source.ImageSHA256, Width: doc.Source.Width, Height: doc.Source.Height}
	if doc.Metadata.Addr.SourceReviewRevision != nil {
		generation.ReviewRevision = *doc.Metadata.Addr.SourceReviewRevision
	} else if doc.Metadata.Addr.Generation != nil {
		generation.ReviewRevision = doc.Metadata.Addr.Generation.ReviewRevision
	}
	return generation
}

// stripMark removes the push supersede mark (tag superseded-frame,
// metadata.addr.supersededBy, metadata.addr.stale) so a document can be
// compared with its pre-mark baseline. Containers left empty are dropped; apply
// it to both sides so the comparison stays symmetric. Number spellings are kept.
func stripMark(raw json.RawMessage) json.RawMessage {
	var doc map[string]json.RawMessage
	if json.Unmarshal(raw, &doc) != nil || doc == nil {
		return raw
	}
	var tags []string
	if json.Unmarshal(doc["tags"], &tags) == nil {
		kept := []string{}
		for _, tag := range tags {
			if tag != push.TagSuperseded {
				kept = append(kept, tag)
			}
		}
		if len(kept) == 0 {
			delete(doc, "tags")
		} else {
			doc["tags"], _ = json.Marshal(kept)
		}
	}
	var meta map[string]json.RawMessage
	if json.Unmarshal(doc["metadata"], &meta) == nil && meta != nil {
		var addr map[string]json.RawMessage
		if json.Unmarshal(meta["addr"], &addr) == nil && addr != nil {
			delete(addr, "supersededBy")
			delete(addr, "stale")
			if len(addr) == 0 {
				delete(meta, "addr")
			} else {
				meta["addr"], _ = json.Marshal(addr)
			}
		}
		if len(meta) == 0 {
			delete(doc, "metadata")
		} else {
			doc["metadata"], _ = json.Marshal(meta)
		}
	}
	out, err := json.Marshal(doc)
	if err != nil {
		return raw
	}
	return out
}

// receiptEvidence is the push counterpart of sourceEvidence: the exact JSON
// bytes our push uploaded, read back from push/assets/<sha> and hash-checked.
func receiptEvidence(receipts *push.ReceiptIndex, raw json.RawMessage) (Evidence, error) {
	var ref struct {
		SHA256 string `json:"sha256"`
		Bytes  int64  `json:"bytes"`
	}
	if json.Unmarshal(raw, &ref) != nil || ref.SHA256 == "" {
		return Evidence{}, errors.New("source reference lacks a hash")
	}
	data, err := receipts.Evidence(ref.SHA256)
	if err != nil {
		return Evidence{}, fmt.Errorf("push evidence: %w", err)
	}
	if ref.Bytes != 0 && ref.Bytes != int64(len(data)) || !json.Valid(data) {
		return Evidence{}, errors.New("push evidence does not match its immutable reference")
	}
	return evidence(data), nil
}

func sourceEvidence(ctx context.Context, bundle *migration.Bundle, raw json.RawMessage) (Evidence, error) {
	var ref struct{ Path, SHA256 string }
	if json.Unmarshal(raw, &ref) != nil || ref.Path == "" || ref.SHA256 == "" {
		return Evidence{}, errors.New("source reference lacks a frozen path or hash")
	}
	data, err := bundle.ReadFile(ctx, ref.Path)
	if err != nil {
		return Evidence{}, err
	}
	if migration.Digest(data) != ref.SHA256 || !json.Valid(data) {
		return Evidence{}, errors.New("source JSON does not match its immutable reference")
	}
	return evidence(data), nil
}

// ComputeDigest is shared with the local API's JSON.stringify(array) contract.
// All variable values are strings; literal U+2028/U+2029 are escaped by Go and
// by the API, and HTML escaping is disabled. No floating-point canonicalizer is
// needed for the wrapped evidence JSON. Schema 2 appends the binding kind and
// receipt sha to every entry tuple; schema 1 tuples are unchanged.
func (p *Packet) ComputeDigest() (string, error) {
	entries, quarantined := [][]string{}, [][]string{}
	for _, e := range p.Entries {
		review, reviewModel := "", ""
		if e.Review != nil {
			review = e.Review.SHA256
		}
		if e.ReviewModel != nil {
			reviewModel = e.ReviewModel.SHA256
		}
		tuple := []string{e.ItemID, e.SceneID, e.BaseItem.SHA256, e.Item.SHA256, e.Model.SHA256, review, reviewModel, e.Actor, e.Timestamp}
		if p.SchemaVersion >= SchemaWithReceipts {
			kind, receiptSHA := "", ""
			if e.Binding != nil {
				kind, receiptSHA = e.Binding.Kind, e.Binding.ReceiptSHA256
			}
			tuple = append(tuple, kind, receiptSHA)
		}
		entries = append(entries, tuple)
	}
	for _, q := range p.Quarantined {
		quarantined = append(quarantined, []string{q.ItemID, q.Reason})
	}
	var out bytes.Buffer
	enc := json.NewEncoder(&out)
	enc.SetEscapeHTML(false)
	if err := enc.Encode([]any{p.SchemaVersion, p.Origin, p.ProjectID, p.SnapshotID, p.ManifestSHA256, p.Cursor, entries, quarantined, p.Selection}); err != nil {
		return "", err
	}
	return migration.Digest(bytes.TrimSuffix(out.Bytes(), []byte("\n"))), nil
}

// Select narrows a validated packet without changing its frozen evidence or
// cursor. Preview and apply must use the same selection to bind the same plan.
func (p *Packet) Select(itemID string) (*Packet, error) {
	if p == nil || itemID == "" || strings.TrimSpace(itemID) != itemID {
		return nil, errors.New("feedback selection requires an item ID")
	}
	if p.Selection != "" && p.Selection != itemID {
		return nil, errors.New("feedback packet is already bound to a different item")
	}
	selected := *p
	selected.Selection = itemID
	selected.Entries = []Entry{}
	selected.Quarantined = []Quarantine{}
	for _, entry := range p.Entries {
		if entry.ItemID == itemID {
			selected.Entries = append(selected.Entries, entry)
		}
	}
	for _, quarantine := range p.Quarantined {
		if quarantine.ItemID == itemID {
			selected.Quarantined = append(selected.Quarantined, quarantine)
		}
	}
	if len(selected.Entries) == 0 && len(selected.Quarantined) == 0 && p.Selection != itemID {
		return nil, errors.New("selected item is not present in the feedback packet")
	}
	var err error
	selected.Digest, err = selected.ComputeDigest()
	return &selected, err
}

// Read validates a packet before it is submitted to a local preview/apply API.
// Schema 1 entries carry no binding; every schema 2 entry carries one.
func Read(data []byte) (*Packet, error) {
	if len(data) > MaxPacketBytes {
		return nil, errors.New("feedback packet exceeds 64 MiB")
	}
	var p Packet
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if dec.Decode(&p) != nil || dec.Decode(new(any)) != io.EOF || p.SchemaVersion != SchemaFrozenOnly && p.SchemaVersion != SchemaWithReceipts || p.Entries == nil || p.Quarantined == nil {
		return nil, errors.New("invalid feedback packet schema")
	}
	for _, entry := range p.Entries {
		if p.SchemaVersion == SchemaFrozenOnly && entry.Binding != nil || p.SchemaVersion == SchemaWithReceipts && (entry.Binding == nil || entry.Binding.Kind != BindingFrozen && entry.Binding.Kind != BindingPush) {
			return nil, errors.New("feedback entry binding does not match the packet schema")
		}
		for _, wrapper := range []*Evidence{&entry.BaseItem, &entry.Item, &entry.Model, entry.Review, entry.ReviewModel} {
			if wrapper != nil && (!json.Valid([]byte(wrapper.JSON)) || migration.Digest([]byte(wrapper.JSON)) != wrapper.SHA256) {
				return nil, errors.New("feedback evidence hash or JSON mismatch")
			}
		}
	}
	digest, err := p.ComputeDigest()
	if err != nil || digest != p.Digest {
		return nil, errors.New("feedback packet digest mismatch")
	}
	return &p, nil
}

// Write publishes a complete packet by atomic rename. A failed build or write
// leaves the previous packet intact; its digest is stable for identical inputs.
func Write(ctx context.Context, path string, packet *Packet) error {
	data, err := json.MarshalIndent(packet, "", "  ")
	if err != nil {
		return err
	}
	if len(data)+1 > MaxPacketBytes {
		return errors.New("feedback packet exceeds 64 MiB")
	}
	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, 0700); err != nil {
		return err
	}
	file, err := os.CreateTemp(directory, ".feedback-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	defer file.Close()
	if _, err := file.Write(append(data, '\n')); err != nil {
		return err
	}
	if err := file.Sync(); err != nil {
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if err := os.Rename(file.Name(), path); err != nil {
		return err
	}
	dir, err := os.Open(directory)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func sameDocument(a, b json.RawMessage) bool {
	var left, right map[string]json.RawMessage
	if json.Unmarshal(a, &left) != nil || json.Unmarshal(b, &right) != nil {
		return false
	}
	for _, key := range []string{"revision", "createdAt", "updatedAt"} {
		delete(left, key)
		delete(right, key)
	}
	x, _ := json.Marshal(left)
	y, _ := json.Marshal(right)
	return sameJSON(x, y)
}

// Compare JSON number values exactly, including arbitrary decimal precision;
// this comparison never rewrites the source bytes included in the packet.
func sameJSON(a, b []byte) bool {
	decode := func(raw []byte) (any, error) {
		dec := json.NewDecoder(bytes.NewReader(raw))
		dec.UseNumber()
		var value any
		if err := dec.Decode(&value); err != nil {
			return nil, err
		}
		if dec.Decode(new(any)) != io.EOF {
			return nil, errors.New("trailing JSON")
		}
		return normalizeNumbers(value), nil
	}
	left, err := decode(a)
	if err != nil {
		return false
	}
	right, err := decode(b)
	return err == nil && reflect.DeepEqual(left, right)
}

func normalizeNumbers(value any) any {
	switch value := value.(type) {
	case json.Number:
		if n, ok := new(big.Rat).SetString(string(value)); ok {
			return json.Number(n.RatString())
		}
	case map[string]any:
		for k, child := range value {
			value[k] = normalizeNumbers(child)
		}
		return value
	case []any:
		for i, child := range value {
			value[i] = normalizeNumbers(child)
		}
		return value
	}
	return value
}
