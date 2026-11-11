package annotationsync

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const testOrigin = "https://fieldwork.example"

func testChange(cursor, kind, id, operation string, revision int64) Change {
	docRevision := revision
	if operation == "delete" {
		docRevision--
	}
	doc, _ := json.Marshal(map[string]any{"id": id, "projectId": "project", "revision": docRevision, "name": id})
	return Change{Cursor: cursor, ProjectID: "project", Kind: kind, ID: id, Operation: operation, Revision: revision, Document: doc}
}

func initialFetch(_ context.Context, _, cursor string, _ int) (Page, error) {
	if cursor == "0" {
		return Page{Changes: []Change{testChange("1", "project", "project", "create", 1), testChange("2", "label", "label", "create", 1)}, NextCursor: "2", HasMore: true}, nil
	}
	if cursor == "2" {
		// Preserve cursors beyond the exact range of JavaScript numbers and
		// allow gaps caused by changes in other projects.
		return Page{Changes: []Change{testChange("9007199254740993", "item", "item", "create", 1)}, NextCursor: "9007199254740993"}, nil
	}
	return Page{Changes: []Change{}, NextCursor: cursor}, nil
}

func TestSyncResumeDeleteAndNoOp(t *testing.T) {
	dir := t.TempDir()
	ctx := context.Background()
	first, err := Sync(ctx, initialFetch, testOrigin, "project", dir, 2)
	if err != nil || !first.Updated || first.FromCursor != "0" || first.Cursor != "9007199254740993" || first.Items != 1 || first.Labels != 1 || first.Changes != 3 {
		t.Fatalf("initial sync: %+v, %v", first, err)
	}
	before, _ := os.ReadFile(first.File)
	noOp, err := Sync(ctx, initialFetch, testOrigin, "project", dir, 2)
	if err != nil || noOp.Updated || noOp.Changes != 0 || noOp.FromCursor != first.Cursor || noOp.Cursor != first.Cursor {
		t.Fatalf("no-op sync: %+v, %v", noOp, err)
	}
	after, _ := os.ReadFile(first.File)
	if !bytes.Equal(before, after) {
		t.Fatal("no-op rewrote the mirror")
	}
	fetch := func(_ context.Context, project, cursor string, _ int) (Page, error) {
		if project != "project" || cursor != first.Cursor {
			t.Fatalf("did not resume bound checkpoint: %s/%s", project, cursor)
		}
		return Page{Changes: []Change{
			testChange("9007199254740994", "project", "project", "update", 2),
			testChange("9007199254740995", "item", "item", "delete", 2),
			testChange("9007199254740996", "label", "label", "delete", 2),
		}, NextCursor: "9007199254740996"}, nil
	}
	updated, err := Sync(ctx, fetch, testOrigin, "project", dir, 3)
	if err != nil || updated.Deletes != 2 || updated.Upserts != 1 || updated.Items != 0 || updated.Labels != 0 {
		t.Fatalf("incremental sync: %+v, %v", updated, err)
	}
	saved, err := loadState(first.File, testOrigin, "project")
	if err != nil || saved.Cursor != updated.Cursor || len(saved.Items) != 0 || len(saved.Labels) != 0 {
		t.Fatalf("inconsistent persisted checkpoint: %+v, %v", saved, err)
	}
}

