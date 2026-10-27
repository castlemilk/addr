package migration

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"

	"github.com/benebsworth/addr/internal/annotation"
)

type Plan struct {
	SchemaVersion int                `json:"schemaVersion"`
	SnapshotID    string             `json:"snapshotId"`
	Manifest      annotation.Object  `json:"manifest"`
	Project       annotation.Project `json:"project"`
	Labels        []annotation.Label `json:"labels"`
	Items         []annotation.Item  `json:"items"`
	Stats         annotation.Object  `json:"stats"`
}

type sceneModel struct {
	ID            string              `json:"id"`
	Title         string              `json:"title"`
	ImageSHA256   string              `json:"image_sha256"`
	Width         float64             `json:"image_width"`
	Height        float64             `json:"image_height"`
	Signature     string              `json:"model_signature"`
	Objects       []annotation.Object `json:"objects"`
	PixelToMeters any                 `json:"pixel_to_meters"`
	MetersToPixel any                 `json:"meters_to_pixel"`
}
type sceneReview struct {
	Revision  int                 `json:"revision"`
	Signature string              `json:"model_signature"`
	UpdatedBy string              `json:"updated_by"`
	UpdatedAt string              `json:"updated_at"`
	Objects   []annotation.Object `json:"objects"`
}

func BuildPlan(ctx context.Context, b *Bundle, projectID string) (*Plan, error) {
	if !idPattern.MatchString(projectID) || len(projectID) > 80 {
		return nil, fmt.Errorf("invalid project ID")
	}
	p := &Plan{SchemaVersion: 1, SnapshotID: b.Manifest.SnapshotID, Manifest: b.ManifestRef(), Project: annotation.Project{Base: annotation.Base{ID: projectID}, Name: "Mooroolbark", Description: "Frozen local scene, review, provenance and analysis dataset. Snapshot " + b.Manifest.SnapshotID + ". Source object statuses and actor provenance are retained; import does not certify human approval."}, Labels: []annotation.Label{}, Items: []annotation.Item{}}
	kinds := map[string]bool{}
	objects := 0
	reviews := 0
	maxBytes := 0
	sceneItems := annotation.Object{}
	for _, sceneID := range b.Manifest.SceneIDs {
		item, hasReview, err := b.sceneItem(ctx, sceneID, projectID)
		if err != nil {
			return nil, fmt.Errorf("scene %s: %w", sceneID, err)
		}
		for _, a := range item.Annotations {
			kinds[strings.TrimPrefix(a.LabelID, projectID+"-kind-")] = true
		}
		objects += len(item.Annotations)
		if hasReview {
			reviews++
		}
		if err := validateProjection(&item); err != nil {
			return nil, fmt.Errorf("scene %s: %w", sceneID, err)
		}
		data, _ := json.Marshal(item)
		if len(data) > maxBytes {
			maxBytes = len(data)
		}
		p.Items = append(p.Items, item)
		sceneItems[sceneID] = item.ID
	}
	keys := []string{}
	for kind := range kinds {
		keys = append(keys, kind)
	}
	sort.Strings(keys)
	colors := map[string]string{"building": "#D19A69", "tree": "#58A675", "solar_panel": "#588BD2", "road": "#8A96A4", "car": "#D4676D", "pool": "#51B5C5", "driveway": "#ABA08F", "footpath": "#BCAC93", "vegetation": "#91B85C", "water": "#538AB0"}
	for _, kind := range keys {
		color := colors[kind]
		if color == "" {
			color = "#9A93C8"
		}
		p.Labels = append(p.Labels, annotation.Label{Base: annotation.Base{ID: projectID + "-kind-" + kind}, ProjectID: projectID, Name: strings.ReplaceAll(kind, "_", " "), Color: color})
	}
	report := annotation.Item{Base: annotation.Base{ID: ReportID(projectID)}, ProjectID: projectID, Title: "Mooroolbark · Local dataset and analysis", Adapter: "addr-report", Source: annotation.Object{"manifest": b.ManifestRef(), "sceneItems": sceneItems}, Metadata: annotation.Object{"addr": annotation.Object{"schemaVersion": 1, "kind": "dataset-report", "snapshotId": b.Manifest.SnapshotID, "importedAt": b.Manifest.CreatedAt, "summary": b.Manifest.Summary, "exclusions": b.Manifest.Exclusions}}, Tags: []string{"mooroolbark", "imported", "dataset-report"}, Status: "unreviewed", Annotations: []annotation.Annotation{}}
	for key, name := range map[string]string{"overview": "data/fieldwork-derived/mooroolbark/processing.json", "map": "data/fieldwork-derived/mooroolbark/processing-map.geojson", "summary": "data/fieldwork-derived/mooroolbark/processing-summary.json", "boundary": "data/suburbs/mooroolbark/boundary.geojson", "crm": "data/fieldwork-derived/mooroolbark/crm.json"} {
		ref, err := b.Ref(name)
		if err != nil {
			return nil, err
		}
		report.Source[key] = ref
	}
	if err := validateProjection(&report); err != nil {
		return nil, err
	}
	p.Items = append(p.Items, report)
	aggregateBytes := 0
	for _, item := range p.Items {
		data, _ := json.Marshal(item)
		aggregateBytes += len(data)
	}
	var originalBytes, uniqueBytes int64
	for _, file := range b.Manifest.Files {
		originalBytes += file.Bytes
	}
	for _, chunk := range b.Chunks {
		uniqueBytes += chunk.Bytes
	}
	p.Stats = annotation.Object{"scenes": len(b.Manifest.SceneIDs), "sceneReviews": reviews, "items": len(p.Items), "annotations": objects, "labels": len(p.Labels), "files": len(b.Manifest.Files), "originalBytes": originalBytes, "uniqueBlobs": len(b.Chunks), "uniqueBlobBytes": uniqueBytes, "maxSceneDocumentBytes": maxBytes, "aggregateItemBytes": aggregateBytes}
	return p, nil
}

