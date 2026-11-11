package annotation

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
)

func callMCPTest(t *testing.T, server *httptest.Server, name string, arguments Object) (Object, bool) {
	t.Helper()
	body, _ := json.Marshal(Object{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": Object{"name": name, "arguments": arguments}})
	status, data, _ := request(t, server, http.MethodPost, "/mcp", string(body), testToken, "")
	var response struct {
		Result Object `json:"result"`
	}
	if status != 200 || json.Unmarshal(data, &response) != nil || response.Result == nil {
		t.Fatalf("MCP %s: %d %s", name, status, data)
	}
	failed, _ := response.Result["isError"].(bool)
	return response.Result, failed
}

func TestMCPDiscoveryAndRevisionCheckedMutations(t *testing.T) {
	server := httpTestServer(t)
	status, data, _ := request(t, server, http.MethodPost, "/mcp", `{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}`, testToken, "")
	var discovery struct {
		Result struct {
			Tools []struct {
				Name        string `json:"name"`
				InputSchema struct {
					Required []string `json:"required"`
				} `json:"inputSchema"`
				Annotations struct {
					ReadOnly    bool `json:"readOnlyHint"`
					Destructive bool `json:"destructiveHint"`
				} `json:"annotations"`
			} `json:"tools"`
		} `json:"result"`
	}
	if status != 200 || json.Unmarshal(data, &discovery) != nil || len(discovery.Result.Tools) != 16 {
		t.Fatalf("tool discovery: %d %s", status, data)
	}
	for _, tool := range discovery.Result.Tools {
		if tool.Name == "save_agent_review" {
			if tool.Annotations.ReadOnly || tool.Annotations.Destructive || !slices.Contains(tool.InputSchema.Required, "expectedRevision") {
				t.Fatalf("unsafe append-only review metadata: %+v", tool)
			}
			continue
		}
		if strings.HasPrefix(tool.Name, "save_") || strings.HasPrefix(tool.Name, "delete_") {
			if tool.Annotations.ReadOnly || !tool.Annotations.Destructive || !slices.Contains(tool.InputSchema.Required, "expectedRevision") {
				t.Fatalf("unsafe mutation discovery metadata: %+v", tool)
			}
		}
	}
	if !strings.Contains(string(data), `"LineString"`) {
		t.Fatal("MCP schema omits a supported geometry type")
	}
	call := func(name string, arguments Object) Object {
		t.Helper()
		result, failed := callMCPTest(t, server, name, arguments)
		if failed {
			t.Fatalf("%s failed: %v", name, result)
		}
		return result["structuredContent"].(map[string]any)
	}
	if call("get_info", Object{})["name"] != "Fieldwork" {
		t.Fatal("get_info did not reach the shared service")
	}
	if _, failed := callMCPTest(t, server, "save_project", Object{"project": Object{"id": "no-revision", "name": "Must not create"}}); !failed {
		t.Fatal("MCP mutation accepted omitted expectedRevision")
	}
	call("save_project", Object{"project": Object{"id": "project", "name": "MCP project"}, "expectedRevision": 0})
	if _, failed := callMCPTest(t, server, "save_project", Object{"project": Object{"id": "project", "name": "Stale write"}, "expectedRevision": 0}); !failed {
		t.Fatal("MCP mutation ignored revision conflict")
	}
	call("save_label", Object{"label": Object{"id": "label", "projectId": "project", "name": "Roof", "color": "#123456"}, "expectedRevision": 0})
	if len(call("list_labels", Object{"projectId": "project"})["labels"].([]any)) != 1 {
		t.Fatal("list_labels missing new label")
	}
	call("save_item", Object{"item": Object{"id": "item", "projectId": "project", "title": "Image", "adapter": "image", "source": Object{"url": "/test.svg"}, "notes": "Keep these notes"}, "expectedRevision": 0})
	annotation := Object{"id": "line", "labelId": "label", "geometry": Object{"type": "LineString", "coordinates": [][]float64{{0.1, 0.2}, {0.7, 0.8}}}, "properties": Object{"custom": "retained"}}
	saved := call("save_annotation", Object{"itemId": "item", "annotation": annotation, "expectedRevision": 1})["item"].(map[string]any)
	if saved["revision"] != float64(2) || saved["notes"] != "Keep these notes" || len(saved["annotations"].([]any)) != 1 {
		t.Fatalf("annotation edit did not preserve parent: %v", saved)
	}
	for _, name := range []string{"save_annotation", "delete_annotation"} {
		arguments := Object{"itemId": "item", "expectedRevision": 1}
		if name == "save_annotation" {
			arguments["annotation"] = annotation
		} else {
			arguments["id"] = "line"
		}
		result, failed := callMCPTest(t, server, name, arguments)
		encoded, _ := json.Marshal(result)
		if !failed || !strings.Contains(string(encoded), "revision conflict") {
			t.Fatalf("%s did not report parent revision conflict: %s", name, encoded)
		}
	}
	deleted := call("delete_annotation", Object{"itemId": "item", "id": "line", "expectedRevision": 2})["item"].(map[string]any)
	if deleted["revision"] != float64(3) || len(deleted["annotations"].([]any)) != 0 {
		t.Fatalf("annotation delete failed: %v", deleted)
	}
	full := call("list_changes", Object{"projectId": "project", "cursor": "0", "limit": 100})
	compact := call("list_changes", Object{"projectId": "project", "cursor": "0", "limit": 100, "includeDocuments": false})
	if full["nextCursor"] != compact["nextCursor"] || len(full["changes"].([]any)) != 5 {
		t.Fatalf("compact feed changed the checkpoint: %v", compact)
	}
	if _, exists := full["changes"].([]any)[0].(map[string]any)["document"]; !exists {
		t.Fatal("default list_changes lost document payloads")
	}
	for _, raw := range compact["changes"].([]any) {
		if _, exists := raw.(map[string]any)["document"]; exists {
			t.Fatal("compact list_changes included a document")
		}
	}
	// Omitted filters/cursor retain compatibility with existing MCP clients.
	if call("list_changes", Object{})["nextCursor"] != full["nextCursor"] {
		t.Fatal("default global feed changed")
	}
}
