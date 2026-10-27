package migration

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/benebsworth/addr/internal/annotation"
)

// Agents never edit a review project: the migration client (annotate push,
// annotation-migrate, the training artefact publisher) refuses any write that
// names one, before a request is sent, and still reads it.
func TestClientRefusesWritesToReviewProjects(t *testing.T) {
	var hits atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{}`))
	}))
	defer server.Close()
	client, err := NewClient(server.URL, "")
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	refused := []struct {
		method string
		input  annotation.Object
	}{
		{"SaveItem", annotation.Object{"item": annotation.Object{"id": "r1008-x", "projectId": "mooroolbark-review"}, "expectedRevision": 0}},
		{"SaveItem", annotation.Object{"item": annotation.Object{"id": "r1008-x", "projectId": "mooroolbark-review"}, "expectedRevision": 3}},
		{"SaveLabel", annotation.Object{"label": annotation.Object{"id": "l", "projectId": "x-review"}, "expectedRevision": 0}},
		{"SaveProject", annotation.Object{"project": annotation.Object{"id": "mooroolbark-review"}, "expectedRevision": 0}},
		{"SaveItem", annotation.Object{"item": annotation.Object{"id": "r1008-x", "projectId": "Mooroolbark-Review "}, "expectedRevision": 0}},
		{"SaveItem", annotation.Object{"item": annotation.Object{"id": "r1008-x", "projectId": "mooroolbark", "ProjectID": "MOOROOLBARK-REVIEW"}, "expectedRevision": 0}},
		{"SaveLabel", annotation.Object{"Label": annotation.Object{"id": "l", "projectId": "mooroolbark\uff0dreview"}, "expectedRevision": 0}},
		{"SaveProject", annotation.Object{"project": annotation.Object{"ID": "mooroolbark-review\u200b"}, "expectedRevision": 0}},
	}
	for _, c := range refused {
		if _, err := client.Call(ctx, c.method, c.input); err == nil || !strings.Contains(err.Error(), "agents never edit a review project") {
			t.Fatalf("%s %v: want a review-project refusal, got %v", c.method, c.input, err)
		}
		if _, err := client.CallRaw(ctx, c.method, c.input); err == nil || !strings.Contains(err.Error(), "agents never edit a review project") {
			t.Fatalf("CallRaw %s: want a review-project refusal, got %v", c.method, err)
		}
	}
	if hits.Load() != 0 {
		t.Fatalf("a refused write reached the server (%d requests)", hits.Load())
	}
	if _, err := client.Call(ctx, "ListItems", annotation.Object{"projectId": "mooroolbark-review"}); err != nil {
		t.Fatalf("reads stay allowed: %v", err)
	}
	if _, err := client.Call(ctx, "SaveItem", annotation.Object{"item": annotation.Object{"id": "addr-scene-x", "projectId": "mooroolbark"}, "expectedRevision": 0}); err != nil {
		t.Fatalf("writes to other projects stay allowed: %v", err)
	}
	if hits.Load() != 2 {
		t.Fatalf("want 2 forwarded requests, got %d", hits.Load())
	}
}

func TestIsReviewProject(t *testing.T) {
	for id, want := range map[string]bool{"mooroolbark-review": true, "x-review": true, "mooroolbark": false, "review": false, "mooroolbark-reviews": false,
		// Spellings never bypass the guard: case, surrounding whitespace, fullwidth forms, other dashes, invisible characters.
		"MOOROOLBARK-REVIEW": true, "Mooroolbark-Review": true, "mooroolbark-review ": true, " \tmooroolbark-review\n": true,
		"mooroolbark\uff0dreview": true, "\uff4d\uff4f\uff4f\uff52\uff4f\uff4f\uff4c\uff42\uff41\uff52\uff4b\uff0d\uff52\uff45\uff56\uff49\uff45\uff57": true,
		"mooroolbark\u2010review": true, "mooroolbark\u2212review": true, "mooroolbark-review\u200b": true, "mooroolbark-rev\u00adiew": true,
		"mooroolbark-review\u00a0": true, "mooroolbark-review\u3000": true, "x-\u0280eview": false, "MOOROOLBARK": false} {
		if IsReviewProject(id) != want {
			t.Errorf("IsReviewProject(%q) = %v", id, !want)
		}
	}
}

func TestNormalizeProjectID(t *testing.T) {
	for in, want := range map[string]string{"mooroolbark-review": "mooroolbark-review", " Mooroolbark-REVIEW\t": "mooroolbark-review",
		"mooroolbark\uff0dreview": "mooroolbark-review", "\u212aelvin\u017fx": "kelvinsx", "a\ufeffb": "ab"} {
		if got := NormalizeProjectID(in); got != want {
			t.Errorf("NormalizeProjectID(%q) = %q, want %q", in, got, want)
		}
	}
}

// The server's encoding/json decoder matches keys case-insensitively, so a
// review project named under any key spelling is refused too.
func TestDeclaredProjectsReadsEverySpellingTheServerDecodes(t *testing.T) {
	cases := map[string][]string{
		`{"item":{"id":"r","ProjectID":"a-review"}}`:                  {"a-review"},
		`{"ITEM":{"projectid":"b-review"}}`:                           {"b-review"},
		`{"item":{"projectId":"mooroolbark","PROJECTID":"c-review"}}`: {"c-review"},
		`{"Project":{"Id":"d-review"}}`:                               {"d-review"},
		`{"label":{"pRoJeCtId":"e-review"},"ProjectId":"f-review"}`:   {"e-review", "f-review"},
		`{"item":{"project\u0049d":"g-review"}}`:                      {"g-review"}, // a JSON escape in the key
	}
	for body, want := range cases {
		got := strings.Join(DeclaredProjects([]byte(body)), ",")
		for _, project := range want {
			if !strings.Contains(got, project) {
				t.Errorf("DeclaredProjects(%s) = %q, want it to hold %s", body, got, project)
			}
		}
		if err := RefuseDeclaredReviewWrite("SaveItem", []byte(body)); err == nil {
			t.Errorf("RefuseDeclaredReviewWrite(%s): want a refusal", body)
		}
	}
	if got := FieldStrings([]byte(`{"item":{"id":"x","ID":"y","Id":7}}`), "item", "id"); strings.Join(got, ",") != "y,x" {
		t.Errorf("FieldStrings = %q, want every string candidate in key order", got)
	}
}
