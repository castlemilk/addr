package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// reviewServer is a loopback Fieldwork stand-in: items r1008-* live in
// mooroolbark-review, items addr-scene-* in mooroolbark; it records every
// request path.
func reviewServer(t *testing.T) (*httptest.Server, func() []string) {
	t.Helper()
	var mu sync.Mutex
	var paths []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		paths = append(paths, r.URL.Path)
		mu.Unlock()
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/api/v1/rpc/GetItem":
			id, _ := body["id"].(string)
			project := map[bool]string{true: "mooroolbark-review", false: "mooroolbark"}[strings.HasPrefix(id, "r1008-")]
			if !strings.HasPrefix(id, "r1008-") && !strings.HasPrefix(id, "addr-scene-") {
				w.WriteHeader(http.StatusNotFound)
				_, _ = w.Write([]byte(`{"code":"not_found","message":"item not found"}`))
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"item": map[string]any{"id": id, "projectId": project, "revision": 2}})
		case "/api/v1/rpc/ListProjects":
			_, _ = w.Write([]byte(`{"projects":[{"id":"mooroolbark"},{"id":"mooroolbark-review"}],"nextCursor":""}`))
		case "/api/v1/rpc/ListLabels":
			if body["projectId"] == "mooroolbark-review" {
				_, _ = w.Write([]byte(`{"labels":[{"id":"mooroolbark-review-proposed","projectId":"mooroolbark-review"}],"nextCursor":""}`))
				return
			}
			_, _ = w.Write([]byte(`{"labels":[],"nextCursor":""}`))
		case "/mcp":
			_, _ = w.Write([]byte(`{"jsonrpc":"2.0","id":0,"result":{"content":[]}}`))
		default:
			_, _ = w.Write([]byte(`{}`))
		}
	}))
	t.Cleanup(server.Close)
	return server, func() []string {
		mu.Lock()
		defer mu.Unlock()
		return append([]string(nil), paths...)
	}
}

func hit(paths []string, path string) bool {
	for _, p := range paths {
		if p == path {
			return true
		}
	}
	return false
}

func TestAnnotateRefusesEveryWriteToAReviewProject(t *testing.T) {
	server, paths := reviewServer(t)
	refused := []struct {
		args  []string
		input string
		never string
	}{
		{[]string{"save-item"}, `{"id":"r1008-1-shadow-002","projectId":"mooroolbark-review","title":"t"}`, "/api/v1/rpc/SaveItem"},
		{[]string{"save-item", "--revision", "3"}, `{"id":"r1008-1-shadow-002","projectId":"mooroolbark-review","title":"t"}`, "/api/v1/rpc/SaveItem"},
		{[]string{"save-label"}, `{"id":"mooroolbark-review-x","projectId":"mooroolbark-review","name":"x","color":"#fff"}`, "/api/v1/rpc/SaveLabel"},
		{[]string{"save-project"}, `{"id":"mooroolbark-review","name":"x"}`, "/api/v1/rpc/SaveProject"},
		{[]string{"delete-item", "--id", "r1008-1-shadow-002", "--revision", "2"}, ``, "/api/v1/rpc/DeleteItem"},
		{[]string{"delete-label", "--id", "mooroolbark-review-proposed", "--revision", "1"}, ``, "/api/v1/rpc/DeleteLabel"},
		{[]string{"rpc", "SaveAnnotation"}, `{"itemId":"r1008-1-shadow-002","annotation":{"id":"new-x"},"expectedRevision":2}`, "/api/v1/rpc/SaveAnnotation"},
		{[]string{"rpc", "DeleteAnnotation"}, `{"itemId":"r1008-1-shadow-002","id":"new-x","expectedRevision":2}`, "/api/v1/rpc/DeleteAnnotation"},
		{[]string{"import"}, `{"schemaVersion":1,"project":{"id":"mooroolbark-review","name":"x"},"labels":[],"items":[]}`, "/api/v1/rpc/SaveProject"},
		{[]string{"push", "--project", "mooroolbark-review", "--dir", t.TempDir()}, ``, "/api/v1/rpc/ListChanges"},
	}
	for _, c := range refused {
		var output, diagnostics bytes.Buffer
		args := append(append([]string{}, c.args...), "--url", server.URL)
		err := run(context.Background(), args, strings.NewReader(c.input), &output, &diagnostics)
		if err == nil || !strings.Contains(err.Error(), "agents never edit a review project") {
			t.Fatalf("%v: want a review-project refusal, got %v", c.args, err)
		}
		if hit(paths(), c.never) {
			t.Fatalf("%v: the refused write reached %s", c.args, c.never)
		}
	}
	allowed := []struct {
		args  []string
		input string
		want  string
	}{
		{[]string{"save-item"}, `{"id":"addr-scene-x","projectId":"mooroolbark","title":"t"}`, "/api/v1/rpc/SaveItem"},
		{[]string{"delete-item", "--id", "addr-scene-x", "--revision", "2"}, ``, "/api/v1/rpc/DeleteItem"},
		{[]string{"rpc", "SaveAnnotation"}, `{"itemId":"addr-scene-x","annotation":{"id":"a"},"expectedRevision":2}`, "/api/v1/rpc/SaveAnnotation"},
		{[]string{"delete-label", "--id", "mooroolbark-building", "--revision", "1"}, ``, "/api/v1/rpc/DeleteLabel"},
		{[]string{"get", "--id", "r1008-1-shadow-002"}, ``, "/api/v1/rpc/GetItem"},
		{[]string{"rpc", "SaveAgentReview"}, `{"itemId":"r1008-1-shadow-002","expectedRevision":2,"reviewer":"codex","decision":"uncertain","notes":"Occluded edge"}`, "/api/v1/rpc/SaveAgentReview"},
	}
	for _, c := range allowed {
		var output, diagnostics bytes.Buffer
		args := append(append([]string{}, c.args...), "--url", server.URL)
		if err := run(context.Background(), args, strings.NewReader(c.input), &output, &diagnostics); err != nil {
			t.Fatalf("%v: other projects and reads stay allowed, got %v", c.args, err)
		}
		if !hit(paths(), c.want) {
			t.Fatalf("%v: want a request to %s", c.args, c.want)
		}
	}
}

