package annotation

import (
	"context"
	"encoding/json"
	"strings"
	"time"

	"github.com/google/uuid"
)

// AgentReview is an append-only assessment, independent of the item's human
// status and annotation overrides. Its subject retains the evidence reviewed.
type AgentReview struct {
	ID               string `json:"id"`
	AnnotationID     string `json:"annotationId,omitempty"`
	Reviewer         string `json:"reviewer"`
	Decision         string `json:"decision"`
	Notes            string `json:"notes"`
	VegetationType   string `json:"vegetationType,omitempty"`
	Source           string `json:"source"`
	HumanApproval    bool   `json:"humanApproval"`
	TrainingEligible bool   `json:"trainingEligible"`
	BasedOnRevision  int64  `json:"basedOnRevision"`
	CreatedAt        string `json:"createdAt"`
	Supersedes       string `json:"supersedes,omitempty"`
	Subject          Object `json:"subject"`
}

type agentReviewWriteKey struct{}

// reviewSubject excludes human decisions and notes on an annotation. A human
// answer alone does not stale an agent assessment; changed evidence does.
func reviewSubject(item Item, annotationID string) (Object, error) {
	marks := []Object{}
	for _, mark := range item.Annotations {
		if annotationID != "" && mark.ID != annotationID {
			continue
		}
		entry := Object{"id": mark.ID, "labelId": mark.LabelID, "geometry": mark.Geometry}
		addr, _ := mark.Properties["addr"].(map[string]any)
		overrides, _ := addr["overrides"].(map[string]any)
		for _, key := range []string{"kind", "label", "structure_kind", "vegetation_type"} {
			value := addr[key]
			if edited, exists := overrides[key]; exists {
				value = edited
			}
			entry[key] = value
		}
		marks = append(marks, entry)
	}
	if annotationID != "" && len(marks) != 1 {
		return nil, invalid("annotationId must name an existing annotation in this item")
	}
	subject := Object{"adapter": item.Adapter, "source": item.Source, "title": item.Title, "annotations": marks, "crop": item.Crop}
	if annotationID == "" {
		subject["notes"] = item.Notes
	}
	return subject, nil
}

func (s *Service) saveAgentReview(ctx context.Context, data Object) (Object, error) {
	var req struct {
		ItemID           string `json:"itemId"`
		AnnotationID     string `json:"annotationId"`
		ExpectedRevision int64  `json:"expectedRevision"`
		Reviewer         string `json:"reviewer"`
		Decision         string `json:"decision"`
		Notes            string `json:"notes"`
		VegetationType   string `json:"vegetationType"`
	}
	// The narrow endpoint cannot accept geometry, human flags, actor or records
	// supplied by a caller, including differently spelled JSON keys.
	allowed := map[string]bool{"itemId": true, "annotationId": true, "expectedRevision": true, "reviewer": true, "decision": true, "notes": true, "vegetationType": true}
	for key := range data {
		if !allowed[key] {
			return nil, invalid("unknown agent review argument %q", key)
		}
	}
	if err := decodeObject(data, &req); err != nil {
		return nil, err
	}
	if err := checkID(req.ItemID); err != nil {
		return nil, err
	}
	if err := checkID(req.Reviewer); err != nil {
		return nil, invalid("reviewer must be a stable agent ID, such as codex")
	}
	if req.ExpectedRevision <= 0 || req.ExpectedRevision >= MaxSafeInteger {
		return nil, invalid("current expectedRevision of the parent item is required")
	}
	switch req.Decision {
	case "approved", "rejected", "needs_adjustment", "uncertain":
	default:
		return nil, invalid("decision must be approved, rejected, needs_adjustment or uncertain")
	}
	if err := checkText("agent review notes", req.Notes, 1, 10000); err != nil {
		return nil, err
	}
	if req.VegetationType != "" {
		switch req.VegetationType {
		case "grass", "hedge", "shrub", "tree":
		default:
			return nil, invalid("vegetationType must be grass, hedge, shrub or tree")
		}
		if req.AnnotationID == "" || req.Decision != "approved" {
			return nil, invalid("vegetationType requires an approved annotation review")
		}
	}
	doc, err := s.Store.Get(ctx, "item", req.ItemID)
	if err != nil {
		return nil, err
	}
	var item Item
	if err := json.Unmarshal(doc, &item); err != nil {
		return nil, err
	}
	if item.Revision != req.ExpectedRevision {
		return nil, ErrConflict
	}
	subject, err := reviewSubject(item, req.AnnotationID)
	if err != nil {
		return nil, err
	}
	review := AgentReview{ID: "ar_" + uuid.NewString(), AnnotationID: req.AnnotationID, Reviewer: req.Reviewer,
		Decision: req.Decision, Notes: strings.TrimSpace(req.Notes), VegetationType: req.VegetationType,
		Source: "agent:fieldwork", BasedOnRevision: item.Revision, CreatedAt: time.Now().UTC().Format(time.RFC3339Nano), Subject: subject}
	for i := len(item.AgentReviews) - 1; i >= 0; i-- {
		prior := item.AgentReviews[i]
		if prior.Reviewer == review.Reviewer && prior.AnnotationID == review.AnnotationID {
			review.Supersedes = prior.ID
			break
		}
	}
	item.AgentReviews = append(item.AgentReviews, review)
	input, err := json.Marshal(item)
	if err != nil {
		return nil, err
	}
	// Actor is assigned by the service, never inferred from a bearer token or
	// accepted from the request. Human browser saves keep their existing actor.
	doc, err = s.Store.Save(context.WithValue(ctx, agentReviewWriteKey{}, true), "item", item.ID, req.ExpectedRevision,
		"agent:fieldwork:"+req.Reviewer, input)
	if err != nil {
		return nil, err
	}
	return objectOf(Object{"item": doc, "agentReview": review})
}

func preserveAgentReviews(ctx context.Context, old *Item, item *Item) error {
	if allowed, _ := ctx.Value(agentReviewWriteKey{}).(bool); allowed {
		if len(item.AgentReviews) != len(old.AgentReviews)+1 || len(old.AgentReviews) > 0 && !sameJSON(item.AgentReviews[:len(old.AgentReviews)], old.AgentReviews) {
			return invalid("agent review history is append-only")
		}
		return nil
	}
	// Older browsers and clients omit this new field. Preserve it, never erase it.
	if len(item.AgentReviews) != 0 && !sameJSON(item.AgentReviews, old.AgentReviews) {
		return invalid("agentReviews is server-managed; use SaveAgentReview")
	}
	item.AgentReviews = old.AgentReviews
	return nil
}
