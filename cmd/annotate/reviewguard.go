package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/benebsworth/addr/internal/annotation/migration"
)

// Agents append reviews through SaveAgentReview; ordinary edits to a review project remain refused.
// A review project (an id ending in -review, for example
// mooroolbark-review; see migration.ReviewProjectSuffix). annotate refuses
// ordinary writes there: save-*, delete-*, rpc, import and push, and the write
// tools a connected client sends through the annotate mcp bridge (a JSON-RPC
// batch holding one is refused whole; read-only batches are forwarded). The
// deployed Workers apply the same rule to their MCP routes
// (deploy/annotation/src/mcp-guard.ts). SaveAgentReview appends server-stamped
// agent history without editing human fields. scripts/ml/fieldwork_review.py
// push also creates the initial review documents, outside annotate.

// mcpToolMethods maps the server's MCP write tools to their RPC methods.
var mcpToolMethods = map[string]string{
	"save_agent_review": "SaveAgentReview",
	"save_project":      "SaveProject", "save_label": "SaveLabel", "save_item": "SaveItem",
	"save_annotation": "SaveAnnotation", "delete_annotation": "DeleteAnnotation",
	"delete_item": "DeleteItem", "delete_label": "DeleteLabel",
}

var mcpReadTools = map[string]bool{
	"get_info": true, "list_projects": true, "list_items": true, "get_item": true, "list_labels": true,
	"list_changes": true, "export_project": true, "search_mentions": true,
}

// guardReviewWrite refuses a write whose target is a review project, in any
// spelling of the project id (migration.NormalizeProjectID) or of the field
// that names it (migration.FieldStrings: the server's decoder matches keys
// case-insensitively). A target named only by id is looked up (GetItem, or
// the review projects' labels), and so is the stored copy an update of an
// item or label replaces (an update may move it out of a review project); a
// lookup that fails refuses rather than guesses.
func (c *client) guardReviewWrite(ctx context.Context, method string, request any) error {
	if migration.ReadMethod(method) || method == "SaveAgentReview" {
		return nil
	}
	body, err := json.Marshal(request)
	if err != nil {
		return err
	}
	if err := migration.RefuseDeclaredReviewWrite(method, body); err != nil {
		return err
	}
	items := migration.FieldStrings(body, "itemId")
	var labels []string
	switch method {
	case "DeleteItem":
		items = append(items, migration.FieldStrings(body, "id")...)
	case "DeleteLabel":
		labels = append(labels, migration.FieldStrings(body, "id")...)
	case "SaveItem":
		// The server refuses an item without a projectId, so only an update
		// naming one can move a stored item.
		if updatesStored(body) && len(migration.FieldStrings(body, "item", "projectId")) > 0 {
			items = append(items, migration.FieldStrings(body, "item", "id")...)
		}
	case "SaveLabel":
		if updatesStored(body) {
			labels = append(labels, migration.FieldStrings(body, "label", "id")...)
		}
	}
	for _, id := range items {
		if err := c.refuseReviewItem(ctx, method, id); err != nil {
			return err
		}
	}
	for _, id := range labels {
		if err := c.refuseReviewLabel(ctx, method, id); err != nil {
			return err
		}
	}
	return nil
}

// updatesStored reports whether a save may replace a stored document: any
// expectedRevision other than an explicit or omitted 0. A create
// (expectedRevision 0) of an existing id is refused by the server.
func updatesStored(body []byte) bool {
	var request map[string]json.RawMessage
	if json.Unmarshal(body, &request) != nil {
		return true
	}
	for name, value := range request {
		if strings.EqualFold(name, "expectedRevision") && strings.TrimSpace(string(value)) != "0" {
			return true
		}
	}
	return false
}

func (c *client) refuseReviewItem(ctx context.Context, method, id string) error {
	data, err := c.call(ctx, "GetItem", map[string]any{"id": id})
	if err != nil {
		var api *apiError
		if errors.As(err, &api) && api.Status == http.StatusNotFound {
			return nil // no such item: nothing to protect, and the server refuses the write
		}
		return fmt.Errorf("refusing %s: cannot check which project item %s belongs to: %w", method, id, err)
	}
	var got struct {
		Item struct {
			ProjectID string `json:"projectId"`
		} `json:"item"`
	}
	if err := json.Unmarshal(data, &got); err != nil || got.Item.ProjectID == "" {
		return fmt.Errorf("refusing %s: cannot read the project of item %s", method, id)
	}
	if migration.IsReviewProject(got.Item.ProjectID) {
		return migration.ReviewWriteRefusal(method, got.Item.ProjectID)
	}
	return nil
}

// refuseReviewLabel looks a label id up in every review project (the API has
// no GetLabel).
func (c *client) refuseReviewLabel(ctx context.Context, method, id string) error {
	projects, err := c.listIDs(ctx, "ListProjects", nil, "projects")
	if err != nil {
		return fmt.Errorf("refusing %s: cannot list projects to check label %s: %w", method, id, err)
	}
	for _, project := range projects {
		if !migration.IsReviewProject(project) {
			continue
		}
		labels, err := c.listIDs(ctx, "ListLabels", map[string]any{"projectId": project}, "labels")
		if err != nil {
			return fmt.Errorf("refusing %s: cannot list the labels of %s to check label %s: %w", method, project, id, err)
		}
		for _, label := range labels {
			if label == id {
				return migration.ReviewWriteRefusal(method, project)
			}
		}
	}
	return nil
}