func TestMCPBridgeRefusesReviewProjectWriteTools(t *testing.T) {
	server, paths := reviewServer(t)
	lines := []string{
		`{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"save_item","arguments":{"item":{"id":"r1008-x","projectId":"mooroolbark-review","title":"t","adapter":"addr-scene","source":{}},"expectedRevision":2}}}`,
		`{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"save_annotation","arguments":{"itemId":"r1008-1-shadow-002","annotation":{"id":"new-x"},"expectedRevision":2}}}`,
		`{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"delete_item","arguments":{"id":"r1008-1-shadow-002","expectedRevision":2}}}`,
		`{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_item","arguments":{"id":"r1008-1-shadow-002"}}}`,
		`{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"save_item","arguments":{"item":{"id":"addr-scene-x","projectId":"mooroolbark","title":"t","adapter":"addr-scene","source":{}},"expectedRevision":2}}}`,
	}
	var output, diagnostics bytes.Buffer
	if err := run(context.Background(), []string{"mcp", "--url", server.URL}, strings.NewReader(strings.Join(lines, "\n")+"\n"), &output, &diagnostics); err != nil {
		t.Fatal(err)
	}
	responses := strings.Split(strings.TrimSpace(output.String()), "\n")
	if len(responses) != 5 {
		t.Fatalf("want 5 responses, got %d: %s", len(responses), output.String())
	}
	for _, response := range responses[:3] {
		var message struct {
			Result struct {
				IsError bool `json:"isError"`
				Content []struct {
					Text string `json:"text"`
				} `json:"content"`
			} `json:"result"`
		}
		if err := json.Unmarshal([]byte(response), &message); err != nil || !message.Result.IsError || len(message.Result.Content) == 0 ||
			!strings.Contains(message.Result.Content[0].Text, "agents never edit a review project") {
			t.Fatalf("want a tool error refusing the review-project write, got %s", response)
		}
	}
	forwarded := 0
	for _, p := range paths() {
		if p == "/mcp" {
			forwarded++
		}
	}
	if forwarded != 2 {
		t.Fatalf("want only get_item and the mooroolbark save forwarded, got %d /mcp requests", forwarded)
	}
}

