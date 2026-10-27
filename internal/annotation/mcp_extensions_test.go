package annotation

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"slices"
	"strings"
	"testing"
)

func requestMCPMethod(t *testing.T, server *httptest.Server, method string, params Object) Object {
	t.Helper()
	body, _ := json.Marshal(Object{"jsonrpc": "2.0", "id": "extensions", "method": method, "params": params})
	status, data, _ := request(t, server, http.MethodPost, "/mcp", string(body), testToken, "")
	var response Object
	if status != http.StatusOK || json.Unmarshal(data, &response) != nil {
		t.Fatalf("%s: %d %s", method, status, data)
	}
	return response
}

func TestMCPMentionDiscovery(t *testing.T) {
	server := httpTestServer(t)
	response := requestMCPMethod(t, server, "tools/list", Object{})
	tools := response["result"].(map[string]any)["tools"].([]any)
	var mention Object
	for _, raw := range tools {
		tool := raw.(map[string]any)
		if title, ok := tool["title"].(string); !ok || title == "" {
			t.Fatalf("tool has no display title: %v", tool)
		}
		if tool["name"] == "search_mentions" {
			mention = tool
		}
	}
	if mention == nil {
		t.Fatal("mention tool not discoverable")
	}
	metadata := mention["_meta"].(map[string]any)
	if !reflect.DeepEqual(metadata, Object{"openai/extensions": Object{"mentions/search": Object{}}, "ui": Object{"visibility": []any{"app"}}}) {
		t.Fatalf("OpenAI mention metadata: %v", metadata)
	}
	schema := mention["inputSchema"].(map[string]any)
	properties := schema["properties"].(map[string]any)
	if !reflect.DeepEqual(schema["required"], []any{"query"}) || len(properties) != 1 || properties["query"].(map[string]any)["type"] != "string" {
		t.Fatalf("mention input schema: %v", schema)
	}
	output := mention["outputSchema"].(map[string]any)
	items := output["properties"].(map[string]any)["items"].(map[string]any)
	link := items["items"].(map[string]any)
	if items["maxItems"] != float64(mcpMentionLimit) || link["properties"].(map[string]any)["type"].(map[string]any)["const"] != "resource_link" {
		t.Fatalf("mention output schema: %v", output)
	}
	annotations := mention["annotations"].(map[string]any)
	if annotations["readOnlyHint"] != true || annotations["destructiveHint"] != false {
		t.Fatalf("mention search is not advertised as read only: %v", annotations)
	}
	for _, version := range []string{"2025-03-26", "2025-06-18", "2025-11-25"} {
		initialized := requestMCPMethod(t, server, "initialize", Object{"protocolVersion": version})["result"].(map[string]any)
		capabilities := initialized["capabilities"].(map[string]any)
		if initialized["protocolVersion"] != version || capabilities["resources"] == nil || capabilities["extensions"] != nil || capabilities["elicitation"] != nil {
			t.Fatalf("invalid supported capabilities for %s: %v", version, initialized)
		}
	}
	templates := requestMCPMethod(t, server, "resources/templates/list", Object{})["result"].(map[string]any)["resourceTemplates"].([]any)
	if len(templates) != 2 || templates[0].(map[string]any)["uriTemplate"] != "fieldwork://project/{id}" || templates[1].(map[string]any)["uriTemplate"] != "fieldwork://item/{id}" {
		t.Fatalf("resource templates: %v", templates)
	}
	listed := requestMCPMethod(t, server, "resources/list", Object{})["result"].(map[string]any)["resources"].([]any)
	if len(listed) != 0 {
		t.Fatalf("dynamic resources should be discovered through search: %v", listed)
	}
	if error := requestMCPMethod(t, server, "server/discover", Object{})["error"].(map[string]any); error["code"] != float64(-32601) {
		t.Fatalf("unsupported discovery should not be advertised: %v", error)
	}
}

