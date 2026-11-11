package annotation

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
)

func agentReviewFixture(t *testing.T) (*Service, Item) {
	t.Helper()
	store := openTestStore(t)
	createProject(t, store, "mooroolbark-review")
	item := imageItem("mooroolbark-review")
	item.Status = "approved"
	item.Notes = "Human review remains independent"
	mark := polygonAnnotation("proposal", "")
	mark.Properties = Object{"addr": Object{"kind": "tree", "overrides": Object{"status": "confirmed", "geometry_reviewed": true}}}
	item.Annotations = []Annotation{mark}
	if err := json.Unmarshal(saveTest(t, store, "item", "review-item", 0, item), &item); err != nil {
		t.Fatal(err)
	}
	return &Service{Store: store}, item
}

func agentReviewArgs(item Item) Object {
	return Object{"itemId": item.ID, "annotationId": "proposal", "expectedRevision": item.Revision, "reviewer": "codex", "decision": "approved", "notes": "Visible crown edge supported; the shadow falls outside the outline."}
}

func TestAgentAndHumanReviewsRemainIndependent(t *testing.T) {
	s, item := agentReviewFixture(t)
	before := item
	result, err := s.callMCP(context.Background(), "save_agent_review", agentReviewArgs(item))
	if err != nil {
		t.Fatal(err)
	}
	var saved Item
	if err := decodeObject(result["item"].(map[string]any), &saved); err != nil {
		t.Fatal(err)
	}
	if saved.Revision != 2 || len(saved.AgentReviews) != 1 {
		t.Fatalf("missing review: %+v", saved)
	}
	review := saved.AgentReviews[0]
	if review.Source != "agent:fieldwork" || review.Reviewer != "codex" || review.BasedOnRevision != 1 || review.HumanApproval || review.TrainingEligible {
		t.Fatalf("wrong provenance: %+v", review)
	}
	withoutReviews := saved
	withoutReviews.AgentReviews = nil
	withoutReviews.Base = before.Base
	if !sameJSON(withoutReviews, before) {
		t.Fatal("agent review changed human state, geometry or provenance")
	}
	changes, err := s.Store.Changes(context.Background(), item.ProjectID, "0", 500)
	if err != nil {
		t.Fatal(err)
	}
	if changes.Changes[len(changes.Changes)-1].Actor != "agent:fieldwork:codex" {
		t.Fatal("agent save attributed to a human")
	}

	// A browser opened before agentReviews existed can still save a human
	// decision; the service preserves the append-only history automatically.
	saved.Status = "rejected"
	saved.Notes = "Human disagrees with the proposal"
	saved.AgentReviews = nil
	doc, err := s.Store.Save(context.Background(), "item", saved.ID, saved.Revision, "workspace", raw(t, saved))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(doc, &saved); err != nil {
		t.Fatal(err)
	}
	if len(saved.AgentReviews) != 1 || saved.Status != "rejected" {
		t.Fatal("human save lost agent history")
	}
	args := agentReviewArgs(saved)
	args["decision"] = "uncertain"
	result, err = s.Call(context.Background(), "SaveAgentReview", args)
	if err != nil {
		t.Fatal(err)
	}
	if err := decodeObject(result["item"].(map[string]any), &saved); err != nil {
		t.Fatal(err)
	}
	if saved.Status != "rejected" || len(saved.AgentReviews) != 2 || saved.AgentReviews[1].Supersedes != review.ID {
		t.Fatal("independent history not preserved")
	}
	if _, err := s.Call(context.Background(), "SaveAgentReview", args); !errors.Is(err, ErrConflict) {
		t.Fatalf("stale save: %v", err)
	}

	// Ordinary saves cannot forge provenance or rewrite a previous assessment.
	saved.AgentReviews[0].HumanApproval = true
	if _, err := s.Store.Save(context.Background(), "item", saved.ID, saved.Revision, "workspace", raw(t, saved)); !errors.Is(err, ErrInvalid) {
		t.Fatalf("forged approval: %v", err)
	}
}

func TestAgentReviewValidationAndNoForgedHumanFlags(t *testing.T) {
	s, item := agentReviewFixture(t)
	for _, change := range []Object{
		{"annotationId": "missing"}, {"reviewer": "human:reviewer"}, {"decision": "confirmed"}, {"notes": " "},
		{"humanApproval": true}, {"actor": "workspace"}, {"geometry": Object{}}, {"ExpectedRevision": 1},
		{"vegetationType": "oak"}, {"vegetationType": "grass", "decision": "rejected"},
	} {
		args := agentReviewArgs(item)
		for key, value := range change {
			args[key] = value
		}
		if _, err := s.Call(context.Background(), "SaveAgentReview", args); !errors.Is(err, ErrInvalid) {
			t.Errorf("accepted %v: %v", change, err)
		}
	}
	doc, err := s.Store.Get(context.Background(), "item", item.ID)
	if err != nil {
		t.Fatal(err)
	}
	var saved Item
	if err := json.Unmarshal(doc, &saved); err != nil {
		t.Fatal(err)
	}
	if saved.Revision != 1 || len(saved.AgentReviews) != 0 {
		t.Fatal("failed validation changed the document")
	}
	args := agentReviewArgs(item)
	args["annotationId"] = ""
	if _, err := s.Call(context.Background(), "SaveAgentReview", args); err != nil {
		t.Fatal("whole-item review:", err)
	}
}

func TestListItemsSummaryDropsHeavyFieldsAndKeepsTheCount(t *testing.T) {
	s, _ := agentReviewFixture(t)
	if _, err := s.Call(context.Background(), "SaveAgentReview", agentReviewArgs(mustGetItem(t, s, "review-item"))); err != nil {
		t.Fatal(err)
	}
	full, err := s.Call(context.Background(), "ListItems", Object{"projectId": "mooroolbark-review"})
	if err != nil {
		t.Fatal(err)
	}
	summary, err := s.Call(context.Background(), "ListItems", Object{"projectId": "mooroolbark-review", "summary": true, "tag": "EXAMPLE"})
	if err != nil {
		t.Fatal(err)
	}
	f := full["items"].([]any)[0].(map[string]any)
	m := summary["items"].([]any)[0].(map[string]any)
	if len(f["annotations"].([]any)) != 1 || len(f["agentReviews"].([]any)) != 1 || f["summary"] != nil {
		t.Fatalf("full list changed: %v", f)
	}
	if len(m["annotations"].([]any)) != 0 || m["annotationCount"] != float64(1) || m["summary"] != true || m["agentReviews"] != nil || m["notes"] != nil || m["metadata"] != nil {
		t.Fatalf("summary entry: %v", m)
	}
	if m["title"] != f["title"] || m["status"] != f["status"] || m["revision"] != f["revision"] {
		t.Fatal("summary lost list fields")
	}
}

func mustGetItem(t *testing.T, s *Service, id string) Item {
	t.Helper()
	result, err := s.Call(context.Background(), "GetItem", Object{"id": id})
	if err != nil {
		t.Fatal(err)
	}
	var item Item
	if err := decodeObject(result["item"].(Object), &item); err != nil {
		if raw, e := json.Marshal(result["item"]); e == nil && json.Unmarshal(raw, &item) == nil {
			return item
		}
		t.Fatal(err)
	}
	return item
}
