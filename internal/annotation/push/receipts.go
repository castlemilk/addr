// Package push publishes changed local scene frames and review state to
// Fieldwork as new immutable addr-scene items. It never edits Fieldwork
// overrides, never deletes, and treats every pushed value as a model estimate
// plus local review state as of publication, not as a human label.
package push

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/benebsworth/addr/internal/annotation/migration"
)

const ReceiptSchemaVersion = 1

var idPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)
var digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

// Generation identifies what one pushed item carries.
type Generation struct {
	ModelSignature      string  `json:"modelSignature"`
	ReviewRevision      int     `json:"reviewRevision"`
	ReviewHeadSHA256    string  `json:"reviewHeadSha256,omitempty"`
	ReviewHistorySHA256 string  `json:"reviewHistorySha256,omitempty"`
	ImageSHA256         string  `json:"imageSha256"`
	Width               float64 `json:"width"`
	Height              float64 `json:"height"`
}

// Supersedes names an older live item of the same scene at the revision the
// plan observed it.
type Supersedes struct {
	ItemID         string `json:"itemId"`
	Revision       int64  `json:"revision"`
	ModelSignature string `json:"modelSignature,omitempty"`
	ImageSHA256    string `json:"imageSha256,omitempty"`
}

// Mark records one successful supersede mark written by our token.
type Mark struct {
	RunID       string          `json:"runId"`
	SupersedeBy string          `json:"supersededBy"`
	Revision    int64           `json:"revision"`
	MarkedAt    string          `json:"markedAt"`
	Document    json.RawMessage `json:"document"`
}

// ItemReceipt is the durable record of our own writes to one item. Document is
// the server's returned document from the latest own write; Baseline is the
// pristine created document (nil for frozen ids, whose baseline is the frozen plan).
type ItemReceipt struct {
	SchemaVersion int             `json:"schemaVersion"`
	ItemID        string          `json:"itemId"`
	SceneID       string          `json:"sceneId"`
	RunID         string          `json:"runId"`
	PushedAt      string          `json:"pushedAt"`
	Revision      int64           `json:"revision"`
	Document      json.RawMessage `json:"document"`
	Baseline      json.RawMessage `json:"baseline,omitempty"`
	Generation    Generation      `json:"generation"`
	Supersedes    []Supersedes    `json:"supersedes"`
	Marks         []Mark          `json:"marks"`
}

// SceneReceipt lists every generation item we created for a scene. Generation
// describes LatestItemID; the training publisher reads
// generation.modelSignature / generation.reviewRevision from this file.
type SceneReceipt struct {
	SchemaVersion int        `json:"schemaVersion"`
	SceneID       string     `json:"sceneId"`
	LatestItemID  string     `json:"latestItemId"`
	ItemIDs       []string   `json:"itemIds"`
	UpdatedAt     string     `json:"updatedAt"`
	Generation    Generation `json:"generation"`
}

// ReceiptIndex is safe for concurrent use: Apply writes receipts from several
// scene workers while others consult OwnWrite. Read the maps directly only when
// no writer can be active (tests, status output); prefer Item/Scene otherwise.
type ReceiptIndex struct {
	Dir    string
	Items  map[string]ItemReceipt
	Scenes map[string]SceneReceipt
	mu     sync.RWMutex
}

