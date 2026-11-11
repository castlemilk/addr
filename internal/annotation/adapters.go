package annotation

import (
	"encoding/json"
	"math"
	"net/url"
)

// Adapter validates source, geometry and view transforms for an item family.
// New adapters register here; storage, revisions and change delivery stay shared.
type Adapter interface {
	Info() AdapterInfo
	Validate(*Item) error
}

type AdapterInfo struct {
	ID               string   `json:"id"`
	Name             string   `json:"name"`
	CoordinateSystem string   `json:"coordinateSystem"`
	GeometryTypes    []string `json:"geometryTypes"`
	SupportsCrop     bool     `json:"supportsCrop"`
	WriteHints       []string `json:"writeHints,omitempty"`
}

type mapAdapter struct{}
type imageAdapter struct{}

func (mapAdapter) Info() AdapterInfo {
	return AdapterInfo{ID: "map", Name: "Map", CoordinateSystem: "WGS84 longitude/latitude", GeometryTypes: []string{"Point", "LineString", "Polygon"}}
}
func (imageAdapter) Info() AdapterInfo {
	return AdapterInfo{ID: "image", Name: "Image", CoordinateSystem: "Normalized original image (0..1)", GeometryTypes: []string{"Point", "LineString", "Polygon"}, SupportsCrop: true}
}

func DefaultAdapters() map[string]Adapter {
	return map[string]Adapter{"map": mapAdapter{}, "image": imageAdapter{}, "addr-scene": addrSceneAdapter{}, "addr-report": addrReportAdapter{}}
}

func (mapAdapter) Validate(item *Item) error {
	if item.Crop != nil {
		return invalid("crop is supported only for image items")
	}
	if center, ok := item.Source["center"]; ok {
		data, _ := json.Marshal(center)
		var coords []float64
		if !numericCoordinateScalars(data, 1) || json.Unmarshal(data, &coords) != nil || !validPoint(coords, false) {
			return invalid("map source.center must be [longitude,latitude]")
		}
	}
	if zoom, ok := item.Source["zoom"]; ok {
		n, ok := zoom.(float64)
		if !ok || !finite(n) || n < 0 || n > 24 {
			return invalid("map source.zoom must be between 0 and 24")
		}
	}
	for _, a := range item.Annotations {
		if err := validateGeometry(a.Geometry, false); err != nil {
			return err
		}
	}
	return nil
}

func (imageAdapter) Validate(item *Item) error {
	src, ok := item.Source["url"].(string)
	u, err := url.Parse(src)
	if !ok || err != nil || src == "" || !((u.Scheme == "https" || u.Scheme == "http") && u.Host != "" || u.Scheme == "" && len(src) > 0 && src[0] == '/' && u.Host == "") {
		return invalid("image source.url must be an HTTP(S) URL or an absolute site path")
	}
	for _, key := range []string{"width", "height"} {
		if raw, exists := item.Source[key]; exists {
			n, ok := raw.(float64)
			if !ok || !finite(n) || n <= 0 || n > 1000000 || math.Trunc(n) != n {
				return invalid("image source.%s must be a positive integer", key)
			}
		}
	}
	if c := item.Crop; c != nil {
		if !finite(c.X) || !finite(c.Y) || !finite(c.Width) || !finite(c.Height) || c.X < 0 || c.Y < 0 || c.Width <= 0 || c.Height <= 0 || c.X+c.Width > 1+1e-9 || c.Y+c.Height > 1+1e-9 {
			return invalid("crop must be a positive rectangle inside the original image (0..1)")
		}
	}
	for _, a := range item.Annotations {
		if err := validateGeometry(a.Geometry, true); err != nil {
			return err
		}
	}
	return nil
}

func finite(n float64) bool { return !math.IsNaN(n) && !math.IsInf(n, 0) }

