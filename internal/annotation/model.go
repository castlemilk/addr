// Package annotation is the standalone, generic annotation CMS. It does not
// depend on the address, roof, or CRM application.
package annotation

import (
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"
)

type Object = map[string]any

var (
	ErrNotFound = errors.New("not found")
	ErrConflict = errors.New("revision conflict: reload the current document before saving")
	ErrInvalid  = errors.New("invalid input")
)

type Base struct {
	ID        string `json:"id"`
	Revision  int64  `json:"revision"`
	CreatedAt string `json:"createdAt"`
	UpdatedAt string `json:"updatedAt"`
}

type Project struct {
	Base
	Name        string `json:"name"`
	Description string `json:"description"`
}

type Label struct {
	Base
	ProjectID string `json:"projectId"`
	Name      string `json:"name"`
	Color     string `json:"color"`
}

type Crop struct {
	X      float64 `json:"x"`
	Y      float64 `json:"y"`
	Width  float64 `json:"width"`
	Height float64 `json:"height"`
}

type Geometry struct {
	Type        string          `json:"type"`
	Coordinates json.RawMessage `json:"coordinates"`
}

type Annotation struct {
	ID         string   `json:"id"`
	LabelID    string   `json:"labelId"`
	Geometry   Geometry `json:"geometry"`
	Notes      string   `json:"notes,omitempty"`
	Properties Object   `json:"properties,omitempty"`
}

type Item struct {
	Base
	ProjectID    string        `json:"projectId"`
	Title        string        `json:"title"`
	Adapter      string        `json:"adapter"`
	Source       Object        `json:"source"`
	Metadata     Object        `json:"metadata"`
	Tags         []string      `json:"tags"`
	Status       string        `json:"status"`
	Notes        string        `json:"notes"`
	Crop         *Crop         `json:"crop,omitempty"`
	Annotations  []Annotation  `json:"annotations"`
	AgentReviews []AgentReview `json:"agentReviews,omitempty"`
}

type Change struct {
	Cursor    string          `json:"cursor"`
	ProjectID string          `json:"projectId"`
	Kind      string          `json:"kind"`
	ID        string          `json:"id"`
	Operation string          `json:"operation"`
	Revision  int64           `json:"revision"`
	Actor     string          `json:"actor"`
	Timestamp string          `json:"timestamp"`
	Document  json.RawMessage `json:"document"`
}

type ChangePage struct {
	Changes    []Change `json:"changes"`
	NextCursor string   `json:"nextCursor"`
	HasMore    bool     `json:"hasMore"`
}

type Export struct {
	SchemaVersion int     `json:"schemaVersion"`
	Project       Project `json:"project"`
	Items         []Item  `json:"items"`
	Labels        []Label `json:"labels"`
	Cursor        string  `json:"cursor"`
}

var validID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)
var validColor = regexp.MustCompile(`^#[0-9a-fA-F]{6}$`)

func invalid(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrInvalid, fmt.Sprintf(format, args...))
}
func checkID(id string) error {
	if !validID.MatchString(id) {
		return invalid("id must be 1–128 letters, digits, hyphens or underscores")
	}
	return nil
}
func checkText(name, value string, min, max int) error {
	if len(strings.TrimSpace(value)) < min || len(value) > max {
		return invalid("%s must be %d–%d characters", name, min, max)
	}
	return nil
}

func normalizeItem(item *Item) {
	if item.Source == nil {
		item.Source = Object{}
	}
	if item.Metadata == nil {
		item.Metadata = Object{}
	}
	if item.Tags == nil {
		item.Tags = []string{}
	}
	if item.Annotations == nil {
		item.Annotations = []Annotation{}
	}
	if item.Status == "" {
		item.Status = "unreviewed"
	}
}
