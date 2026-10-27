package annotation

import (
	"context"
	"encoding/json"
	"errors"
	"path/filepath"
	"strconv"
	"sync"
	"testing"
)

func openTestStore(t *testing.T) *SQLStore {
	t.Helper()
	s, err := OpenStore(context.Background(), "sqlite", filepath.Join(t.TempDir(), "annotations.db"), true)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func raw(t *testing.T, value any) json.RawMessage {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return data
}
func saveTest(t *testing.T, s Store, kind, id string, expected int64, value any) json.RawMessage {
	t.Helper()
	data, err := s.Save(context.Background(), kind, id, expected, "test", raw(t, value))
	if err != nil {
		t.Fatalf("save %s/%s: %v", kind, id, err)
	}
	return data
}
func createProject(t *testing.T, s Store, id string) {
	t.Helper()
	saveTest(t, s, "project", id, 0, Project{Name: "Example verification project"})
}
func imageItem(project string) Item {
	return Item{ProjectID: project, Title: "Example photo", Adapter: "image", Source: Object{"url": "https://example.com/photo.jpg", "width": 1000, "height": 600}, Tags: []string{"example"}, Status: "unreviewed", Annotations: []Annotation{}}
}
func polygonAnnotation(id, label string) Annotation {
	return Annotation{ID: id, LabelID: label, Geometry: Geometry{Type: "Polygon", Coordinates: json.RawMessage(`[[[0.1,0.1],[0.8,0.1],[0.8,0.8],[0.1,0.1]]]`)}}
}

func TestPersistenceAndOptimisticRevisions(t *testing.T) {
	path := filepath.Join(t.TempDir(), "annotations.db")
	s, err := OpenStore(context.Background(), "sqlite", path, true)
	if err != nil {
		t.Fatal(err)
	}
	createProject(t, s, "project")
	item := imageItem("project")
	created := saveTest(t, s, "item", "item", 0, item)
	if err = json.Unmarshal(created, &item); err != nil {
		t.Fatal(err)
	}
	if item.Revision != 1 || item.CreatedAt == "" {
		t.Fatal("missing revision/timestamps")
	}
	item.Notes = "persist me"
	saveTest(t, s, "item", "item", 1, item)
	if _, err = s.Save(context.Background(), "item", "item", 1, "stale", raw(t, item)); !errors.Is(err, ErrConflict) {
		t.Fatalf("stale update = %v", err)
	}
	if _, err = s.Save(context.Background(), "item", "item", 0, "create", raw(t, item)); !errors.Is(err, ErrConflict) {
		t.Fatalf("duplicate create = %v", err)
	}
	if err = s.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = OpenStore(context.Background(), "sqlite", path, false)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	data, err := s.Get(context.Background(), "item", "item")
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(data, &item); err != nil {
		t.Fatal(err)
	}
	if item.Notes != "persist me" || item.Revision != 2 {
		t.Fatalf("not persisted: %+v", item)
	}
	page, err := s.Changes(context.Background(), "", "0", 500)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Changes) != 3 {
		t.Fatalf("failed writes leaked events: %d", len(page.Changes))
	}
	if err = s.Delete(context.Background(), "item", "item", 1, "test"); !errors.Is(err, ErrConflict) {
		t.Fatalf("stale delete=%v", err)
	}
	if err = s.Delete(context.Background(), "item", "item", 2, "test"); err != nil {
		t.Fatal(err)
	}
	if _, err = s.Get(context.Background(), "item", "item"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("deleted document exists: %v", err)
	}
	if _, err = s.Save(context.Background(), "item", "item", 0, "test", raw(t, item)); !errors.Is(err, ErrConflict) {
		t.Fatalf("deleted ID was reused: %v", err)
	}
}