func TestMirrorLoadAndLatestChangeProvenance(t *testing.T) {
	dir := t.TempDir()
	if _, err := Load(dir, testOrigin, "project"); err == nil {
		t.Fatal("missing mirror loaded as a completed checkpoint")
	}
	fetch := func(ctx context.Context, project, cursor string, limit int) (Page, error) {
		page, err := initialFetch(ctx, project, cursor, limit)
		for i := range page.Changes {
			if page.Changes[i].Kind == "item" {
				page.Changes[i].Actor = "workspace"
				page.Changes[i].Timestamp = "2026-09-27T06:00:00Z"
			}
		}
		return page, err
	}
	if _, err := Sync(context.Background(), fetch, testOrigin, "project", dir, 2); err != nil {
		t.Fatal(err)
	}
	saved, err := Load(dir, testOrigin, "project")
	if err != nil || saved.ItemProvenance["item"] != (Provenance{"workspace", "2026-09-27T06:00:00Z"}) {
		t.Fatalf("lost provenance: %+v, %v", saved.ItemProvenance, err)
	}
	before, _ := os.ReadFile(filepath.Join(dir, FileName))
	if _, err := Sync(context.Background(), fetch, testOrigin, "project", dir, 2); err != nil {
		t.Fatal(err)
	}
	after, _ := os.ReadFile(filepath.Join(dir, FileName))
	if !bytes.Equal(before, after) {
		t.Fatal("no-op pull changed provenance or checksum")
	}
	if _, err := Load(dir, "https://other.example", "project"); err == nil {
		t.Fatal("accepted wrong mirror origin")
	}
	altered := bytes.Replace(before, []byte("workspace"), []byte("claimed-human"), 1)
	if err := os.WriteFile(filepath.Join(dir, FileName), altered, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(dir, testOrigin, "project"); err == nil {
		t.Fatal("accepted tampered provenance checksum")
	}
}

func TestFailedPageAndWritePreserveCheckpoint(t *testing.T) {
	dir := t.TempDir()
	ctx := context.Background()
	first, err := Sync(ctx, initialFetch, testOrigin, "project", dir, 2)
	if err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(first.File)
	failedFetch := func(_ context.Context, _, cursor string, _ int) (Page, error) {
		if cursor == first.Cursor {
			return Page{Changes: []Change{testChange("9007199254740994", "item", "item", "update", 2)}, NextCursor: "9007199254740994", HasMore: true}, nil
		}
		return Page{}, errors.New("connection interrupted")
	}
	if _, err := Sync(ctx, failedFetch, testOrigin, "project", dir, 2); err == nil {
		t.Fatal("failed page reported success")
	}
	after, _ := os.ReadFile(first.File)
	if !bytes.Equal(before, after) {
		t.Fatal("failed fetch advanced persisted documents or checkpoint")
	}
	// Simulate a write failing after some bytes reached the temporary file.
	err = atomicWrite(ctx, first.File, func(w io.Writer) error {
		_, _ = io.WriteString(w, `{"cursor":"9007199254740994",`)
		return io.ErrShortWrite
	})
	if !errors.Is(err, io.ErrShortWrite) {
		t.Fatalf("expected write failure, got %v", err)
	}
	after, _ = os.ReadFile(first.File)
	if !bytes.Equal(before, after) {
		t.Fatal("failed write advanced persisted documents or checkpoint")
	}
	matches, _ := filepath.Glob(filepath.Join(dir, ".mirror-*.tmp"))
	if len(matches) != 0 {
		t.Fatal("failed write left a temporary file")
	}
	retry, err := Sync(ctx, initialFetch, testOrigin, "project", dir, 2)
	if err != nil || retry.FromCursor != first.Cursor || retry.Updated {
		t.Fatalf("failed sync did not resume previous checkpoint: %+v, %v", retry, err)
	}
}

func TestSyncRejectsWrongBindingCorruptionAndConcurrentWriter(t *testing.T) {
	dir := t.TempDir()
	ctx := context.Background()
	first, err := Sync(ctx, initialFetch, testOrigin, "project", dir, 2)
	if err != nil {
		t.Fatal(err)
	}
	neverFetch := func(context.Context, string, string, int) (Page, error) {
		t.Fatal("invalid mirror made a network request")
		return Page{}, nil
	}
	for _, binding := range [][2]string{{"https://other.example", "project"}, {testOrigin + "/other-workspace", "project"}, {testOrigin, "different-project"}} {
		if _, err := Sync(ctx, neverFetch, binding[0], binding[1], dir, 2); err == nil || !strings.Contains(err.Error(), "different server URL or project") {
			t.Fatalf("accepted different binding %v: %v", binding, err)
		}
	}
	unlock, err := lockDirectory(dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Sync(ctx, neverFetch, testOrigin, "project", dir, 2); err == nil {
		t.Fatal("accepted a concurrent mirror writer")
	}
	unlock()
	data, _ := os.ReadFile(first.File)
	data = bytes.Replace(data, []byte(`"cursor":"9007199254740993"`), []byte(`"cursor":"9007199254740994"`), 1)
	if err := os.WriteFile(first.File, data, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := Sync(ctx, neverFetch, testOrigin, "project", dir, 2); err == nil || !strings.Contains(err.Error(), "checksum") {
		t.Fatalf("accepted corrupt checkpoint: %v", err)
	}
}

func TestReplayRejectsStalledOrMismatchedFeed(t *testing.T) {
	for name, page := range map[string]Page{
		"stalled":            {Changes: []Change{}, NextCursor: "0", HasMore: true},
		"wrong project":      {Changes: []Change{{Cursor: "1", ProjectID: "other", Kind: "project", ID: "other", Operation: "create", Revision: 1}}, NextCursor: "1"},
		"skipped checkpoint": {Changes: []Change{testChange("1", "project", "project", "create", 1)}, NextCursor: "2"},
	} {
		t.Run(name, func(t *testing.T) {
			fetch := func(context.Context, string, string, int) (Page, error) { return page, nil }
			if _, _, err := Replay(context.Background(), fetch, "project", Snapshot{}, 100); err == nil {
				t.Fatal("invalid feed accepted")
			}
		})
	}
}
