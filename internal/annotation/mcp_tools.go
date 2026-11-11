package annotation

import (
	"context"
	"sort"
	"strings"
)

type mcpTool struct {
	method     string
	definition Object
}

var mcpCatalog = makeMCPCatalog()

func mcpTools() []Object {
	names := make([]string, 0, len(mcpCatalog))
	for name := range mcpCatalog {
		names = append(names, name)
	}
	sort.Strings(names)
	result := make([]Object, 0, len(names))
	for _, name := range names {
		result = append(result, mcpCatalog[name].definition)
	}
	return result
}

func (s *Service) callMCP(ctx context.Context, name string, arguments Object) (Object, error) {
	tool := mcpCatalog[name]
	if arguments == nil {
		arguments = Object{}
	}
	schema := tool.definition["inputSchema"].(Object)
	for _, key := range schema["required"].([]string) {
		value, exists := arguments[key]
		if !exists || value == nil || value == "" && !(name == "search_mentions" && key == "query") {
			return nil, invalid("%s is required", key)
		}
	}
	for key := range arguments {
		if _, exists := schema["properties"].(Object)[key]; !exists {
			return nil, invalid("unknown tool argument %q", key)
		}
	}
	if name == "search_mentions" {
		return s.searchMCPMentions(ctx, arguments)
	}
	includeDocuments := true
	if value, exists := arguments["includeDocuments"]; exists {
		var ok bool
		includeDocuments, ok = value.(bool)
		if !ok {
			return nil, invalid("includeDocuments must be a boolean")
		}
		delete(arguments, "includeDocuments")
	}
	if _, supportsLimit := schema["properties"].(Object)["limit"]; supportsLimit {
		if _, exists := arguments["limit"]; !exists {
			arguments["limit"] = 20
		}
	}
	// Domain validation, optimistic revisions, auth context and persistence all
	// remain at the same boundary as REST and Connect.
	value, err := s.Call(ctx, tool.method, arguments)
	if err != nil {
		return nil, err
	}
	if name == "list_changes" && !includeDocuments {
		changes, _ := value["changes"].([]any)
		for _, raw := range changes {
			if change, ok := raw.(map[string]any); ok {
				delete(change, "document")
			}
		}
	}
	return value, nil
}

