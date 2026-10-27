// Package annotationsync materializes the existing annotation change feed.
// A mirror stores its documents and checkpoint in one atomically replaced file.
package annotationsync

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
)

const FileName = "mirror.json"

// Snapshot is also the API/CLI export format. Raw messages retain all adapter
// fields and numeric precision rather than round-tripping through float64.
type Snapshot struct {
	SchemaVersion int               `json:"schemaVersion"`
	Project       json.RawMessage   `json:"project"`
	Labels        []json.RawMessage `json:"labels"`
	Items         []json.RawMessage `json:"items"`
	Cursor        string            `json:"cursor"`
}

type Change struct {
	Cursor    string          `json:"cursor"`
	ProjectID string          `json:"projectId"`
	Kind      string          `json:"kind"`
	ID        string          `json:"id"`
	Operation string          `json:"operation"`
	Revision  int64           `json:"revision"`
	Actor     string          `json:"actor,omitempty"`
	Timestamp string          `json:"timestamp,omitempty"`
	Document  json.RawMessage `json:"document"`
}

type Page struct {
	Changes    []Change `json:"changes"`
	NextCursor string   `json:"nextCursor"`
	HasMore    bool     `json:"hasMore"`
}

type Fetch func(ctx context.Context, project, cursor string, limit int) (Page, error)

type Summary struct {
	Origin     string `json:"origin,omitempty"`
	ProjectID  string `json:"projectId"`
	FromCursor string `json:"fromCursor"`
	Cursor     string `json:"cursor"`
	Changes    int    `json:"changes"`
	Upserts    int    `json:"upserts"`
	Deletes    int    `json:"deletes"`
	Items      int    `json:"items"`
	Labels     int    `json:"labels"`
	Updated    bool   `json:"updated"`
	File       string `json:"file,omitempty"`
}

// Provenance records the latest observed change identity. A shared workspace
// actor is not a verified human identity or a training approval.
type Provenance struct {
	Actor     string `json:"actor,omitempty"`
	Timestamp string `json:"timestamp,omitempty"`
}

// Mirror is a validated checkpoint and its complete current documents.
type Mirror struct {
	Origin    string `json:"origin"`
	ProjectID string `json:"projectId"`
	Snapshot
	ItemProvenance map[string]Provenance `json:"itemProvenance,omitempty"`
	SHA256         string                `json:"sha256,omitempty"`
}

type state = Mirror

// Load reads and validates an existing mirror without changing files or
// contacting the server. It rejects absent files instead of creating state.
func Load(directory, origin, project string) (Mirror, error) {
	path := filepath.Join(directory, FileName)
	if _, err := os.Lstat(path); err != nil {
		return Mirror{}, err
	}
	return loadState(path, origin, project)
}

var validID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)

func emptySnapshot() Snapshot {
	return Snapshot{SchemaVersion: 1, Cursor: "0", Labels: []json.RawMessage{}, Items: []json.RawMessage{}}
}

// Sync resumes from the last successful invocation. A failed fetch or write
// before the atomic rename leaves that prior mirror intact. The lock prevents
// two CLI processes from replacing each other's checkpoints.
func Sync(ctx context.Context, fetch Fetch, origin, project, directory string, limit int) (Summary, error) {
	if origin == "" || !validID.MatchString(project) || directory == "" {
		return Summary{}, errors.New("sync requires a server URL, valid project ID, and directory")
	}
	directory, err := filepath.Abs(directory)
	if err != nil {
		return Summary{}, err
	}
	if err := os.MkdirAll(directory, 0700); err != nil {
		return Summary{}, err
	}
	unlock, err := lockDirectory(directory)
	if err != nil {
		return Summary{}, err
	}
	defer unlock()
	path := filepath.Join(directory, FileName)
	saved, err := loadState(path, origin, project)
	if err != nil {
		return Summary{}, err
	}
	withProvenance := func(ctx context.Context, project, cursor string, limit int) (Page, error) {
		page, err := fetch(ctx, project, cursor, limit)
		if err == nil {
			for _, change := range page.Changes {
				if change.Kind != "item" {
					continue
				}
				if change.Operation == "delete" {
					delete(saved.ItemProvenance, change.ID)
				} else if change.Actor != "" || change.Timestamp != "" {
					if saved.ItemProvenance == nil {
						saved.ItemProvenance = map[string]Provenance{}
					}
					saved.ItemProvenance[change.ID] = Provenance{change.Actor, change.Timestamp}
				} else {
					delete(saved.ItemProvenance, change.ID)
				}
			}
		}
		return page, err
	}
	snapshot, summary, err := Replay(ctx, withProvenance, project, saved.Snapshot, limit)
	if err != nil {
		return Summary{}, err
	}
	summary.Origin, summary.File = origin, path
	if summary.Changes == 0 && saved.SHA256 != "" {
		return summary, nil
	}
	saved.Snapshot = snapshot
	saved.SHA256, err = checksum(saved)
	if err != nil {
		return Summary{}, err
	}
	if err := atomicWrite(ctx, path, func(w io.Writer) error { return json.NewEncoder(w).Encode(saved) }); err != nil {
		return Summary{}, err
	}
	summary.Updated = true
	return summary, nil
}