// Check the JSON values before decoding into float64 slices: encoding/json
// accepts null into a float64 and silently leaves a zero value behind.
func numericCoordinateScalars(data json.RawMessage, depth int) bool {
	var value any
	if json.Unmarshal(data, &value) != nil {
		return false
	}
	var valid func(any, int) bool
	valid = func(value any, depth int) bool {
		if depth == 0 {
			number, ok := value.(float64)
			return ok && finite(number)
		}
		values, ok := value.([]any)
		if !ok {
			return false
		}
		for _, entry := range values {
			if !valid(entry, depth-1) {
				return false
			}
		}
		return true
	}
	return valid(value, depth)
}

func validPoint(p []float64, image bool) bool {
	if len(p) != 2 || !finite(p[0]) || !finite(p[1]) {
		return false
	}
	if image {
		return p[0] >= 0 && p[0] <= 1 && p[1] >= 0 && p[1] <= 1
	}
	return p[0] >= -180 && p[0] <= 180 && p[1] >= -90 && p[1] <= 90
}

func validateGeometry(g Geometry, image bool) error {
	bad := invalid("%s geometry requires valid %s coordinates", g.Type, map[bool]string{true: "normalized image", false: "longitude/latitude"}[image])
	switch g.Type {
	case "Point":
		var p []float64
		if !numericCoordinateScalars(g.Coordinates, 1) || json.Unmarshal(g.Coordinates, &p) != nil || !validPoint(p, image) {
			return bad
		}
	case "LineString":
		var points [][]float64
		if !numericCoordinateScalars(g.Coordinates, 2) || json.Unmarshal(g.Coordinates, &points) != nil || len(points) < 2 || len(points) > 10000 {
			return bad
		}
		for _, p := range points {
			if !validPoint(p, image) {
				return bad
			}
		}
	case "Polygon":
		var rings [][][]float64
		if !numericCoordinateScalars(g.Coordinates, 3) || json.Unmarshal(g.Coordinates, &rings) != nil || len(rings) < 1 || len(rings) > 100 {
			return bad
		}
		for _, points := range rings {
			if len(points) < 4 || len(points) > 10000 {
				return bad
			}
			for _, p := range points {
				if !validPoint(p, image) {
					return bad
				}
			}
			first, last := points[0], points[len(points)-1]
			if first[0] != last[0] || first[1] != last[1] {
				return invalid("polygon rings must be closed")
			}
			area := 0.0
			for j := 1; j < len(points); j++ {
				area += points[j-1][0]*points[j][1] - points[j][0]*points[j-1][1]
			}
			if math.Abs(area) < 1e-15 {
				return invalid("polygon rings must have nonzero area")
			}
		}
	default:
		return invalid("unsupported geometry type %q", g.Type)
	}
	return nil
}

func validateItem(item *Item, adapters map[string]Adapter) error {
	if err := checkText("title", item.Title, 1, 300); err != nil {
		return err
	}
	if err := checkID(item.ProjectID); err != nil {
		return err
	}
	if len(item.Notes) > 100000 || len(item.Tags) > 100 || len(item.Annotations) > 5000 {
		return invalid("item exceeds notes, tags or annotation limit")
	}
	for _, tag := range item.Tags {
		if err := checkText("tag", tag, 1, 100); err != nil {
			return err
		}
	}
	switch item.Status {
	case "unreviewed", "in_review", "approved", "rejected":
	default:
		return invalid("unsupported review status %q", item.Status)
	}
	seen := map[string]bool{}
	for _, a := range item.Annotations {
		if err := checkID(a.ID); err != nil {
			return invalid("annotation %v", err)
		}
		if seen[a.ID] {
			return invalid("duplicate annotation id %q", a.ID)
		}
		seen[a.ID] = true
		if len(a.Notes) > 100000 {
			return invalid("annotation notes too long")
		}
	}
	adapter, ok := adapters[item.Adapter]
	if !ok {
		return invalid("unsupported adapter %q", item.Adapter)
	}
	return adapter.Validate(item)
}