func TestMCPBridgeRefusesABatchHoldingAReviewProjectWrite(t *testing.T) {
	server, paths := reviewServer(t)
	read := `{"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"get_item","arguments":{"id":"r1008-1-shadow-002"}}}`
	write := `{"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"save_annotation","arguments":{"itemId":"r1008-1-shadow-002","annotation":{"id":"new-x"},"expectedRevision":2}}}`
	notifyWrite := `{"jsonrpc":"2.0","method":"tools/call","params":{"name":"delete_item","arguments":{"id":"r1008-1-shadow-002","expectedRevision":2}}}`
	otherProject := `{"jsonrpc":"2.0","id":13,"method":"tools/call","params":{"name":"save_annotation","arguments":{"itemId":"addr-scene-x","annotation":{"id":"a"},"expectedRevision":2}}}`
	lines := []string{
		"[" + read + "," + write + "]",                          // refused whole: one tool error per request id, as an array
		"[" + read + ",[" + write + "]]",                        // a nested array is checked too
		"[" + notifyWrite + "]",                                 // only a notification: refused, nothing to answer
		"[" + read + "," + otherProject + "]",                   // no review-project write: forwarded unchanged
		` [ {"jsonrpc":"2.0","id":14,"method":"tools/list"} ] `, // read-only batch with spaces: forwarded
	}
	var output, diagnostics bytes.Buffer
	if err := run(context.Background(), []string{"mcp", "--url", server.URL}, strings.NewReader(strings.Join(lines, "\n")+"\n"), &output, &diagnostics); err != nil {
		t.Fatal(err)
	}
	responses := strings.Split(strings.TrimSpace(output.String()), "\n")
	if len(responses) != 4 {
		t.Fatalf("want 2 refusal arrays and 2 forwarded answers, got %d: %s", len(responses), output.String())
	}
	for i, want := range [][]string{{"11", "12"}, {"11", "12"}} {
		var refused []struct {
			ID     json.RawMessage `json:"id"`
			Result struct {
				IsError bool `json:"isError"`
				Content []struct {
					Text string `json:"text"`
				} `json:"content"`
			} `json:"result"`
		}
		if err := json.Unmarshal([]byte(responses[i]), &refused); err != nil || len(refused) != len(want) {
			t.Fatalf("line %d: want an array of %d tool errors, got %s", i+1, len(want), responses[i])
		}
		for j, r := range refused {
			if string(r.ID) != want[j] || !r.Result.IsError || len(r.Result.Content) == 0 ||
				!strings.Contains(r.Result.Content[0].Text, "refusing the whole MCP batch") ||
				!strings.Contains(r.Result.Content[0].Text, "agents never edit a review project") {
				t.Fatalf("line %d: want a whole-batch refusal for id %s, got %s", i+1, want[j], responses[i])
			}
		}
	}
	forwarded := 0
	for _, p := range paths() {
		if p == "/mcp" {
			forwarded++
		}
	}
	if forwarded != 2 {
		t.Fatalf("want only the two batches without a review-project write forwarded, got %d /mcp requests", forwarded)
	}
	if strings.Count(diagnostics.String(), "annotate mcp: refusing the whole MCP batch") != 3 {
		t.Fatalf("want 3 batch refusals on stderr, got %q", diagnostics.String())
	}
}

// Spelling never bypasses the guard: a review project id in any case, with
// surrounding whitespace or a fullwidth hyphen, a key the server's decoder
// matches case-insensitively, and an update that would move a stored review
// item or label to another project are all refused before the write is sent.
func TestAnnotateRefusesEverySpellingOfAReviewWrite(t *testing.T) {
	server, paths := reviewServer(t)
	refused := []struct {
		args  []string
		input string
	}{
		{[]string{"save-item"}, `{"id":"r1008-x","projectId":"MOOROOLBARK-REVIEW","title":"t"}`},
		{[]string{"save-item"}, `{"id":"r1008-x","projectId":"mooroolbark-review ","title":"t"}`},
		{[]string{"save-item"}, `{"id":"r1008-x","projectId":"mooroolbark－review","title":"t"}`},
		{[]string{"save-item"}, `{"id":"r1008-x","projectId":"mooroolbark","ProjectID":"Mooroolbark-Review","title":"t"}`},
		{[]string{"save-label"}, `{"id":"l","PROJECTID":"mooroolbark-review","name":"x","color":"#ffffff"}`},
		{[]string{"save-project"}, `{"id":"Mooroolbark-Review","name":"x"}`},
		{[]string{"rpc", "SaveItem"}, `{"Item":{"id":"r1008-x","projectId":"x-REVIEW\t"},"expectedRevision":0}`},
		{[]string{"rpc", "SaveAnnotation"}, `{"ItemId":"r1008-1-shadow-002","itemId":"addr-scene-x","annotation":{"id":"a"},"expectedRevision":2}`},
		{[]string{"rpc", "DeleteItem"}, `{"ID":"r1008-1-shadow-002","expectedRevision":2}`},
		// An update naming another project still replaces the stored review item (or label): looked up and refused.
		{[]string{"save-item", "--revision", "3"}, `{"id":"r1008-1-shadow-002","projectId":"mooroolbark","title":"t"}`},
		{[]string{"rpc", "SaveItem"}, `{"item":{"ID":"r1008-1-shadow-002","projectId":"mooroolbark"},"expectedRevision":3}`},
		{[]string{"save-label", "--revision", "2"}, `{"id":"mooroolbark-review-proposed","projectId":"mooroolbark","name":"x","color":"#ffffff"}`},
		{[]string{"push", "--project", "Mooroolbark-Review", "--dir", t.TempDir()}, ``},
	}
	for _, c := range refused {
		var output, diagnostics bytes.Buffer
		args := append(append([]string{}, c.args...), "--url", server.URL)
		err := run(context.Background(), args, strings.NewReader(c.input), &output, &diagnostics)
		if err == nil || !strings.Contains(err.Error(), "agents never edit a review project") {
			t.Fatalf("%v %s: want a review-project refusal, got %v", c.args, c.input, err)
		}
	}
	for _, p := range paths() {
		if strings.HasPrefix(p, "/api/v1/rpc/Save") || strings.HasPrefix(p, "/api/v1/rpc/Delete") || p == "/api/v1/rpc/ListChanges" {
			t.Fatalf("a refused write reached %s", p)
		}
	}
	// A create (revision 0) needs no lookup: the server refuses a create over an existing id. An update of an item stored
	// in another project is looked up and allowed.
	for _, c := range []struct {
		args  []string
		input string
	}{
		{[]string{"save-item"}, `{"id":"addr-scene-new","projectId":"mooroolbark","title":"t"}`},
		{[]string{"save-item", "--revision", "4"}, `{"id":"addr-scene-x","projectId":"mooroolbark","title":"t"}`},
	} {
		var output, diagnostics bytes.Buffer
		if err := run(context.Background(), append(append([]string{}, c.args...), "--url", server.URL), strings.NewReader(c.input), &output, &diagnostics); err != nil {
			t.Fatalf("%v: want it allowed, got %v", c.args, err)
		}
	}
	if !hit(paths(), "/api/v1/rpc/SaveItem") || !hit(paths(), "/api/v1/rpc/GetItem") {
		t.Fatalf("want the allowed saves sent after an item lookup, got %v", paths())
	}
}

