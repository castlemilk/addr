package annotation

import "encoding/json"

type addrSceneAdapter struct{}
type addrReportAdapter struct{}

func (addrSceneAdapter) Info() AdapterInfo {
	return AdapterInfo{
		ID: "addr-scene", Name: "Address scene", CoordinateSystem: "Normalized original image (0..1); raw pixel/metre/Z state retained", GeometryTypes: []string{"Polygon"},
		WriteHints: []string{
			"Read the current item, preserve the complete annotation, and save with the parent item's expectedRevision. annotation.id must equal properties.addr.objectId; source references and source binding fields are immutable.",
			"Object notes use properties.addr.overrides.notes; an empty string clears them. Nonempty top-level annotation.notes is rejected. Item.notes remains scene-level feedback.",
			"Object classification uses properties.addr.overrides.kind; its display name uses overrides.label. Existing annotation.labelId is an immutable source-class label, not the effective edited class.",
			"Object review uses properties.addr.overrides.status: candidate, confirmed, needs_adjustment, rejected, or superseded. Deleting or omitting an existing scene annotation is rejected; set overrides.status to rejected instead.",
			"Geometry is a normalized Polygon including closed outer and hole rings. Points, lines, and crop are unsupported. annotation.geometry is authoritative; do not edit overrides.ring or overrides.holes.",
			"Changing geometry or classification clears geometry_reviewed, height_verified, and derived roofs, and marks the object needs_adjustment unless rejected or superseded. Old outline-bound roof feedback is preserved without rebinding. Review again in a later revision.",
			"New objects need explicit matching annotation/object IDs and complete scene overrides including kind, label, source, status, geometry_reviewed, and model. Source predictions/history and training eligibility are never changed by Fieldwork feedback.",
		},
	}
}
func (addrReportAdapter) Info() AdapterInfo {
	return AdapterInfo{ID: "addr-report", Name: "Dataset report", CoordinateSystem: "WGS84 report views with immutable source references", GeometryTypes: []string{}}
}

func validateAssetRef(raw any) error {
	ref, ok := raw.(map[string]any)
	if !ok {
		return invalid("asset reference must be an object")
	}
	hash, ok := ref["sha256"].(string)
	if !ok || !assetDigest.MatchString(hash) {
		return invalid("asset reference requires a SHA-256 digest")
	}
	if ref["url"] != "/api/v1/assets/"+hash {
		return invalid("asset URL must use the protected content-addressed route")
	}
	return nil
}

func (addrSceneAdapter) Validate(item *Item) error {
	if item.Crop != nil {
		return invalid("addr-scene does not support crop; preserve the source frame or create an image item")
	}
	if id, ok := item.Source["sceneId"].(string); !ok || checkID(id) != nil {
		return invalid("addr-scene requires a valid source.sceneId")
	}
	for _, key := range []string{"image", "model"} {
		if err := validateAssetRef(item.Source[key]); err != nil {
			return invalid("source.%s: %v", key, err)
		}
	}
	for _, key := range []string{"review", "reviewModel", "reviewImage", "confidence"} {
		if ref, exists := item.Source[key]; exists && ref != nil {
			if err := validateAssetRef(ref); err != nil {
				return invalid("source.%s: %v", key, err)
			}
		}
	}
	for _, key := range []string{"width", "height"} {
		if n, ok := item.Source[key].(float64); !ok || n <= 0 || n > 1000000 {
			return invalid("addr-scene requires positive source.%s", key)
		}
	}
	copy := *item
	copy.Source = Object{"url": item.Source["image"].(map[string]any)["url"], "width": item.Source["width"], "height": item.Source["height"]}
	if err := (imageAdapter{}).Validate(&copy); err != nil {
		return err
	}
	for _, mark := range item.Annotations {
		if err := validateAddrSceneAnnotation(mark); err != nil {
			return err
		}
	}
	return nil
}

