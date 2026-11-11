package annotation

import (
	"context"
	"encoding/json"
	"net/url"
	"strings"
)

const mcpMentionLimit = 20

// searchMCPMentions implements the OpenAI composer at-mention extension using
// only shared read operations. Both project and item queries are workspace-wide
// bounded database queries, so typeahead never triggers a full project export.
func (s *Service) searchMCPMentions(ctx context.Context, arguments Object) (Object, error) {
	query, ok := arguments["query"].(string)
	if !ok {
		return nil, invalid("query must be a string")
	}
	if len(query) > 300 {
		return nil, invalid("query is too long")
	}
	matched, err := s.Call(ctx, "ListProjects", Object{"query": query, "limit": mcpMentionLimit})
	if err != nil {
		return nil, err
	}
	projects := []Object{}
	for _, raw := range matched["projects"].([]any) {
		project := raw.(map[string]any)
		projects = append(projects, mcpDocumentLink("project", project, "Annotation project"))
	}
	page, err := s.Call(ctx, "SearchItems", Object{"query": query, "limit": mcpMentionLimit})
	if err != nil {
		return nil, err
	}
	items := []Object{}
	for _, raw := range page["items"].([]any) {
		item := raw.(map[string]any)
		items = append(items, mcpDocumentLink("item", item, "Annotation item in project "+item["projectId"].(string)))
	}
	// Alternate project and item matches to preserve both kinds for an empty
	// query or a common substring. Each kind retains the store's stable ID order.
	links := []Object{}
	for i := 0; len(links) < mcpMentionLimit && (i < len(projects) || i < len(items)); i++ {
		if i < len(projects) {
			links = append(links, projects[i])
		}
		if i < len(items) && len(links) < mcpMentionLimit {
			links = append(links, items[i])
		}
	}
	return Object{"items": links}, nil
}

func mcpDocumentLink(kind string, document Object, description string) Object {
	title := document["title"]
	if kind == "project" {
		title = document["name"]
	}
	return Object{"type": "resource_link", "uri": "fieldwork://" + kind + "/" + document["id"].(string), "name": document["id"], "title": title, "description": description, "mimeType": "application/json"}
}

func mcpResourceTemplates() []Object {
	return []Object{
		{"uriTemplate": "fieldwork://project/{id}", "name": "annotation-project", "title": "Annotation project", "description": "Current project document, including its revision", "mimeType": "application/json"},
		{"uriTemplate": "fieldwork://item/{id}", "name": "annotation-item", "title": "Annotation item", "description": "Current item document, annotations and labels; source asset bytes are fetched separately over authenticated HTTP", "mimeType": "application/json"},
	}
}

func (s *Service) readMCPResource(ctx context.Context, uri string) (Object, error) {
	u, err := url.Parse(uri)
	if err != nil || u.Scheme != "fieldwork" || (u.Host != "project" && u.Host != "item") || u.User != nil {
		return nil, invalid("expected a fieldwork project or item resource URI")
	}
	id := strings.TrimPrefix(u.Path, "/")
	// Only canonical URIs are accepted: no extra path, credentials, queries,
	// fragments or percent-encoded aliases may resolve to a document.
	if checkID(id) != nil || uri != "fieldwork://"+u.Host+"/"+id {
		return nil, invalid("expected a canonical fieldwork project or item resource URI")
	}
	method := "GetItem"
	if u.Host == "project" {
		method = "GetProject"
	}
	value, err := s.Call(ctx, method, Object{"id": id})
	if err != nil {
		return nil, err
	}
	encoded, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	return Object{"contents": []Object{{"uri": uri, "mimeType": "application/json", "text": string(encoded)}}}, nil
}
