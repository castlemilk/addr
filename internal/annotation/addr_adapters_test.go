package annotation

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func sceneTestItem() Item {
	hash := strings.Repeat("a", 64)
	ref := Object{"url": "/api/v1/assets/" + hash, "sha256": hash}
	return Item{
		ProjectID: "project", Title: "Isolated scene", Adapter: "addr-scene", Status: "in_review",
		Source: Object{"sceneId": "source-scene", "image": ref, "model": ref, "width": 100, "height": 100},
		Annotations: []Annotation{{ID: "object", Geometry: Geometry{Type: "Polygon", Coordinates: json.RawMessage(`[[[0,0],[1,0],[1,1],[0,1],[0,0]],[[0.2,0.2],[0.3,0.2],[0.3,0.3],[0.2,0.2]]]`)}, Properties: Object{
			"custom": "preserved", "addr": Object{"objectId": "object", "layer": "review", "status": "confirmed", "source": "sam3", "overrides": Object{
				"notes": "Old object note", "geometry_reviewed": true, "model": Object{"height_verified": true},
				"roof_parts": []any{Object{"ring_meters": []any{}}}, "roof_lines": []any{}, "roof_annotation": Object{"schema_version": 1}, "roof_geometry_source": "image-guided-prior",
				"roof_line_edits": Object{"outline": Object{"ring": []any{1, 2}}, "checked": true},
			}},
		}}},
	}
}

func sceneTestService(t *testing.T) (*Service, Item) {
	t.Helper()
	store := openTestStore(t)
	createProject(t, store, "project")
	var item Item
	if err := json.Unmarshal(saveTest(t, store, "item", "scene", 0, sceneTestItem()), &item); err != nil {
		t.Fatal(err)
	}
	return &Service{Store: store}, item
}

func saveSceneMark(t *testing.T, service *Service, item Item, mark Annotation) Item {
	t.Helper()
	result, err := service.Call(context.Background(), "SaveAnnotation", Object{"itemId": item.ID, "expectedRevision": item.Revision, "annotation": mark})
	if err != nil {
		t.Fatal(err)
	}
	var saved Item
	if err := decodeObject(result["item"].(map[string]any), &saved); err != nil {
		t.Fatal(err)
	}
	return saved
}

func TestAddrSceneNotesContractPreservesRoofsAndEmptyClear(t *testing.T) {
	service, item := sceneTestService(t)
	before := raw(t, item.Annotations[0].Properties)
	mark := item.Annotations[0]
	sceneOverrides(mark)["notes"] = "Saved through the generic API"
	// Real clients spell numbers differently and reorder Geometry object keys.
	mark.Geometry.Coordinates = json.RawMessage(`[[[0.0,0.0],[1.0,0.0],[1.0,1.0],[0.0,1.0],[0.0,0.0]],[[0.2,0.2],[0.3,0.2],[0.3,0.3],[0.2,0.2]]]`)
	item = saveSceneMark(t, service, item, mark)
	if got := sceneOverrides(item.Annotations[0])["notes"]; got != "Saved through the generic API" {
		t.Fatalf("canonical notes not saved: %v", got)
	}
	if sceneOverrides(item.Annotations[0])["geometry_reviewed"] != true || sceneOverrides(item.Annotations[0])["model"].(map[string]any)["height_verified"] != true {
		t.Fatal("notes-only update invalidated approved geometry")
	}
	var originalProperties Object
	_ = json.Unmarshal(before, &originalProperties)
	actual := item.Annotations[0].Properties
	originalProperties["addr"].(map[string]any)["overrides"].(map[string]any)["notes"] = "Saved through the generic API"
	if !sameJSON(actual, originalProperties) {
		t.Fatalf("notes-only update changed source or roof fields: %s", raw(t, actual))
	}
	mark = item.Annotations[0]
	sceneOverrides(mark)["notes"] = ""
	item = saveSceneMark(t, service, item, mark)
	if note, exists := sceneOverrides(item.Annotations[0])["notes"]; !exists || note != "" {
		t.Fatal("explicit empty object note was lost")
	}
	mark.Notes = "This field is not displayed by the scene editor"
	_, err := service.Call(context.Background(), "SaveAnnotation", Object{"itemId": item.ID, "expectedRevision": item.Revision, "annotation": mark})
	if !errors.Is(err, ErrInvalid) || !strings.Contains(err.Error(), "properties.addr.overrides.notes") {
		t.Fatalf("unsupported notes lack actionable rejection: %v", err)
	}
}