func TestMCPMentionsAndResourceLifecycle(t *testing.T) {
	server := httpTestServer(t)
	for _, project := range []Object{{"id": "alpha", "name": "Survey"}, {"id": "omega", "name": "Roof work"}} {
		if _, failed := callMCPTest(t, server, "save_project", Object{"project": project, "expectedRevision": 0}); failed {
			t.Fatal("create fixture project")
		}
	}
	item := Object{"id": "roof", "projectId": "alpha", "title": "Roof sample", "adapter": "image", "source": Object{"url": "/api/v1/assets/private.svg"}, "notes": "Review roof boundary"}
	if _, failed := callMCPTest(t, server, "save_item", Object{"item": item, "expectedRevision": 0}); failed {
		t.Fatal("create fixture item")
	}
	before, _ := callMCPTest(t, server, "list_changes", Object{})
	search := func(query string) []any {
		t.Helper()
		result, failed := callMCPTest(t, server, "search_mentions", Object{"query": query})
		if failed || len(result["content"].([]any)) != 0 {
			t.Fatalf("mention search %q: %v", query, result)
		}
		return result["structuredContent"].(map[string]any)["items"].([]any)
	}
	links := search("ROOF")
	if len(links) != 2 || links[0].(map[string]any)["uri"] != "fieldwork://project/omega" || links[1].(map[string]any)["uri"] != "fieldwork://item/roof" {
		t.Fatalf("case-insensitive name/title matches: %v", links)
	}
	if !reflect.DeepEqual(links, search("ROOF")) {
		t.Fatal("search order is not deterministic")
	}
	for _, raw := range links {
		link := raw.(map[string]any)
		if link["type"] != "resource_link" || link["name"] == "" || link["title"] == "" || link["mimeType"] != "application/json" || len(link) != 6 {
			t.Fatalf("invalid resource link result: %v", link)
		}
		encoded, _ := json.Marshal(link)
		if strings.Contains(string(encoded), testToken) || strings.Contains(string(encoded), "private.svg") || link["source"] != nil {
			t.Fatalf("mention result contains source or credentials: %s", encoded)
		}
	}
	if len(search("")) != 3 || len(search("%")) != 0 || len(search("missing")) != 0 {
		t.Fatal("empty query or literal substring behavior is incorrect")
	}
	read := func(uri string) Object {
		t.Helper()
		response := requestMCPMethod(t, server, "resources/read", Object{"uri": uri})
		if response["error"] != nil {
			t.Fatalf("read %s: %v", uri, response)
		}
		contents := response["result"].(map[string]any)["contents"].([]any)
		if len(contents) != 1 {
			t.Fatalf("read contents: %v", contents)
		}
		content := contents[0].(map[string]any)
		var value Object
		if content["uri"] != uri || content["mimeType"] != "application/json" || content["blob"] != nil || json.Unmarshal([]byte(content["text"].(string)), &value) != nil {
			t.Fatalf("read resource representation: %v", content)
		}
		return value
	}
	project := read("fieldwork://project/omega")["project"].(map[string]any)
	if project["id"] != "omega" || project["name"] != "Roof work" || project["revision"] != float64(1) {
		t.Fatalf("project resource: %v", project)
	}
	got := read("fieldwork://item/roof")
	if got["item"].(map[string]any)["notes"] != item["notes"] || got["labels"] == nil {
		t.Fatalf("item resource is not the shared get_item response: %v", got)
	}
	after, _ := callMCPTest(t, server, "list_changes", Object{})
	if !reflect.DeepEqual(before["structuredContent"], after["structuredContent"]) {
		t.Fatal("search or resources/read changed workspace documents")
	}
	item["title"] = "Roof sample updated"
	if _, failed := callMCPTest(t, server, "save_item", Object{"item": item, "expectedRevision": 1}); failed {
		t.Fatal("update fixture item")
	}
	updated := read("fieldwork://item/roof")["item"].(map[string]any)
	if updated["revision"] != float64(2) || updated["title"] != item["title"] {
		t.Fatalf("stable URI did not return the current document: %v", updated)
	}
	if _, failed := callMCPTest(t, server, "delete_item", Object{"id": "roof", "expectedRevision": 2}); failed {
		t.Fatal("delete fixture item")
	}
	response := requestMCPMethod(t, server, "resources/read", Object{"uri": "fieldwork://item/roof"})
	if response["error"].(map[string]any)["code"] != float64(-32002) {
		t.Fatalf("deleted resource did not report not found: %v", response)
	}
	if len(search("ROOF")) != 1 {
		t.Fatal("deleted item remains in mention search")
	}
}