func TestConcurrentWritersAndChangePagination(t *testing.T) {
	s := openTestStore(t)
	createProject(t, s, "project")
	createProject(t, s, "another")
	item := imageItem("project")
	saveTest(t, s, "item", "item", 0, item)
	var wg sync.WaitGroup
	results := make(chan error, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := s.Save(context.Background(), "item", "item", 1, "concurrent", raw(t, item))
			results <- err
		}()
	}
	wg.Wait()
	close(results)
	success, conflict := 0, 0
	for err := range results {
		if err == nil {
			success++
		} else if errors.Is(err, ErrConflict) {
			conflict++
		} else {
			t.Fatal(err)
		}
	}
	if success != 1 || conflict != 1 {
		t.Fatalf("success=%d conflict=%d", success, conflict)
	}
	page, err := s.Changes(context.Background(), "project", "0", 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Changes) != 1 || !page.HasMore || page.NextCursor != page.Changes[0].Cursor {
		t.Fatalf("bad first page: %+v", page)
	}
	seen := []Change{}
	cursor := "0"
	for {
		p, err := s.Changes(context.Background(), "project", cursor, 1)
		if err != nil {
			t.Fatal(err)
		}
		seen = append(seen, p.Changes...)
		cursor = p.NextCursor
		if !p.HasMore {
			break
		}
	}
	if len(seen) != 3 {
		t.Fatalf("wanted 3 filtered changes, got %d", len(seen))
	}
	last := int64(0)
	for _, change := range seen {
		n, err := strconv.ParseInt(change.Cursor, 10, 64)
		if err != nil || n <= last {
			t.Fatal("cursors are not strictly increasing")
		}
		last = n
	}
	empty, err := s.Changes(context.Background(), "project", cursor, 1)
	if err != nil || len(empty.Changes) != 0 || empty.NextCursor != cursor {
		t.Fatalf("bad end page: %+v %v", empty, err)
	}
	export, err := s.Export(context.Background(), "project")
	if err != nil {
		t.Fatal(err)
	}
	if len(export.Items) != 1 || export.Items[0].Revision != 2 || export.Cursor != cursor {
		t.Fatalf("bad snapshot: %+v", export)
	}
	item.Notes = "after export"
	saveTest(t, s, "item", "item", 2, item)
	after, err := s.Changes(context.Background(), "project", export.Cursor, 50)
	if err != nil || len(after.Changes) != 1 || after.Changes[0].Revision != 3 {
		t.Fatalf("snapshot resume: %+v %v", after, err)
	}
}

func TestAdapterForeignKeysAndNonDestructiveEditing(t *testing.T) {
	s := openTestStore(t)
	createProject(t, s, "project")
	createProject(t, s, "another")
	saveTest(t, s, "label", "label", 0, Label{ProjectID: "project", Name: "Object", Color: "#f97316"})
	saveTest(t, s, "label", "foreign", 0, Label{ProjectID: "another", Name: "Other", Color: "#000000"})
	item := imageItem("project")
	item.Annotations = []Annotation{polygonAnnotation("annotation", "label")}
	item.Crop = &Crop{X: .1, Y: .1, Width: .8, Height: .8}
	item.Status = "approved"
	saveTest(t, s, "item", "item", 0, item)
	if err := s.Delete(context.Background(), "label", "label", 1, "test"); !errors.Is(err, ErrInvalid) {
		t.Fatalf("referenced label deleted: %v", err)
	}
	invalidItem := imageItem("project")
	invalidItem.Annotations = []Annotation{polygonAnnotation("annotation", "foreign")}
	if _, err := s.Save(context.Background(), "item", "foreign_label", 0, "test", raw(t, invalidItem)); !errors.Is(err, ErrInvalid) {
		t.Fatalf("cross project label accepted: %v", err)
	}
	invalidItem.ProjectID = "missing"
	invalidItem.Annotations = nil
	if _, err := s.Save(context.Background(), "item", "missing_project", 0, "test", raw(t, invalidItem)); !errors.Is(err, ErrInvalid) {
		t.Fatalf("missing project accepted: %v", err)
	}
	item.Source["url"] = "https://example.com/replacement.jpg"
	if _, err := s.Save(context.Background(), "item", "item", 1, "test", raw(t, item)); !errors.Is(err, ErrInvalid) {
		t.Fatalf("rebound source accepted: %v", err)
	}
	item.Source["url"] = "https://example.com/photo.jpg"
	item.Crop.Width = .7
	data := saveTest(t, s, "item", "item", 1, item)
	var updated Item
	_ = json.Unmarshal(data, &updated)
	if updated.Status != "in_review" || updated.Source["url"] != item.Source["url"] || updated.Annotations[0].Geometry.Type != "Polygon" {
		t.Fatalf("crop/review state incorrect: %+v", updated)
	}
	updated.Annotations = []Annotation{}
	saveTest(t, s, "item", "item", 2, updated)
	if err := s.Delete(context.Background(), "label", "label", 1, "test"); err != nil {
		t.Fatal(err)
	}
}