// Replay returns the state at the final applied cursor, without publishing any
// partially fetched result. A zero Snapshot starts at cursor "0". Project
// filtering can leave gaps between cursors; they are ordered decimal strings,
// never floating point numbers or page offsets.
func Replay(ctx context.Context, fetch Fetch, project string, initial Snapshot, limit int) (Snapshot, Summary, error) {
	if !validID.MatchString(project) {
		return Snapshot{}, Summary{}, errors.New("invalid project ID")
	}
	if limit < 1 || limit > 500 {
		return Snapshot{}, Summary{}, errors.New("page limit must be between 1 and 500")
	}
	if initial.SchemaVersion == 0 {
		initial = emptySnapshot()
	}
	documents, err := indexSnapshot(initial, project, true)
	if err != nil {
		return Snapshot{}, Summary{}, err
	}
	summary := Summary{ProjectID: project, FromCursor: initial.Cursor, Cursor: initial.Cursor}
	last, _ := parseCursor(initial.Cursor)
	for pages := 0; ; pages++ {
		if err := ctx.Err(); err != nil {
			return Snapshot{}, Summary{}, err
		}
		if pages >= 100000 {
			return Snapshot{}, Summary{}, errors.New("sync exceeds 100000 pages; existing mirror preserved")
		}
		page, err := fetch(ctx, project, summary.Cursor, limit)
		if err != nil {
			return Snapshot{}, Summary{}, fmt.Errorf("read changes after %s: %w", summary.Cursor, err)
		}
		if page.Changes == nil {
			return Snapshot{}, Summary{}, errors.New("change page is missing its changes array")
		}
		for _, change := range page.Changes {
			cursor, err := parseCursor(change.Cursor)
			if err != nil || cursor <= last || change.ProjectID != project {
				return Snapshot{}, Summary{}, errors.New("change feed contains an invalid cursor or mismatched project")
			}
			bucket, ok := documents[change.Kind]
			if !ok {
				return Snapshot{}, Summary{}, fmt.Errorf("unsupported change kind %q", change.Kind)
			}
			base, err := documentBase(change.Document, change.Kind, project)
			if err != nil || base.ID != change.ID {
				return Snapshot{}, Summary{}, fmt.Errorf("invalid %s change document %q", change.Kind, change.ID)
			}
			previous, exists := bucket[change.ID]
			var previousBase header
			if exists {
				previousBase, _ = documentBase(previous, change.Kind, project)
			}
			switch change.Operation {
			case "create":
				if exists || change.Revision != 1 || base.Revision != 1 {
					return Snapshot{}, Summary{}, fmt.Errorf("invalid create revision for %s %q", change.Kind, change.ID)
				}
				bucket[change.ID] = change.Document
				summary.Upserts++
			case "update", "delete":
				if !exists || change.Revision != previousBase.Revision+1 {
					return Snapshot{}, Summary{}, fmt.Errorf("missing previous revision for %s %q", change.Kind, change.ID)
				}
				if change.Operation == "delete" {
					// Tombstones carry the prior document and the deletion revision.
					if change.Kind == "project" || base.Revision != previousBase.Revision {
						return Snapshot{}, Summary{}, errors.New("invalid deletion snapshot")
					}
					delete(bucket, change.ID)
					summary.Deletes++
				} else {
					if base.Revision != change.Revision {
						return Snapshot{}, Summary{}, errors.New("change and document revisions disagree")
					}
					bucket[change.ID] = change.Document
					summary.Upserts++
				}
			default:
				return Snapshot{}, Summary{}, fmt.Errorf("unsupported change operation %q", change.Operation)
			}
			last, summary.Cursor = cursor, change.Cursor
			summary.Changes++
		}
		if page.NextCursor != summary.Cursor || page.HasMore && len(page.Changes) == 0 {
			return Snapshot{}, Summary{}, errors.New("change page has an invalid or stalled checkpoint")
		}
		if !page.HasMore {
			break
		}
	}
	if len(documents["project"]) != 1 {
		return Snapshot{}, Summary{}, errors.New("project was not found in the change feed; check --project")
	}
	result := Snapshot{SchemaVersion: 1, Project: documents["project"][project], Labels: sortedDocuments(documents["label"]), Items: sortedDocuments(documents["item"]), Cursor: summary.Cursor}
	summary.Items, summary.Labels = len(result.Items), len(result.Labels)
	return result, summary, nil
}