// LoadReceipts reads data/fieldwork-sync/<P>/push. A missing directory is an
// empty index; corrupt receipts are an error so nothing is silently re-pushed.
func LoadReceipts(dir string) (*ReceiptIndex, error) {
	r := &ReceiptIndex{Dir: dir, Items: map[string]ItemReceipt{}, Scenes: map[string]SceneReceipt{}}
	for _, kind := range []string{"items", "scenes"} {
		entries, err := os.ReadDir(filepath.Join(dir, kind))
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return nil, err
		}
		for _, entry := range entries {
			if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") || strings.HasPrefix(entry.Name(), ".") {
				continue
			}
			data, err := os.ReadFile(filepath.Join(dir, kind, entry.Name()))
			if err != nil {
				return nil, err
			}
			id := strings.TrimSuffix(entry.Name(), ".json")
			if kind == "items" {
				var rec ItemReceipt
				if json.Unmarshal(data, &rec) != nil || rec.SchemaVersion != ReceiptSchemaVersion || rec.ItemID != id || !idPattern.MatchString(id) {
					return nil, fmt.Errorf("corrupt push receipt %s; preserve it and inspect before pushing again", entry.Name())
				}
				r.Items[id] = rec
			} else {
				var rec SceneReceipt
				if json.Unmarshal(data, &rec) != nil || rec.SchemaVersion != ReceiptSchemaVersion || rec.SceneID != id {
					return nil, fmt.Errorf("corrupt push scene receipt %s; preserve it and inspect before pushing again", entry.Name())
				}
				r.Scenes[id] = rec
			}
		}
	}
	return r, nil
}

func writeAtomic(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".receipt-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if _, err := file.Write(data); err != nil {
		file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	if err := os.Rename(file.Name(), path); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

// Item returns the receipt of one item id, if any.
func (r *ReceiptIndex) Item(itemID string) (ItemReceipt, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	rec, ok := r.Items[itemID]
	return rec, ok
}

// Scene returns the scene receipt of one scene id, if any.
func (r *ReceiptIndex) Scene(sceneID string) (SceneReceipt, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	rec, ok := r.Scenes[sceneID]
	return rec, ok
}

// SceneIDs lists the scenes with a receipt, sorted.
func (r *ReceiptIndex) SceneIDs() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	ids := make([]string, 0, len(r.Scenes))
	for id := range r.Scenes {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// ItemIDs lists the items with a receipt, sorted.
func (r *ReceiptIndex) ItemIDs() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	ids := make([]string, 0, len(r.Items))
	for id := range r.Items {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// Len reports how many item and scene receipts the index holds.
func (r *ReceiptIndex) Len() (items, scenes int) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.Items), len(r.Scenes)
}

func (r *ReceiptIndex) WriteItem(rec ItemReceipt) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.writeItemLocked(rec)
}

func (r *ReceiptIndex) writeItemLocked(rec ItemReceipt) error {
	if !idPattern.MatchString(rec.ItemID) {
		return fmt.Errorf("invalid receipt item id")
	}
	rec.SchemaVersion = ReceiptSchemaVersion
	if rec.Supersedes == nil {
		rec.Supersedes = []Supersedes{}
	}
	if rec.Marks == nil {
		rec.Marks = []Mark{}
	}
	data, err := json.MarshalIndent(rec, "", "  ")
	if err != nil {
		return err
	}
	if err := writeAtomic(filepath.Join(r.Dir, "items", rec.ItemID+".json"), data); err != nil {
		return err
	}
	r.Items[rec.ItemID] = rec
	return nil
}

func (r *ReceiptIndex) WriteScene(rec SceneReceipt) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.writeSceneLocked(rec)
}

func (r *ReceiptIndex) writeSceneLocked(rec SceneReceipt) error {
	if !idPattern.MatchString(rec.SceneID) {
		return fmt.Errorf("invalid receipt scene id")
	}
	rec.SchemaVersion = ReceiptSchemaVersion
	sort.Strings(rec.ItemIDs)
	data, err := json.MarshalIndent(rec, "", "  ")
	if err != nil {
		return err
	}
	if err := writeAtomic(filepath.Join(r.Dir, "scenes", rec.SceneID+".json"), data); err != nil {
		return err
	}
	r.Scenes[rec.SceneID] = rec
	return nil
}

// RecordCreated stores a new generation item and links it to its scene; the
// scene receipt's Generation describes the new latest item.
func (r *ReceiptIndex) RecordCreated(rec ItemReceipt) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.writeItemLocked(rec); err != nil {
		return err
	}
	scene := r.Scenes[rec.SceneID]
	scene.SceneID = rec.SceneID
	found := false
	for _, id := range scene.ItemIDs {
		found = found || id == rec.ItemID
	}
	if !found {
		scene.ItemIDs = append(scene.ItemIDs, rec.ItemID)
	}
	scene.LatestItemID = rec.ItemID
	scene.UpdatedAt = rec.PushedAt
	scene.Generation = rec.Generation
	return r.writeSceneLocked(scene)
}