func makeMCPCatalog() map[string]mcpTool {
	str := func(description string) Object { return Object{"type": "string", "description": description} }
	object := func(properties Object, required ...string) Object {
		if required == nil {
			required = []string{}
		}
		return Object{"type": "object", "properties": properties, "required": required, "additionalProperties": false}
	}
	id := Object{"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$", "description": "Stable document ID. Omit on create to generate one; include on updates."}
	projectID := str("Project ID; keep this fixed while reusing a change cursor")
	limit := Object{"type": "integer", "minimum": 1, "maximum": 500, "default": 20, "description": "Page size; MCP defaults to 20. Smaller pages reduce large annotation payloads."}
	revision := Object{"type": "integer", "minimum": 0, "maximum": MaxSafeInteger - 1, "description": "0 creates a new document. Updates require the current revision; conflicts require reload and reconciliation."}
	parentRevision := Object{"type": "integer", "minimum": 1, "maximum": MaxSafeInteger - 1, "description": "Current parent item revision from get_item; the save increments that item's revision."}
	storedRevision := Object{"type": "integer", "readOnly": true, "description": "Server-managed; concurrency is controlled by expectedRevision."}
	geometry := object(Object{"type": Object{"type": "string", "enum": []string{"Point", "LineString", "Polygon"}}, "coordinates": Object{"type": "array", "description": "GeoJSON coordinates. Map uses [longitude,latitude]; image uses normalized [x,y] in [0,1]. LineStrings need at least two points; polygon rings must be closed."}}, "type", "coordinates")
	annotation := object(Object{"id": id, "labelId": str("Label ID from list_labels; empty means no label"), "geometry": geometry, "notes": str("Annotation notes"), "properties": Object{"type": "object", "description": "Adapter-specific properties, preserved as JSON"}}, "geometry")
	itemAnnotation := object(annotation["properties"].(Object), "id", "geometry")
	project := object(Object{"id": id, "name": str("Project name"), "description": str("Project description"), "revision": storedRevision, "createdAt": str("Server-managed creation timestamp"), "updatedAt": str("Server-managed update timestamp")}, "name")
	label := object(Object{"id": id, "projectId": projectID, "name": str("Label name"), "color": Object{"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"}, "revision": storedRevision, "createdAt": str("Server-managed creation timestamp"), "updatedAt": str("Server-managed update timestamp")}, "projectId", "name", "color")
	crop := object(Object{"x": Object{"type": "number"}, "y": Object{"type": "number"}, "width": Object{"type": "number"}, "height": Object{"type": "number"}}, "x", "y", "width", "height")
	crop["type"] = []string{"object", "null"}
	crop["description"] = "Image-only crop rectangle in normalized [0,1] coordinates; null clears the crop."
	item := object(Object{
		"id": id, "projectId": projectID, "title": str("Item title"), "adapter": str("Adapter ID from get_info; immutable after create"),
		"source":   Object{"type": "object", "description": "Adapter source; preserve the get_item value when editing. Imported sources and image coordinates are immutable."},
		"metadata": Object{"type": "object"}, "tags": Object{"type": "array", "items": str("Tag")},
		"status": Object{"type": "string", "enum": []string{"unreviewed", "in_review", "approved", "rejected"}},
		"notes":  str("Item notes"), "crop": crop,
		"annotations":  Object{"type": "array", "items": itemAnnotation, "description": "Complete annotation list; every annotation requires an ID when saving a whole item."},
		"agentReviews": Object{"type": "array", "items": Object{"type": "object"}, "description": "Server-managed agent review history. Preserve unchanged or omit; use save_agent_review to append an assessment."},
		"revision":     storedRevision, "createdAt": str("Server-managed creation timestamp"), "updatedAt": str("Server-managed update timestamp"),
	}, "projectId", "title", "adapter", "source")
	catalog := map[string]mcpTool{}
	add := func(name, method, description string, properties Object, readOnly bool, required ...string) {
		title := strings.ToUpper(name[:1]) + strings.ReplaceAll(name[1:], "_", " ")
		catalog[name] = mcpTool{method: method, definition: Object{"name": name, "title": title, "description": description, "inputSchema": object(properties, required...), "annotations": Object{"readOnlyHint": readOnly, "destructiveHint": !readOnly, "idempotentHint": readOnly, "openWorldHint": false}}}
	}
	add("get_info", "GetInfo", "Discover supported adapters, coordinate systems, geometry types, writeHints, and limits; follow adapter writeHints before editing.", Object{}, true)
	add("save_agent_review", "SaveAgentReview", "Append a separate agent assessment of an item or annotation, including in -review projects. Read get_item and original imagery first. Records the agent identity, evidence notes and current source/geometry binding without changing human status, geometry or checks. Agent approval is tier 3, never human approval or training eligibility. Reload after save; reconcile revision conflicts.", Object{
		"itemId": str("Parent item ID"), "annotationId": str("Existing annotation ID; omit for an assessment of the whole item"), "expectedRevision": parentRevision,
		"reviewer": str("Stable agent identity, such as codex"), "decision": Object{"type": "string", "enum": []string{"approved", "rejected", "needs_adjustment", "uncertain"}},
		"notes": str("Visible evidence, scope inspected and remaining uncertainty; required"), "vegetationType": Object{"type": "string", "enum": []string{"grass", "hedge", "shrub", "tree"}, "description": "Optional type recommendation on an approved annotation review; never changes the human type"},
	}, false, "itemId", "expectedRevision", "reviewer", "decision", "notes")
	agentTool := catalog["save_agent_review"].definition
	agentTool["annotations"] = Object{"readOnlyHint": false, "destructiveHint": false, "idempotentHint": false, "openWorldHint": false}
	add("list_projects", "ListProjects", "List accessible projects with their IDs and current revisions. Page until nextCursor is empty.", Object{"cursor": str("Next document-list cursor"), "limit": limit, "query": str("Literal project name substring")}, true)
	add("list_items", "ListItems", "List complete items in a project with optional filters. Prefer a small limit for items with many annotations; page until nextCursor is empty.", Object{"projectId": projectID, "cursor": str("Next document-list cursor"), "limit": limit, "query": str("Literal title substring"), "status": str("unreviewed, in_review, approved, rejected"), "adapter": str("Adapter ID from get_info"), "tag": str("Exact item tag (case-insensitive)")}, true, "projectId")
	add("get_item", "GetItem", "Read an item, current revision, annotations, source, crop, and labels. If labelsNextCursor is nonempty, continue with list_labels.", Object{"id": str("Item ID")}, true, "id")
	add("list_labels", "ListLabels", "List a project's labels and their current revisions. Page until nextCursor is empty.", Object{"projectId": projectID, "cursor": str("Next document-list cursor"), "limit": limit, "query": str("Literal label name substring")}, true, "projectId")
	add("list_changes", "ListChanges", "Read committed creates, updates, and deletes after an exclusive decimal cursor. For a project mirror, provide projectId and start with cursor '0'; keep the project filter fixed; continue while hasMore. Apply the complete page and save nextCursor atomically with local documents. Omitted projectId reads all projects; omitted cursor starts at '0'. Delete records retain the prior document but increment the change revision. includeDocuments:false omits payloads for compact inspection and cannot build a full mirror.", Object{"projectId": str("Optional project filter. For project sync, supply it and keep it fixed with the saved cursor."), "cursor": Object{"type": "string", "pattern": "^(0|[1-9][0-9]*)$", "default": "0", "description": "Exclusive decimal cursor as a string; '0' starts a fresh mirror"}, "limit": limit, "includeDocuments": Object{"type": "boolean", "default": true, "description": "False returns change metadata without documents; true is required to apply a complete mirror"}}, true)
	add("export_project", "ExportProject", "Read a consistent project snapshot and cursor. Maximum 32 MiB; for larger projects, page list_changes from '0' or use annotate sync. Protected source assets are separate HTTP resources, retrieved by annotation-migrate pull.", Object{"projectId": projectID}, true, "projectId")
	add("save_project", "SaveProject", "Create or replace a project with revision checking. Read its current document first for updates; send the complete document to preserve all fields.", Object{"project": project, "expectedRevision": revision}, false, "project", "expectedRevision")
	add("save_label", "SaveLabel", "Create or replace a label with revision checking. Read list_labels first for updates; a label cannot move between projects.", Object{"label": label, "expectedRevision": revision}, false, "label", "expectedRevision")
	add("save_item", "SaveItem", "Create or replace a complete item with revision checking. For updates, read get_item and preserve fields you are not editing: omitted optional fields reset. Use save_annotation for an individual annotation. Changing approved geometry returns the item to in_review.", Object{"item": item, "expectedRevision": revision}, false, "item", "expectedRevision")
	add("save_annotation", "SaveAnnotation", "Create or replace one annotation without resending the whole item. Use the parent item's current expectedRevision. Omit annotation.id to create, or include it to replace; send the complete annotation when replacing. Returns the updated parent item.", Object{"itemId": str("Parent item ID"), "annotation": annotation, "expectedRevision": parentRevision}, false, "itemId", "annotation", "expectedRevision")
	add("delete_annotation", "DeleteAnnotation", "Delete one annotation with the parent item's current expectedRevision. Returns the updated parent item.", Object{"itemId": str("Parent item ID"), "id": str("Annotation ID"), "expectedRevision": parentRevision}, false, "itemId", "id", "expectedRevision")
	deleteRevision := Object{"type": "integer", "minimum": 1, "maximum": MaxSafeInteger - 1, "description": "Current document revision; stale values fail without deleting"}
	add("delete_item", "DeleteItem", "Delete an item with revision checking. Its deletion is retained in the change feed; the ID cannot be reused.", Object{"id": str("Item ID"), "expectedRevision": deleteRevision}, false, "id", "expectedRevision")
	add("delete_label", "DeleteLabel", "Delete an unused label with revision checking. Labels still referenced by annotations cannot be deleted.", Object{"id": str("Label ID"), "expectedRevision": deleteRevision}, false, "id", "expectedRevision")
	add("search_mentions", "", "Search project names and annotation item titles across the workspace for the composer at-mention picker. The query may be empty. Returns up to 20 read-only resource links in stable ID order.", Object{"query": Object{"type": "string", "maxLength": 300, "description": "Literal name/title substring; may be empty"}}, true, "query")
	mentions := catalog["search_mentions"].definition
	mentions["title"] = "Search projects and annotation items"
	mentions["_meta"] = Object{"openai/extensions": Object{"mentions/search": Object{}}, "ui": Object{"visibility": []string{"app"}}}
	mentions["outputSchema"] = object(Object{"items": Object{"type": "array", "maxItems": mcpMentionLimit, "items": object(Object{
		"type": Object{"type": "string", "const": "resource_link"}, "uri": Object{"type": "string", "format": "uri"},
		"name": str("Stable document ID"), "title": str("Project name or item title"), "description": str("Resource kind and project context"), "mimeType": Object{"type": "string", "const": "application/json"},
	}, "type", "uri", "name", "title", "description", "mimeType")}}, "items")
	return catalog
}
