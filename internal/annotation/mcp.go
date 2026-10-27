package annotation

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
)

// serveMCP implements the stateless JSON response mode of Streamable HTTP for
// MCP 2025-11-25 (and its compatible 2025-03-26 / 2025-06-18 predecessors).
// Authentication and Origin validation run in the shared HTTP middleware.
func (s *Service) serveMCP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", "POST")
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	version := r.Header.Get("MCP-Protocol-Version")
	if version != "" && version != "2025-11-25" && version != "2025-06-18" && version != "2025-03-26" {
		writeJSON(w, http.StatusBadRequest, Object{"error": "unsupported MCP protocol version"})
		return
	}
	if !strings.Contains(r.Header.Get("Content-Type"), "application/json") {
		w.WriteHeader(http.StatusUnsupportedMediaType)
		return
	}
	accept := r.Header.Get("Accept")
	if !strings.Contains(accept, "application/json") || !strings.Contains(accept, "text/event-stream") {
		w.WriteHeader(http.StatusNotAcceptable)
		return
	}
	var req struct {
		JSONRPC string          `json:"jsonrpc"`
		ID      json.RawMessage `json:"id"`
		Method  string          `json:"method"`
		Params  json.RawMessage `json:"params"`
		Result  json.RawMessage `json:"result"`
		Error   json.RawMessage `json:"error"`
	}
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(&req); err != nil || dec.Decode(new(any)) != io.EOF {
		mcpError(w, nil, -32700, "Parse error")
		return
	}
	if req.JSONRPC != "2.0" {
		mcpError(w, req.ID, -32600, "Invalid Request")
		return
	}
	if req.Method == "" && (req.Result != nil || req.Error != nil) {
		w.WriteHeader(http.StatusAccepted)
		return
	}
	if req.Method == "" {
		mcpError(w, req.ID, -32600, "Invalid Request")
		return
	}
	if req.ID == nil {
		// Notifications have no response, including notifications/initialized and
		// notifications/cancelled. No long-lived work or session is retained.
		w.WriteHeader(http.StatusAccepted)
		return
	}
	var id any
	if json.Unmarshal(req.ID, &id) != nil {
		mcpError(w, nil, -32600, "Invalid Request")
		return
	}
	switch id.(type) {
	case string, float64:
	default:
		mcpError(w, nil, -32600, "Invalid Request")
		return
	}
	var result any
	switch req.Method {
	case "initialize":
		var params struct {
			ProtocolVersion string `json:"protocolVersion"`
		}
		if json.Unmarshal(req.Params, &params) != nil || params.ProtocolVersion == "" {
			mcpError(w, req.ID, -32602, "protocolVersion is required")
			return
		}
		selected := "2025-11-25"
		if params.ProtocolVersion == "2025-06-18" || params.ProtocolVersion == "2025-03-26" {
			selected = params.ProtocolVersion
		}
		result = Object{"protocolVersion": selected, "capabilities": Object{"tools": Object{"listChanged": false}, "resources": Object{"subscribe": false, "listChanged": false}}, "serverInfo": Object{"name": "fieldwork-annotations", "version": "1.2.0"}, "instructions": "Authenticated annotation workspace. Start with get_info and list_projects. Project and item mentions use fieldwork://project/{id} and fieldwork://item/{id}; read their current JSON through resources/read. Writes require expectedRevision: 0 creates; updates use the current document revision. Annotation edits use the parent item's revision. Reload and reconcile on conflict. For incremental sync, call list_changes with a fixed projectId and cursor '0', apply creates/updates/deletes, and save nextCursor only with the applied data; continue while hasMore. includeDocuments:false gives compact change metadata for inspection, not a complete mirror. Use the annotate sync CLI for bulk document sync and annotation-migrate pull for protected source assets; avoid sending asset bytes through MCP."}
	case "ping":
		result = Object{}
	case "tools/list":
		result = Object{"tools": mcpTools()}
	case "resources/list":
		// Dynamic documents are discoverable through mention search and templates.
		result = Object{"resources": []Object{}}
	case "resources/templates/list":
		result = Object{"resourceTemplates": mcpResourceTemplates()}
	case "resources/read":
		var params struct {
			URI string `json:"uri"`
		}
		if json.Unmarshal(req.Params, &params) != nil || params.URI == "" {
			mcpError(w, req.ID, -32602, "uri is required")
			return
		}
		value, err := s.readMCPResource(r.Context(), params.URI)
		if err != nil {
			code, message := -32603, connectError(err).Error()
			if errors.Is(err, ErrInvalid) {
				code = -32602
			} else if errors.Is(err, ErrNotFound) {
				code, message = -32002, "Resource not found"
			}
			mcpError(w, req.ID, code, message)
			return
		}
		result = value
	case "tools/call":
		var params struct {
			Name      string `json:"name"`
			Arguments Object `json:"arguments"`
		}
		if json.Unmarshal(req.Params, &params) != nil {
			mcpError(w, req.ID, -32602, "Invalid tool parameters")
			return
		}
		if _, ok := mcpCatalog[params.Name]; !ok {
			mcpError(w, req.ID, -32602, "Unknown tool")
			return
		}
		value, err := s.callMCP(r.Context(), params.Name, params.Arguments)
		if err != nil {
			message := connectError(err).Error()
			result = Object{"content": []Object{{"type": "text", "text": message}}, "isError": true}
		} else {
			encoded, _ := json.Marshal(value)
			content := []Object{{"type": "text", "text": string(encoded)}}
			if params.Name == "search_mentions" {
				content = []Object{}
			}
			result = Object{"content": content, "structuredContent": value, "isError": false}
		}
	default:
		mcpError(w, req.ID, -32601, "Method not found")
		return
	}
	writeJSON(w, http.StatusOK, Object{"jsonrpc": "2.0", "id": req.ID, "result": result})
}

func mcpError(w http.ResponseWriter, id json.RawMessage, code int, message string) {
	writeJSON(w, http.StatusOK, Object{"jsonrpc": "2.0", "id": id, "error": Object{"code": code, "message": message}})
}