func TestGeometryAndQueryValidation(t *testing.T) {
	s := openTestStore(t)
	createProject(t, s, "project")
	cases := []struct {
		name     string
		geometry Geometry
		adapter  string
	}{
		{"image out of range", Geometry{"Point", json.RawMessage(`[1.1,0.5]`)}, "image"},
		{"map out of range", Geometry{"Point", json.RawMessage(`[181,0]`)}, "map"},
		{"polygon not closed", Geometry{"Polygon", json.RawMessage(`[[[0,0],[1,0],[1,1],[0,1]]]`)}, "image"},
		{"empty polygon", Geometry{"Polygon", json.RawMessage(`[]`)}, "map"},
		{"degenerate", Geometry{"Polygon", json.RawMessage(`[[[0,0],[0.2,0.2],[0.4,0.4],[0,0]]]`)}, "image"},
		{"unsupported", Geometry{"Circle", json.RawMessage(`[0.5,0.5]`)}, "image"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			item := imageItem("project")
			item.Adapter = c.adapter
			item.Annotations = []Annotation{{ID: "annotation", Geometry: c.geometry}}
			if _, err := s.Save(context.Background(), "item", "invalid", 0, "test", raw(t, item)); !errors.Is(err, ErrInvalid) {
				t.Fatalf("invalid geometry accepted: %v", err)
			}
		})
	}
	item := imageItem("project")
	item.Title = "100% coverage"
	saveTest(t, s, "item", "percent", 0, item)
	item.Title = "Other"
	saveTest(t, s, "item", "other", 0, item)
	items, next, err := s.List(context.Background(), "item", ListOptions{ProjectID: "project", Query: "%", Limit: 10})
	if err != nil || len(items) != 1 || next != "" {
		t.Fatalf("literal search: %d %q %v", len(items), next, err)
	}
	if _, err := s.Changes(context.Background(), "project", "-1", 100); !errors.Is(err, ErrInvalid) {
		t.Fatalf("negative cursor accepted: %v", err)
	}
}

func TestRejectsNonNumericCoordinates(t *testing.T) {
	s := openTestStore(t)
	createProject(t, s, "project")
	geometries := []Geometry{
		{"Point", json.RawMessage(`[null,null]`)},
		{"Point", json.RawMessage(`["0",0]`)},
		{"Point", json.RawMessage(`[false,0]`)},
		{"Point", json.RawMessage(`[{},0]`)},
		{"LineString", json.RawMessage(`[[null,0],[1,1]]`)},
		{"Polygon", json.RawMessage(`[[[null,0],[1,0],[1,1],[null,0]]]`)},
		{"Polygon", json.RawMessage(`[[[0,0],[1,0],[1,1],[0,0]],null]`)},
	}
	for _, adapter := range []string{"map", "image"} {
		for _, geometry := range geometries {
			t.Run(adapter+"/"+geometry.Type+"/"+string(geometry.Coordinates), func(t *testing.T) {
				item := imageItem("project")
				item.Adapter = adapter
				item.Annotations = []Annotation{{ID: "annotation", Geometry: geometry}}
				if _, err := s.Save(context.Background(), "item", "invalid", 0, "test", raw(t, item)); !errors.Is(err, ErrInvalid) {
					t.Fatalf("non-numeric coordinates were accepted: %v", err)
				}
			})
		}
	}
	for _, center := range []any{nil, []any{nil, nil}, []any{nil, 0}, []any{"0", 0}, []any{false, 0}, []any{Object{}, 0}} {
		item := imageItem("project")
		item.Adapter = "map"
		item.Source = Object{"center": center}
		if _, err := s.Save(context.Background(), "item", "invalid_center", 0, "test", raw(t, item)); !errors.Is(err, ErrInvalid) {
			t.Fatalf("non-numeric map center %#v was accepted: %v", center, err)
		}
	}
	item := imageItem("project")
	item.Adapter = "map"
	item.Source = Object{"center": []int{0, 0}}
	item.Annotations = []Annotation{{ID: "origin", Geometry: Geometry{"Point", json.RawMessage(`[0,0]`)}}}
	saveTest(t, s, "item", "valid_origin", 0, item)
}

func TestListFiltersByTagCaseInsensitivelyAndPagesWithinTheTag(t *testing.T) {
	s := openTestStore(t)
	createProject(t, s, "project")
	for i := 0; i < 5; i++ {
		item := imageItem("project")
		item.Tags = []string{"review"}
		if i%2 == 0 {
			item.Tags = append(item.Tags, "Splits-2026")
		}
		saveTest(t, s, "item", "item-"+strconv.Itoa(i), 0, item)
	}
	first, next, err := s.List(context.Background(), "item", ListOptions{ProjectID: "project", Tag: "splits-2026", Limit: 2})
	if err != nil || len(first) != 2 || next != "item-2" {
		t.Fatalf("first tagged page: %v %d %q", err, len(first), next)
	}
	rest, next, err := s.List(context.Background(), "item", ListOptions{ProjectID: "project", Tag: "SPLITS-2026", Cursor: next, Limit: 2})
	if err != nil || len(rest) != 1 || next != "" {
		t.Fatalf("second tagged page: %v %d %q", err, len(rest), next)
	}
	none, _, err := s.List(context.Background(), "item", ListOptions{ProjectID: "project", Tag: "absent"})
	if err != nil || len(none) != 0 {
		t.Fatalf("absent tag: %v %d", err, len(none))
	}
}