func (c *client) listIDs(ctx context.Context, method string, filter map[string]any, key string) ([]string, error) {
	var ids []string
	cursor := ""
	for {
		request := map[string]any{"cursor": cursor, "limit": 500}
		for name, value := range filter {
			request[name] = value
		}
		data, err := c.call(ctx, method, request)
		if err != nil {
			return nil, err
		}
		var page map[string]json.RawMessage
		if err := json.Unmarshal(data, &page); err != nil {
			return nil, err
		}
		var docs []struct {
			ID string `json:"id"`
		}
		if err := json.Unmarshal(page[key], &docs); err != nil && page[key] != nil {
			return nil, err
		}
		for _, doc := range docs {
			ids = append(ids, doc.ID)
		}
		var next string
		_ = json.Unmarshal(page["nextCursor"], &next)
		if next == "" || next == cursor {
			return ids, nil
		}
		cursor = next
	}
}

// mcpReviewRefusal checks one MCP message from the connected client: a
// tools/call of a write tool aimed at a review project is answered locally
// with a tool error and never forwarded. Unknown tools are checked by the
// fields they name.
//
// The line is decoded exactly as the server decodes it (internal/annotation
// mcp.go): the message into a struct whose params stays raw (of a repeated or
// respelled key, the last one read wins), then params into a struct whose
// arguments is a map (annotation.Object), into which encoding/json merges
// every key that folds to "arguments". The guard checks that merged map, so a
// repeated or respelled arguments key cannot hide a write from it, and the
// line the server receives decodes to the same request.
func (c *client) mcpReviewRefusal(ctx context.Context, line []byte) error {
	var message struct {
		Method string          `json:"method"`
		Params json.RawMessage `json:"params"`
	}
	if json.Unmarshal(line, &message) != nil || message.Method != "tools/call" {
		return nil
	}
	var params struct {
		Name      string         `json:"name"`
		Arguments map[string]any `json:"arguments"`
	}
	if json.Unmarshal(message.Params, &params) != nil || mcpReadTools[params.Name] {
		return nil // the server refuses tool parameters it cannot decode
	}
	method, ok := mcpToolMethods[params.Name]
	if !ok {
		method = params.Name
	}
	var arguments any = map[string]any{}
	if params.Arguments != nil {
		arguments = params.Arguments
	}
	return c.guardReviewWrite(ctx, method, arguments)
}

// mcpMessageRefusal checks one line from the connected client: a single
// message, or a JSON-RPC batch array. A batch is refused whole when any of
// its messages (a nested array included) would be refused on its own, so no
// part of it is forwarded; a batch with no review-project write is forwarded
// unchanged.
func (c *client) mcpMessageRefusal(ctx context.Context, line []byte) error {
	trimmed := bytes.TrimSpace(line)
	if len(trimmed) == 0 || trimmed[0] != '[' {
		return c.mcpReviewRefusal(ctx, trimmed)
	}
	var batch []json.RawMessage
	if err := json.Unmarshal(trimmed, &batch); err != nil {
		return fmt.Errorf("refusing an MCP batch that is not a JSON array of messages: %w", err)
	}
	for i, message := range batch {
		if err := c.mcpMessageRefusal(ctx, message); err != nil {
			return fmt.Errorf("refusing the whole MCP batch (message %d of %d): %w", i+1, len(batch), err)
		}
	}
	return nil
}

// mcpRequestIDs lists the ids of the requests in one line (a single message
// or a batch); notifications have none.
func mcpRequestIDs(line []byte) []json.RawMessage {
	trimmed := bytes.TrimSpace(line)
	if len(trimmed) > 0 && trimmed[0] == '[' {
		var batch []json.RawMessage
		_ = json.Unmarshal(trimmed, &batch)
		var ids []json.RawMessage
		for _, message := range batch {
			ids = append(ids, mcpRequestIDs(message)...)
		}
		return ids
	}
	var message struct {
		ID json.RawMessage `json:"id"`
	}
	if json.Unmarshal(trimmed, &message) != nil || len(message.ID) == 0 {
		return nil
	}
	return []json.RawMessage{message.ID}
}

func mcpToolError(id json.RawMessage, message string) map[string]any {
	return map[string]any{"jsonrpc": "2.0", "id": id, "result": map[string]any{
		"content": []map[string]any{{"type": "text", "text": message}}, "isError": true,
	}}
}

func writeMCPToolError(output io.Writer, id json.RawMessage, message string) error {
	return json.NewEncoder(output).Encode(mcpToolError(id, message))
}

// writeMCPRefusal answers a refused line locally: one tool error for a single
// request, an array of them (one per request id) for a batch, nothing when the
// line holds only notifications.
func writeMCPRefusal(output io.Writer, line []byte, message string) error {
	ids := mcpRequestIDs(line)
	trimmed := bytes.TrimSpace(line)
	if len(trimmed) == 0 || trimmed[0] != '[' {
		if len(ids) == 0 {
			return nil
		}
		return writeMCPToolError(output, ids[0], message)
	}
	if len(ids) == 0 {
		return nil
	}
	responses := make([]map[string]any, 0, len(ids))
	for _, id := range ids {
		responses = append(responses, mcpToolError(id, message))
	}
	return json.NewEncoder(output).Encode(responses)
}