func TestMCPMentionAndResourceValidationAndAuth(t *testing.T) {
	server := httpTestServer(t)
	for _, arguments := range []Object{nil, {}, {"query": nil}, {"query": 1}, {"query": false}, {"query": []any{}}, {"query": Object{}}, {"query": strings.Repeat("a", 301)}, {"query": "", "limit": 1}} {
		if _, failed := callMCPTest(t, server, "search_mentions", arguments); !failed {
			t.Fatalf("accepted invalid mention arguments: %v", arguments)
		}
	}
	invalidURIs := []string{"", "https://example.com/project/test", "fieldwork://label/test", "fieldwork://project/../test", "fieldwork://item/test/extra", "fieldwork://item/%74est", "fieldwork://item/test?token=secret", "fieldwork://item/test#fragment", "fieldwork://user:secret@item/test", "fieldwork://item:80/test", "fieldwork://item/test?", "fieldwork://item/test#", "FIELDWORK://item/test"}
	for _, uri := range invalidURIs {
		response := requestMCPMethod(t, server, "resources/read", Object{"uri": uri})
		if response["error"].(map[string]any)["code"] != float64(-32602) {
			t.Fatalf("accepted invalid resource URI %q: %v", uri, response)
		}
	}
	for _, uri := range []string{"fieldwork://project/missing", "fieldwork://item/missing"} {
		response := requestMCPMethod(t, server, "resources/read", Object{"uri": uri})
		if response["error"].(map[string]any)["code"] != float64(-32002) {
			t.Fatalf("missing resource %q: %v", uri, response)
		}
	}
	for method, params := range map[string]Object{"tools/call": {"name": "search_mentions", "arguments": Object{"query": ""}}, "resources/read": {"uri": "fieldwork://project/missing"}, "resources/templates/list": {}, "resources/list": {}} {
		body, _ := json.Marshal(Object{"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
		for _, token := range []string{"", "wrong"} {
			status, _, _ := request(t, server, http.MethodPost, "/mcp", string(body), token, "")
			if status != http.StatusUnauthorized {
				t.Fatalf("%s accepted invalid auth: %d", method, status)
			}
		}
		status, _, _ := request(t, server, http.MethodPost, "/mcp", string(body), testToken, "https://evil.example")
		if status != http.StatusForbidden {
			t.Fatalf("%s accepted invalid origin: %d", method, status)
		}
	}
	service := &Service{Store: openTestStore(t)}
	if _, err := service.Call(context.Background(), "GetProject", Object{"id": "../project"}); err == nil {
		t.Fatal("GetProject accepted an invalid document ID")
	}
}

type mentionTrackingStore struct {
	Store
	calls []struct {
		kind string
		opts ListOptions
	}
}

func (s *mentionTrackingStore) List(ctx context.Context, kind string, opts ListOptions) ([]json.RawMessage, string, error) {
	s.calls = append(s.calls, struct {
		kind string
		opts ListOptions
	}{kind, opts})
	return s.Store.List(ctx, kind, opts)
}

func TestMCPMentionSearchIsBounded(t *testing.T) {
	store := &mentionTrackingStore{Store: openTestStore(t)}
	for i := 0; i < mcpMentionLimit+1; i++ {
		id := fmt.Sprintf("project-%02d", i)
		saveTest(t, store, "project", id, 0, Object{"name": "Project " + id})
		if i == mcpMentionLimit {
			saveTest(t, store, "item", "outside-scan", 0, Object{"projectId": id, "title": "needle", "adapter": "image", "source": Object{"url": "/asset.svg"}})
		}
	}
	service := &Service{Store: store}
	result, err := service.callMCP(context.Background(), "search_mentions", Object{"query": "needle"})
	if err != nil || len(result["items"].([]Object)) != 1 || result["items"].([]Object)[0]["uri"] != "fieldwork://item/outside-scan" || len(store.calls) != 2 {
		t.Fatalf("workspace-wide search must find later-project items with two bounded queries: %v %v calls=%d", result, err, len(store.calls))
	}
	for _, call := range store.calls {
		if call.opts.Limit != mcpMentionLimit || call.opts.Cursor != "" || call.opts.ProjectID != "" || !slices.Contains([]string{"project", "item"}, call.kind) {
			t.Fatalf("unbounded search request: %+v", call)
		}
	}
	store.calls = nil
	result, err = service.callMCP(context.Background(), "search_mentions", Object{"query": ""})
	if err != nil || len(result["items"].([]Object)) != mcpMentionLimit {
		t.Fatalf("result limit: %v %v", result, err)
	}
	if _, err := service.Call(context.Background(), "ListItems", Object{"query": "needle", "limit": mcpMentionLimit}); err == nil {
		t.Fatal("ordinary list_items lost its required project filter")
	}
}