func TestAddrSceneRejectsInvisibleOrDestructiveMutations(t *testing.T) {
	service, saved := sceneTestService(t)
	cases := []struct {
		name, hint string
		edit       func(*Item)
	}{
		{"omission", "cannot be removed", func(item *Item) { item.Annotations = nil }},
		{"label", "overrides.kind", func(item *Item) { item.Annotations[0].LabelID = "different-label" }},
		{"binding", "objectId", func(item *Item) { sceneAddr(item.Annotations[0])["objectId"] = "different-object" }},
		{"source layer", "immutable source binding", func(item *Item) { sceneAddr(item.Annotations[0])["layer"] = "model" }},
		{"point", "requires Polygon", func(item *Item) {
			item.Annotations[0].Geometry = Geometry{Type: "Point", Coordinates: json.RawMessage(`[0.5,0.5]`)}
		}},
		{"crop", "does not support crop", func(item *Item) { item.Crop = &Crop{Width: 0.5, Height: 0.5} }},
		{"class", "overrides.kind", func(item *Item) { sceneOverrides(item.Annotations[0])["kind"] = "unknown-class" }},
		{"review", "overrides.status", func(item *Item) { sceneOverrides(item.Annotations[0])["status"] = "approved" }},
		{"required tombstone", "cannot remove", func(item *Item) { sceneAddr(item.Annotations[0])["removedFields"] = []any{"model"} }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var item Item
			_ = json.Unmarshal(raw(t, saved), &item)
			tc.edit(&item)
			_, err := service.Call(context.Background(), "SaveItem", Object{"item": item, "expectedRevision": saved.Revision})
			if !errors.Is(err, ErrInvalid) || !strings.Contains(err.Error(), tc.hint) {
				t.Fatalf("mutation rejection = %v; want %q", err, tc.hint)
			}
		})
	}
	_, err := service.Call(context.Background(), "DeleteAnnotation", Object{"itemId": saved.ID, "id": "object", "expectedRevision": saved.Revision})
	if !errors.Is(err, ErrInvalid) || !strings.Contains(err.Error(), "overrides.status to rejected") {
		t.Fatalf("DeleteAnnotation must expose rejection workflow: %v", err)
	}
	sceneOverrides(saved.Annotations[0])["status"] = "rejected"
	result := saveSceneMark(t, service, saved, saved.Annotations[0])
	if sceneOverrides(result.Annotations[0])["status"] != "rejected" || len(result.Annotations) != 1 {
		t.Fatal("rejection must preserve the object and its source binding")
	}
}

func TestAddrSceneGeometryAndClassInvalidateDerivedState(t *testing.T) {
	for _, change := range []string{"geometry", "class"} {
		t.Run(change, func(t *testing.T) {
			service, item := sceneTestService(t)
			mark := item.Annotations[0]
			oldFeedback := raw(t, sceneOverrides(mark)["roof_line_edits"])
			if change == "geometry" {
				mark.Geometry.Coordinates = json.RawMessage(`[[[0.01,0],[1,0],[1,1],[0,1],[0.01,0]]]`)
			} else {
				sceneOverrides(mark)["kind"] = "tree"
			}
			item = saveSceneMark(t, service, item, mark)
			overrides := sceneOverrides(item.Annotations[0])
			if overrides["geometry_reviewed"] != false || overrides["model"].(map[string]any)["height_verified"] != false || overrides["status"] != "needs_adjustment" {
				t.Fatalf("stale approvals remain: %s", raw(t, overrides))
			}
			if overrides["roof_annotation"] != nil || overrides["roof_parts"] != nil || overrides["roof_lines"] != nil {
				t.Fatal("derived roof geometry survived an outline/class change")
			}
			if change == "geometry" && string(raw(t, overrides["roof_line_edits"])) != string(oldFeedback) {
				t.Fatal("outline-bound feedback was rebound or deleted")
			}
			removed := raw(t, sceneAddr(item.Annotations[0])["removedFields"])
			if !strings.Contains(string(removed), "roof_parts") || change == "class" && !strings.Contains(string(removed), "roof_line_edits") {
				t.Fatal("baseline roof fields would reappear without tombstones")
			}
			sceneOverrides(item.Annotations[0])["notes"] = "Later note"
			later := saveSceneMark(t, service, item, item.Annotations[0])
			if string(raw(t, sceneAddr(later.Annotations[0])["removedFields"])) != string(removed) {
				t.Fatal("later notes resurrected source roofs")
			}
			if !sameJSON(item.Source, later.Source) {
				t.Fatal("source references changed")
			}
		})
	}
}

func TestAddrSceneWriteHintsDiscoveredThroughGetInfo(t *testing.T) {
	service := &Service{Store: openTestStore(t)}
	info, err := service.Call(context.Background(), "GetInfo", Object{})
	if err != nil {
		t.Fatal(err)
	}
	for _, value := range info["adapters"].([]any) {
		adapter := value.(map[string]any)
		if adapter["id"] != "addr-scene" {
			continue
		}
		if !sameJSON(adapter["geometryTypes"], []string{"Polygon"}) || adapter["supportsCrop"] != false {
			t.Fatal("scene capabilities advertise unsupported mutations")
		}
		hints := string(raw(t, adapter["writeHints"]))
		for _, required := range []string{"overrides.notes", "empty string", "overrides.kind", "overrides.status", "rejected", "expectedRevision"} {
			if !strings.Contains(hints, required) {
				t.Fatalf("missing discovery hint %q", required)
			}
		}
		return
	}
	t.Fatal("addr-scene adapter not discovered")
}