func TestMCPBridgeRefusesSpelledReviewWrites(t *testing.T) {
	server, paths := reviewServer(t)
	lines := []string{
		`{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"save_item","arguments":{"item":{"id":"r1008-x","ProjectId":"MOOROOLBARK-REVIEW","title":"t"},"expectedRevision":0}}}`,
		`{"jsonrpc":"2.0","id":2,"Method":"tools/call","Params":{"Name":"save_annotation","Arguments":{"itemId":"r1008-1-shadow-002","annotation":{"id":"a"},"expectedRevision":2}}}`,
		`{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"save_label","arguments":{"label":{"id":"l","projectId":"x－review "},"expectedRevision":0}}}`,
		`{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"save_item","arguments":{"item":{"id":"r1008-1-shadow-002","projectId":"mooroolbark"},"expectedRevision":2}}}`,
	}
	var output, diagnostics bytes.Buffer
	if err := run(context.Background(), []string{"mcp", "--url", server.URL}, strings.NewReader(strings.Join(lines, "\n")+"\n"), &output, &diagnostics); err != nil {
		t.Fatal(err)
	}
	responses := strings.Split(strings.TrimSpace(output.String()), "\n")
	if len(responses) != len(lines) {
		t.Fatalf("want %d responses, got %s", len(lines), output.String())
	}
	for _, response := range responses {
		if !strings.Contains(response, "agents never edit a review project") || !strings.Contains(response, `"isError":true`) {
			t.Fatalf("want a tool error refusing the review-project write, got %s", response)
		}
	}
	if hit(paths(), "/mcp") {
		t.Fatal("a refused write was forwarded to /mcp")
	}
}

// The server merges every key that folds to "arguments" into one map (and a
// repeated params key keeps the last copy); the bridge reads the line the same
// way, so neither a repeated nor a respelled arguments key hides a write.
func TestMCPBridgeReadsRepeatedArgumentsTheWayTheServerDoes(t *testing.T) {
	server, paths := reviewServer(t)
	write := `{"item":{"id":"r1008-x","projectId":"mooroolbark-review","title":"t"},"expectedRevision":0}`
	lines := []string{
		`{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"save_item","arguments":` + write + `,"arguments":{}}}`,
		`{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"save_item","arguments":` + write + `,"ARGUMENTS":{}}}`,
		`{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"save_annotation","arguments":{"itemId":"r1008-1-shadow-002","annotation":{"id":"a"},"expectedRevision":2},"Arguments":{}}}`,
		`{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_item","arguments":{}},"params":{"name":"save_item","arguments":` + write + `}}`,
	}
	var output, diagnostics bytes.Buffer
	if err := run(context.Background(), []string{"mcp", "--url", server.URL}, strings.NewReader(strings.Join(lines, "\n")+"\n"), &output, &diagnostics); err != nil {
		t.Fatal(err)
	}
	responses := strings.Split(strings.TrimSpace(output.String()), "\n")
	if len(responses) != len(lines) {
		t.Fatalf("want %d responses, got %s", len(lines), output.String())
	}
	for i, response := range responses {
		if !strings.Contains(response, "agents never edit a review project") || !strings.Contains(response, `"isError":true`) {
			t.Fatalf("line %d: want a tool error refusing the review-project write, got %s", i+1, response)
		}
	}
	if hit(paths(), "/mcp") {
		t.Fatal("a refused write was forwarded to /mcp")
	}
}