func validateProjection(item *annotation.Item) error {
	data, err := json.Marshal(item)
	if err != nil {
		return err
	}
	if len(data) > annotation.MaxDocumentBytes-512 {
		return fmt.Errorf("editable projection exceeds the 2 MiB document limit")
	}
	// Normalize JSON numbers to the same types the HTTP service receives.
	var normalized annotation.Item
	if err := json.Unmarshal(data, &normalized); err != nil {
		return err
	}
	if !idPattern.MatchString(item.ID) || len(item.Title) > 300 || len(item.Annotations) > 5000 {
		return fmt.Errorf("editable projection exceeds document bounds")
	}
	seen := map[string]bool{}
	for _, a := range item.Annotations {
		if !idPattern.MatchString(a.ID) || !idPattern.MatchString(a.LabelID) || seen[a.ID] {
			return fmt.Errorf("invalid or repeated object ID %q", a.ID)
		}
		seen[a.ID] = true
	}
	return annotation.DefaultAdapters()[item.Adapter].Validate(&normalized)
}

// SceneFiles abstracts the frozen bundle and a live local scene directory so
// the same projection produces frozen (generation 0) and pushed generation items.
type SceneFiles interface {
	Has(path string) bool
	ReadJSON(ctx context.Context, path string, into any) error
	Ref(path string) (annotation.Object, error)
}

// Has reports whether the frozen manifest lists the path.
func (b *Bundle) Has(path string) bool {
	_, ok := b.Files[path]
	return ok
}

// ProjectOptions customises a scene projection. Zero options reproduce the
// frozen import projection byte for byte (see sceneItem).
type ProjectOptions struct {
	// ItemID overrides SceneID(project, scene).
	ItemID string
	// ExtraBindings copies replaces, clipped_by_frame, structure_kind,
	// has_roof_line_edits and fieldwork_import into properties.addr. They are
	// immutable source bindings, never overrides.
	ExtraBindings bool
	// Metadata is the base metadata.addr object; review-derived keys and the
	// object summary are added to it.
	Metadata annotation.Object
	// Tags precede the layer tag.
	Tags []string
	// AssetPaths become source.assets refs, in order.
	AssetPaths []string
}

var extraBindingKeys = []string{"replaces", "clipped_by_frame", "structure_kind"}