// RecordMark appends a supersede mark; frozen ids get a receipt with nil Baseline.
func (r *ReceiptIndex) RecordMark(itemID, sceneID string, mark Mark) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	rec, ok := r.Items[itemID]
	if !ok {
		rec = ItemReceipt{ItemID: itemID, SceneID: sceneID, RunID: mark.RunID, PushedAt: mark.MarkedAt}
	}
	rec.Marks = append(rec.Marks, mark)
	rec.Revision = mark.Revision
	rec.Document = mark.Document
	return r.writeItemLocked(rec)
}

// OwnWrite reports whether the mirror document at this revision is exactly
// what our token last wrote (revision equal and same document ignoring timestamps).
func (r *ReceiptIndex) OwnWrite(itemID string, revision int64, raw json.RawMessage) bool {
	rec, ok := r.Item(itemID)
	if !ok || rec.Revision != revision || len(rec.Document) == 0 {
		return false
	}
	return SameDocument(rec.Document, raw)
}

// ExpectedRevision is the revision our receipts say an item should be at, or
// 1 for an unmarked frozen id (frozen = true) and 0 when unknown.
func (r *ReceiptIndex) ExpectedRevision(itemID string, frozen bool) (int64, bool) {
	if rec, ok := r.Item(itemID); ok {
		return rec.Revision, true
	}
	if frozen {
		return 1, true
	}
	return 0, false
}

// StoreEvidence keeps the exact JSON bytes we uploaded under push/assets/<sha>.
func (r *ReceiptIndex) StoreEvidence(data []byte) (string, error) {
	sha := migration.Digest(data)
	path := filepath.Join(r.Dir, "assets", sha)
	if _, err := os.Stat(path); err == nil {
		return sha, nil
	}
	return sha, writeAtomic(path, data)
}

// Evidence reads and verifies uploaded JSON bytes by hash.
func (r *ReceiptIndex) Evidence(sha string) ([]byte, error) {
	if !digestPattern.MatchString(sha) {
		return nil, fmt.Errorf("invalid evidence digest")
	}
	data, err := os.ReadFile(filepath.Join(r.Dir, "assets", sha))
	if err != nil {
		return nil, err
	}
	if migration.Digest(data) != sha {
		return nil, fmt.Errorf("push evidence %s failed its hash check", sha)
	}
	return data, nil
}

// SameDocument compares two item documents ignoring revision and timestamps.
func SameDocument(a, b json.RawMessage) bool {
	return sameJSON(comparable(a), comparable(b))
}

func comparable(raw json.RawMessage) map[string]json.RawMessage {
	var doc map[string]json.RawMessage
	if json.Unmarshal(raw, &doc) != nil {
		return nil
	}
	for _, key := range []string{"revision", "createdAt", "updatedAt"} {
		delete(doc, key)
	}
	return doc
}

func sameJSON(a, b any) bool {
	x, errX := json.Marshal(a)
	y, errY := json.Marshal(b)
	if errX != nil || errY != nil || a == nil || b == nil {
		return false
	}
	var left, right any
	if json.Unmarshal(x, &left) != nil || json.Unmarshal(y, &right) != nil {
		return false
	}
	lx, _ := json.Marshal(left)
	rx, _ := json.Marshal(right)
	return string(lx) == string(rx)
}

// NewRunID is UTC yyyymmddTHHMMSSZ plus eight random hex characters.
func NewRunID() string {
	var random [4]byte
	_, _ = rand.Read(random[:])
	return time.Now().UTC().Format("20060102T150405Z") + "-" + hex.EncodeToString(random[:])
}

var runIDPattern = regexp.MustCompile(`^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$`)

func ValidRunID(id string) bool { return runIDPattern.MatchString(id) }