var sceneKinds = []string{"building", "tree", "road", "car", "solar_panel", "skylight", "chimney", "pool", "driveway", "footpath", "vegetation", "water", "other"}
var sceneStatuses = []string{"candidate", "confirmed", "needs_adjustment", "rejected", "superseded"}
var sceneRemovedFields = []string{"holes", "notes", "visibility", "review_issues", "group_id", "parent_id", "roof_type", "roof_parts", "roof_lines", "roof_annotation", "roof_geometry_source", "roof_geometry_note", "roof_feedback", "roof_line_edits"}

func sceneContains(values []string, value string) bool {
	for _, candidate := range values {
		if candidate == value {
			return true
		}
	}
	return false
}

func sceneAddr(mark Annotation) Object {
	addr, _ := mark.Properties["addr"].(map[string]any)
	return addr
}

func sceneOverrides(mark Annotation) Object {
	overrides, _ := sceneAddr(mark)["overrides"].(map[string]any)
	return overrides
}

func validateAddrSceneAnnotation(mark Annotation) error {
	if mark.Geometry.Type != "Polygon" {
		return invalid("addr-scene annotation %q requires Polygon geometry; use an image or map item for points and lines", mark.ID)
	}
	if mark.Notes != "" {
		return invalid("addr-scene annotation.notes is unsupported; use properties.addr.overrides.notes (empty string clears object notes)")
	}
	addr := sceneAddr(mark)
	if id, ok := addr["objectId"].(string); !ok || id != mark.ID || checkID(id) != nil {
		return invalid("addr-scene annotation.id must equal its valid properties.addr.objectId")
	}
	if value, exists := addr["overrides"]; exists && value != nil {
		if _, ok := value.(map[string]any); !ok {
			return invalid("properties.addr.overrides must be an object")
		}
	}
	if value, exists := addr["removedFields"]; exists {
		fields, ok := value.([]any)
		if !ok {
			return invalid("properties.addr.removedFields must be an array of optional scene field names")
		}
		for _, field := range fields {
			name, ok := field.(string)
			if !ok || !sceneContains(sceneRemovedFields, name) {
				return invalid("properties.addr.removedFields cannot remove %v", field)
			}
		}
	}
	overrides := sceneOverrides(mark)
	if id, exists := overrides["id"]; exists && id != mark.ID {
		return invalid("properties.addr.overrides.id cannot change the object identity")
	}
	for key, values := range map[string][]string{"kind": sceneKinds, "status": sceneStatuses, "visibility": {"full", "occluded", "truncated", "occluded_truncated"}} {
		if value, exists := overrides[key]; exists {
			name, ok := value.(string)
			if !ok || !sceneContains(values, name) {
				return invalid("properties.addr.overrides.%s must be one of %v", key, values)
			}
		}
	}
	for key, limit := range map[string]int{"notes": 100000, "label": 300, "source": 300, "group_id": 128, "parent_id": 128} {
		if value, exists := overrides[key]; exists {
			text, ok := value.(string)
			if !ok || len(text) > limit {
				return invalid("properties.addr.overrides.%s must be a string of at most %d characters", key, limit)
			}
		}
	}
	if value, exists := overrides["geometry_reviewed"]; exists {
		if _, ok := value.(bool); !ok {
			return invalid("properties.addr.overrides.geometry_reviewed must be boolean")
		}
	}
	if value, exists := overrides["model"]; exists {
		model, ok := value.(map[string]any)
		if !ok {
			return invalid("properties.addr.overrides.model must be an object")
		}
		for _, key := range []string{"height_m", "base_m", "width_m", "length_m", "area_m2", "rotation_deg", "footprint_area_px"} {
			if value, exists := model[key]; exists {
				if n, ok := value.(float64); !ok || !finite(n) {
					return invalid("properties.addr.overrides.model.%s must be a finite number", key)
				}
			}
		}
		if value, exists := model["height_verified"]; exists {
			if _, ok := value.(bool); !ok {
				return invalid("properties.addr.overrides.model.height_verified must be boolean")
			}
		}
	}
	return nil
}