// ProjectScene projects one scene's effective objects (review layer when a
// review exists, else the model layer) into an addr-scene item. Statuses and
// provenance are copied verbatim; nothing is promoted to a human label.
func ProjectScene(ctx context.Context, files SceneFiles, project, id string, opts ProjectOptions) (annotation.Item, bool, error) {
	prefix := "data/scenes/" + id + "/"
	var model sceneModel
	if err := files.ReadJSON(ctx, prefix+"latest.json", &model); err != nil {
		return annotation.Item{}, false, err
	}
	if model.ID != id || !digestPattern.MatchString(model.Signature) || !digestPattern.MatchString(model.ImageSHA256) || model.Width <= 0 || model.Height <= 0 {
		return annotation.Item{}, false, fmt.Errorf("model frame or signature is invalid")
	}
	imageRef, err := files.Ref(prefix + model.ImageSHA256 + ".png")
	if err != nil {
		return annotation.Item{}, false, err
	}
	if imageRef["sha256"] != model.ImageSHA256 {
		return annotation.Item{}, false, fmt.Errorf("image filename does not match bytes")
	}
	modelRef, err := files.Ref(prefix + "latest.json")
	if err != nil {
		return annotation.Item{}, false, err
	}
	source := annotation.Object{"sceneId": id, "image": imageRef, "model": modelRef, "modelSignature": model.Signature, "imageSha256": model.ImageSHA256, "width": model.Width, "height": model.Height}
	meta := annotation.Object{}
	for key, value := range opts.Metadata {
		meta[key] = value
	}
	if _, ok := meta["sceneId"]; !ok {
		meta["sceneId"] = id
	}
	objects := model.Objects
	layer := "model"
	hasReview := false
	if files.Has(prefix + "review.json") {
		var review sceneReview
		if err := files.ReadJSON(ctx, prefix+"review.json", &review); err != nil {
			return annotation.Item{}, false, err
		}
		if !digestPattern.MatchString(review.Signature) {
			return annotation.Item{}, false, fmt.Errorf("invalid review model signature")
		}
		var reviewModel sceneModel
		if err := files.ReadJSON(ctx, prefix+"models/"+review.Signature+".json", &reviewModel); err != nil {
			return annotation.Item{}, false, err
		}
		if reviewModel.ImageSHA256 != model.ImageSHA256 || reviewModel.Width != model.Width || reviewModel.Height != model.Height || !equalJSON(reviewModel.PixelToMeters, model.PixelToMeters) || !equalJSON(reviewModel.MetersToPixel, model.MetersToPixel) {
			return annotation.Item{}, false, fmt.Errorf("review uses a different image frame; explicit frame migration is required")
		}
		source["review"], _ = files.Ref(prefix + "review.json")
		source["reviewModel"], _ = files.Ref(prefix + "models/" + review.Signature + ".json")
		meta["sourceReviewRevision"] = review.Revision
		meta["reviewModelSignature"] = review.Signature
		meta["stale"] = review.Signature != model.Signature
		meta["reviewActor"] = review.UpdatedBy
		meta["reviewUpdatedAt"] = review.UpdatedAt
		objects = review.Objects
		layer = "review"
		hasReview = true
	}
	if files.Has(prefix + "confidence.json") {
		source["confidence"], _ = files.Ref(prefix + "confidence.json")
	}
	assets := []annotation.Object{}
	for _, path := range opts.AssetPaths {
		ref, err := files.Ref(path)
		if err != nil {
			return annotation.Item{}, false, err
		}
		assets = append(assets, ref)
	}
	source["assets"] = assets
	title := model.Title
	if title == "" {
		title = id
	}
	status := "unreviewed"
	if hasReview {
		status = "in_review"
	}
	itemID := opts.ItemID
	if itemID == "" {
		itemID = SceneID(project, id)
	}
	tags := append(append([]string{}, opts.Tags...), layer)
	item := annotation.Item{Base: annotation.Base{ID: itemID}, ProjectID: project, Title: title, Adapter: "addr-scene", Source: source, Metadata: annotation.Object{"addr": meta}, Tags: tags, Status: status, Annotations: []annotation.Annotation{}}
	counts := map[string]int{}
	statusCounts := map[string]int{}
	for _, object := range objects {
		objectID, _ := object["id"].(string)
		kind, _ := object["kind"].(string)
		if kind == "" {
			return annotation.Item{}, false, fmt.Errorf("object %s has no classification", objectID)
		}
		ring, err := normalizedRing(object["ring"], model.Width, model.Height)
		if err != nil {
			return annotation.Item{}, false, fmt.Errorf("object %s: %w", objectID, err)
		}
		rings := [][][]float64{ring}
		if holes, ok := object["holes"].([]any); ok {
			for _, hole := range holes {
				ring, err := normalizedRing(hole, model.Width, model.Height)
				if err != nil {
					return annotation.Item{}, false, fmt.Errorf("object %s hole: %w", objectID, err)
				}
				rings = append(rings, ring)
			}
		}
		coords, _ := json.Marshal(rings)
		addr := annotation.Object{"objectId": objectID, "layer": layer}
		for _, key := range []string{"status", "geometry_reviewed", "source", "provenance"} {
			if value, ok := object[key]; ok {
				addr[key] = value
			}
		}
		if opts.ExtraBindings {
			for _, key := range extraBindingKeys {
				if value, ok := object[key]; ok {
					addr[key] = value
				}
			}
			if _, ok := object["roof_line_edits"]; ok {
				addr["has_roof_line_edits"] = true
			}
			if marker, ok := object["fieldwork_import"].(map[string]any); ok && marker["pending"] == true {
				addr["fieldwork_import"] = marker
			}
		}
		item.Annotations = append(item.Annotations, annotation.Annotation{ID: objectID, LabelID: project + "-kind-" + kind, Geometry: annotation.Geometry{Type: "Polygon", Coordinates: coords}, Properties: annotation.Object{"addr": addr}})
		counts[kind]++
		if value, ok := object["status"].(string); ok {
			statusCounts[value]++
		}
	}
	meta["summary"] = annotation.Object{"objects": len(objects), "classes": counts, "statuses": statusCounts, "layer": layer}
	return item, hasReview, nil
}