type header struct {
	ID        string `json:"id"`
	ProjectID string `json:"projectId"`
	Revision  int64  `json:"revision"`
}

func documentBase(raw json.RawMessage, kind, project string) (header, error) {
	var base header
	if json.Unmarshal(raw, &base) != nil || !validID.MatchString(base.ID) || base.Revision < 1 || base.Revision > 9007199254740991 {
		return base, errors.New("invalid document ID or revision")
	}
	if kind == "project" && base.ID != project || kind != "project" && base.ProjectID != project {
		return base, errors.New("document belongs to a different project")
	}
	return base, nil
}

func parseCursor(value string) (int64, error) {
	n, err := strconv.ParseInt(value, 10, 64)
	if err != nil || n < 0 || strconv.FormatInt(n, 10) != value {
		return 0, errors.New("cursor must be a canonical nonnegative decimal string")
	}
	return n, nil
}

func indexSnapshot(snapshot Snapshot, project string, allowEmpty bool) (map[string]map[string]json.RawMessage, error) {
	if _, err := parseCursor(snapshot.Cursor); err != nil {
		return nil, err
	}
	if snapshot.SchemaVersion != 1 || snapshot.Labels == nil || snapshot.Items == nil {
		return nil, errors.New("invalid mirror snapshot schema")
	}
	documents := map[string]map[string]json.RawMessage{"project": {}, "label": {}, "item": {}}
	if len(snapshot.Project) == 0 && allowEmpty && snapshot.Cursor == "0" && len(snapshot.Labels) == 0 && len(snapshot.Items) == 0 {
		return documents, nil
	}
	if snapshot.Cursor == "0" {
		return nil, errors.New("nonempty mirror cannot have cursor 0")
	}
	for kind, list := range map[string][]json.RawMessage{"project": {snapshot.Project}, "label": snapshot.Labels, "item": snapshot.Items} {
		for _, doc := range list {
			base, err := documentBase(doc, kind, project)
			if err != nil {
				return nil, fmt.Errorf("invalid mirror %s: %w", kind, err)
			}
			if _, exists := documents[kind][base.ID]; exists {
				return nil, errors.New("mirror contains duplicate documents")
			}
			documents[kind][base.ID] = doc
		}
	}
	return documents, nil
}

func sortedDocuments(documents map[string]json.RawMessage) []json.RawMessage {
	ids := make([]string, 0, len(documents))
	for id := range documents {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	result := make([]json.RawMessage, 0, len(ids))
	for _, id := range ids {
		result = append(result, documents[id])
	}
	return result
}

func loadState(path, origin, project string) (state, error) {
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return state{Origin: origin, ProjectID: project, Snapshot: emptySnapshot()}, nil
	}
	if err != nil {
		return state{}, err
	}
	if !info.Mode().IsRegular() {
		return state{}, errors.New("mirror.json must be a regular file")
	}
	file, err := os.Open(path)
	if err != nil {
		return state{}, err
	}
	defer file.Close()
	var saved state
	decoder := json.NewDecoder(file)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&saved); err != nil || decoder.Decode(new(any)) != io.EOF {
		return state{}, errors.New("mirror.json is corrupt; preserve it and use a new directory to resync")
	}
	if saved.Origin != origin || saved.ProjectID != project {
		return state{}, errors.New("mirror belongs to a different server URL or project; use a separate directory")
	}
	digest, err := checksum(saved)
	if err != nil || saved.SHA256 == "" || saved.SHA256 != digest {
		return state{}, errors.New("mirror checksum mismatch; preserve it and use a new directory to resync")
	}
	if _, err := indexSnapshot(saved.Snapshot, project, false); err != nil {
		return state{}, err
	}
	return saved, nil
}

func checksum(saved state) (string, error) {
	saved.SHA256 = ""
	hash := sha256.New()
	if err := json.NewEncoder(hash).Encode(saved); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

func atomicWrite(ctx context.Context, path string, write func(io.Writer) error) error {
	directory, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer directory.Close()
	file, err := os.CreateTemp(filepath.Dir(path), ".mirror-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	defer file.Close()
	if err := write(file); err != nil {
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
	if err := directory.Sync(); err != nil {
		return fmt.Errorf("mirror and checkpoint committed together, but directory durability could not be confirmed: %w", err)
	}
	return nil
}