// This hook runs on the shared item save path, including SaveAnnotation and
// DeleteAnnotation. It never reads or mutates the immutable source assets.
func validateAddrSceneTransition(old, next *Item) error {
	previous := make(map[string]Annotation, len(old.Annotations))
	for _, mark := range old.Annotations {
		previous[mark.ID] = mark
	}
	for i := range next.Annotations {
		mark := &next.Annotations[i]
		before, exists := previous[mark.ID]
		if !exists {
			overrides := sceneOverrides(*mark)
			for _, key := range []string{"kind", "label", "source", "status", "geometry_reviewed", "model"} {
				if _, exists := overrides[key]; !exists {
					return invalid("new addr-scene objects require complete properties.addr.overrides including %s", key)
				}
			}
			model, _ := overrides["model"].(map[string]any)
			for _, key := range []string{"height_m", "base_m", "height_source", "height_verified", "width_m", "length_m", "area_m2", "rotation_deg", "color"} {
				if _, exists := model[key]; !exists {
					return invalid("new addr-scene objects require complete properties.addr.overrides.model including %s", key)
				}
			}
			continue
		}
		delete(previous, mark.ID)
		if before.LabelID != mark.LabelID {
			return invalid("addr-scene annotation.labelId is the immutable source class; use properties.addr.overrides.kind to change the effective classification")
		}
		for _, key := range []string{"objectId", "layer", "status", "geometry_reviewed", "source", "provenance"} {
			if !sameJSON(sceneAddr(before)[key], sceneAddr(*mark)[key]) {
				return invalid("properties.addr.%s is an immutable source binding; use properties.addr.overrides for scene feedback", key)
			}
		}
		if !sameSceneGeometry(before.Geometry, mark.Geometry) || !sameJSON(sceneOverrides(before)["kind"], sceneOverrides(*mark)["kind"]) {
			invalidateSceneDerived(mark)
		}
	}
	if len(previous) != 0 {
		return invalid("addr-scene annotations cannot be removed; set properties.addr.overrides.status to rejected instead")
	}
	return nil
}

func sameSceneGeometry(a, b Geometry) bool {
	if a.Type != b.Type {
		return false
	}
	var left, right any
	// Decoding makes JSON spelling (1 / 1.0), whitespace and key order irrelevant.
	return json.Unmarshal(a.Coordinates, &left) == nil && json.Unmarshal(b.Coordinates, &right) == nil && sameJSON(left, right)
}

func invalidateSceneDerived(mark *Annotation) {
	addr := sceneAddr(*mark)
	overrides := sceneOverrides(*mark)
	if overrides == nil {
		overrides = Object{}
		addr["overrides"] = overrides
	}
	overrides["geometry_reviewed"] = false
	if status := overrides["status"]; status != "rejected" && status != "superseded" {
		overrides["status"] = "needs_adjustment"
	}
	model, _ := overrides["model"].(map[string]any)
	if model == nil {
		model = Object{}
		overrides["model"] = model
	}
	model["height_verified"] = false
	overrides["roof_annotation"] = nil
	fields := []string{"roof_parts", "roof_lines", "roof_geometry_source", "roof_geometry_note"}
	if kind, ok := overrides["kind"].(string); ok && kind != "building" {
		fields = append(fields, "roof_feedback", "roof_line_edits")
	}
	removed, _ := addr["removedFields"].([]any)
	for _, field := range fields {
		delete(overrides, field)
		found := false
		for _, value := range removed {
			found = found || value == field
		}
		if !found {
			removed = append(removed, field)
		}
	}
	addr["removedFields"] = removed
}

func (addrReportAdapter) Validate(item *Item) error {
	if len(item.Annotations) != 0 || item.Crop != nil {
		return invalid("report items preserve source evidence and do not accept annotation geometry")
	}
	if err := validateAssetRef(item.Source["manifest"]); err != nil {
		return invalid("source.manifest: %v", err)
	}
	for _, key := range []string{"overview", "map", "summary", "boundary", "crm"} {
		if ref, ok := item.Source[key]; ok && ref != nil {
			if err := validateAssetRef(ref); err != nil {
				return invalid("source.%s: %v", key, err)
			}
		}
	}
	return nil
}

func sameJSON(a, b any) bool {
	x, _ := json.Marshal(a)
	y, _ := json.Marshal(b)
	return string(x) == string(y)
}