// sceneItem is the frozen generation-0 projection used by the import plan.
func (b *Bundle) sceneItem(ctx context.Context, id, project string) (annotation.Item, bool, error) {
	prefix := "data/scenes/" + id + "/"
	assets := []string{}
	for _, file := range b.Manifest.Files {
		if strings.HasPrefix(file.Path, prefix) {
			assets = append(assets, file.Path)
		}
	}
	meta := annotation.Object{"schemaVersion": 1, "sceneId": id, "snapshotId": b.Manifest.SnapshotID, "importRun": b.Manifest.SnapshotID, "importedAt": b.Manifest.CreatedAt, "stale": false}
	item, hasReview, err := ProjectScene(ctx, b, project, id, ProjectOptions{Metadata: meta, Tags: []string{"mooroolbark", "imported"}, AssetPaths: assets})
	if err != nil {
		return item, hasReview, err
	}
	analysisPath := "data/fieldwork-derived/mooroolbark/analysis/" + id + ".json"
	if b.Has(analysisPath) {
		ref, _ := b.Ref(analysisPath)
		item.Source["analysisRefs"] = []annotation.Object{{"title": "Stored scene analysis", "kind": "eyes", "ref": ref}}
	}
	return item, hasReview, nil
}

func normalizedRing(raw any, width, height float64) ([][]float64, error) {
	points, ok := raw.([]any)
	if !ok || len(points) < 3 {
		return nil, fmt.Errorf("polygon ring requires at least three points")
	}
	out := make([][]float64, 0, len(points)+1)
	for _, value := range points {
		pair, ok := value.([]any)
		if !ok || len(pair) != 2 {
			return nil, fmt.Errorf("polygon point requires two numeric coordinates")
		}
		x, xok := pair[0].(float64)
		y, yok := pair[1].(float64)
		if !xok || !yok || math.IsNaN(x) || math.IsInf(x, 0) || math.IsNaN(y) || math.IsInf(y, 0) {
			return nil, fmt.Errorf("polygon coordinate is not finite numeric")
		}
		out = append(out, []float64{x / width, y / height})
	}
	first, last := out[0], out[len(out)-1]
	if first[0] != last[0] || first[1] != last[1] {
		out = append(out, []float64{first[0], first[1]})
	}
	return out, nil
}

func equalJSON(a, b any) bool {
	x, _ := json.Marshal(a)
	y, _ := json.Marshal(b)
	return string(x) == string(y)
}

func ReportID(project string) string { return "000-" + project + "-report" }

// GenerationSceneID names a pushed generation of a scene: the frozen scene id
// plus the model signature prefix and the review revision (0 without a review).
func GenerationSceneID(project, scene, modelSignature string, reviewRevision int) string {
	prefix := modelSignature
	if len(prefix) > 12 {
		prefix = prefix[:12]
	}
	return SceneID(project, scene) + "-m" + prefix + "-r" + strconv.Itoa(reviewRevision)
}
func SceneID(project, scene string) string {
	if project == "mooroolbark" {
		return "addr-scene-" + scene
	}
	return project + "-scene-" + scene
}
