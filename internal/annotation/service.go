package annotation

import (
	"context"
	"encoding/json"
	"errors"
	"sort"
	"strings"

	"connectrpc.com/connect"
	annotationv1 "github.com/benebsworth/addr/gen/go/annotation/v1"
	"github.com/google/uuid"
	"google.golang.org/protobuf/types/known/structpb"
)

type Service struct {
	Store        Store
	AuthRequired bool
}
type actorKey struct{}

func actor(ctx context.Context) string {
	if a, ok := ctx.Value(actorKey{}).(string); ok {
		return a
	}
	return "workspace"
}

// summaryDrop are the item fields a queue list never shows; GetItem returns the full document.
var summaryDrop = []string{"annotations", "agentReviews", "metadata", "notes"}

// summariseItems returns list entries without their heavy fields, with annotationCount and summary=true in their place. A summary
// is never a document to save: clients open items with GetItem.
func summariseItems(docs []json.RawMessage) ([]json.RawMessage, error) {
	out := make([]json.RawMessage, 0, len(docs))
	for _, doc := range docs {
		var fields map[string]json.RawMessage
		if err := json.Unmarshal(doc, &fields); err != nil {
			return nil, err
		}
		var annotations []json.RawMessage
		if raw, ok := fields["annotations"]; ok {
			_ = json.Unmarshal(raw, &annotations)
		}
		for _, key := range summaryDrop {
			delete(fields, key)
		}
		fields["annotations"] = json.RawMessage("[]")
		fields["annotationCount"], _ = json.Marshal(len(annotations))
		fields["summary"] = json.RawMessage("true")
		data, err := json.Marshal(fields)
		if err != nil {
			return nil, err
		}
		out = append(out, data)
	}
	return out, nil
}

func decodeObject(data Object, into any) error {
	encoded, err := json.Marshal(data)
	if err != nil {
		return invalid("invalid JSON")
	}
	if json.Unmarshal(encoded, into) != nil {
		return invalid("request fields have invalid types")
	}
	return nil
}

func objectOf(value any) (Object, error) {
	data, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	var result Object
	err = json.Unmarshal(data, &result)
	return result, err
}

// Call is the single operation boundary shared by Connect and REST.
func (s *Service) Call(ctx context.Context, method string, data Object) (result Object, err error) {
	defer func() { logInternalServiceError(method, err) }()
	if data == nil {
		data = Object{}
	}
	switch method {
	case "SaveAgentReview":
		return s.saveAgentReview(ctx, data)
	case "GetInfo":
		workspace, _ := ctx.Value(workspaceKey{}).(Workspace)
		accountUID := ""
		if strings.HasPrefix(actor(ctx), "user:") {
			accountUID = strings.TrimPrefix(actor(ctx), "user:")
		}
		requestID, _ := ctx.Value(requestIDKey{}).(string)
		adapters := s.Store.AdapterInfos()
		sort.Slice(adapters, func(i, j int) bool { return adapters[i].ID < adapters[j].ID })
		return objectOf(Object{"name": "Fieldwork", "version": "1", "workspace": workspace, "accountUid": accountUID, "requestId": requestID, "adapters": adapters, "storage": s.Store.Driver(), "authRequired": s.AuthRequired, "maxDocumentBytes": MaxDocumentBytes,
			"agentReviews": Object{"tool": "save_agent_review", "storage": "item.agentReviews", "humanReview": "item.status and annotation overrides", "humanApproval": false, "trainingEligible": false}})
	case "ListProjects", "ListItems", "ListLabels", "SearchItems":
		var req struct {
			ProjectID string `json:"projectId"`
			Cursor    string `json:"cursor"`
			Query     string `json:"query"`
			Status    string `json:"status"`
			Adapter   string `json:"adapter"`
			Tag       string `json:"tag"`
			Summary   bool   `json:"summary"`
			Limit     int    `json:"limit"`
		}
		if err := decodeObject(data, &req); err != nil {
			return nil, err
		}
		kind, key := "project", "projects"
		if method == "ListItems" || method == "SearchItems" {
			kind, key = "item", "items"
		}
		if method == "ListLabels" {
			kind, key = "label", "labels"
		}
		// Mention search uses the same bounded query against workspace-wide
		// items. Ordinary item/label lists retain their required project filter.
		if kind != "project" && method != "SearchItems" && req.ProjectID == "" {
			return nil, invalid("projectId is required")
		}
		if len(req.Query) > 300 {
			return nil, invalid("query is too long")
		}
		if len(req.Tag) > 100 {
			return nil, invalid("tag is too long")
		}
		docs, next, err := s.Store.List(ctx, kind, ListOptions{ProjectID: req.ProjectID, Cursor: req.Cursor, Query: req.Query, Status: req.Status, Adapter: req.Adapter, Tag: req.Tag, Limit: req.Limit})
		if err != nil {
			return nil, serviceStage("store_list", err)
		}
		if req.Summary && kind == "item" {
			if docs, err = summariseItems(docs); err != nil {
				return nil, serviceStage("summarise_items", err)
			}
		}
		result, err := objectOf(Object{key: docs, "nextCursor": next})
		return result, serviceStage("encode_response", err)
	case "GetProject", "GetItem":
		var req struct {
			ID string `json:"id"`
		}
		if err := decodeObject(data, &req); err != nil {
			return nil, err
		}
		if method == "GetProject" {
			if err := checkID(req.ID); err != nil {
				return nil, err
			}
			doc, err := s.Store.Get(ctx, "project", req.ID)
			if err != nil {
				return nil, serviceStage("store_get", err)
			}
			result, err := objectOf(Object{"project": doc})
			return result, serviceStage("encode_response", err)
		}
		doc, err := s.Store.Get(ctx, "item", req.ID)
		if err != nil {
			return nil, serviceStage("store_get", err)
		}
		var item Item
		if err = json.Unmarshal(doc, &item); err != nil {
			return nil, serviceStage("decode_item", err)
		}
		labels, next, err := s.Store.List(ctx, "label", ListOptions{ProjectID: item.ProjectID, Limit: 500})
		if err != nil {
			return nil, serviceStage("list_labels", err)
		}
		result, err := objectOf(Object{"item": item, "labels": labels, "labelsNextCursor": next})
		return result, serviceStage("encode_response", err)
	case "SaveProject", "SaveItem", "SaveLabel":
		var req struct {
			Project          json.RawMessage `json:"project"`
			Item             json.RawMessage `json:"item"`
			Label            json.RawMessage `json:"label"`
			ExpectedRevision int64           `json:"expectedRevision"`
		}
		if err := decodeObject(data, &req); err != nil {
			return nil, err
		}
		kind, input, prefix := "project", req.Project, "prj_"
		if method == "SaveItem" {
			kind, input, prefix = "item", req.Item, "itm_"
		}
		if method == "SaveLabel" {
			kind, input, prefix = "label", req.Label, "lbl_"
		}
		var base Base
		if len(input) == 0 || string(input) == "null" || json.Unmarshal(input, &base) != nil {
			return nil, invalid("%s object is required", kind)
		}
		if base.ID == "" {
			if req.ExpectedRevision != 0 {
				return nil, invalid("id is required for updates")
			}
			base.ID = prefix + uuid.NewString()
		}
		doc, err := s.Store.Save(ctx, kind, base.ID, req.ExpectedRevision, actor(ctx), input)
		if err != nil {
			return nil, err
		}
		return objectOf(Object{kind: doc})
	case "DeleteItem", "DeleteLabel":
		var req struct {
			ID               string `json:"id"`
			ExpectedRevision int64  `json:"expectedRevision"`
		}
		if err := decodeObject(data, &req); err != nil {
			return nil, err
		}
		kind := "item"
		if method == "DeleteLabel" {
			kind = "label"
		}
		if err := s.Store.Delete(ctx, kind, req.ID, req.ExpectedRevision, actor(ctx)); err != nil {
			return nil, err
		}
		return Object{"deleted": true, "id": req.ID}, nil
	case "SaveAnnotation", "DeleteAnnotation":
		var req struct {
			ItemID           string     `json:"itemId"`
			ID               string     `json:"id"`
			Annotation       Annotation `json:"annotation"`
			ExpectedRevision int64      `json:"expectedRevision"`
		}
		if err := decodeObject(data, &req); err != nil {
			return nil, err
		}
		if req.ExpectedRevision <= 0 {
			return nil, invalid("expectedRevision of the parent item is required")
		}
		doc, err := s.Store.Get(ctx, "item", req.ItemID)
		if err != nil {
			return nil, err
		}
		var item Item
		if err = json.Unmarshal(doc, &item); err != nil {
			return nil, err
		}
		if item.Revision != req.ExpectedRevision {
			return nil, ErrConflict
		}
		if method == "SaveAnnotation" && req.Annotation.ID == "" {
			req.Annotation.ID = "ann_" + uuid.NewString()
		}
		found := false
		annotations := []Annotation{}
		for _, a := range item.Annotations {
			if method == "DeleteAnnotation" && a.ID == req.ID {
				found = true
				continue
			}
			if method == "SaveAnnotation" && a.ID == req.Annotation.ID {
				a = req.Annotation
				found = true
			}
			annotations = append(annotations, a)
		}
		if method == "DeleteAnnotation" && !found {
			return nil, ErrNotFound
		}
		if method == "SaveAnnotation" && !found {
			annotations = append(annotations, req.Annotation)
		}
		item.Annotations = annotations
		encoded, _ := json.Marshal(item)
		doc, err = s.Store.Save(ctx, "item", item.ID, req.ExpectedRevision, actor(ctx), encoded)
		if err != nil {
			return nil, err
		}
		return objectOf(Object{"item": doc})
	case "ListChanges":
		var req struct {
			ProjectID string `json:"projectId"`
			Cursor    string `json:"cursor"`
			Limit     int    `json:"limit"`
		}
		if err := decodeObject(data, &req); err != nil {
			return nil, err
		}
		page, err := s.Store.Changes(ctx, req.ProjectID, req.Cursor, req.Limit)
		if err != nil {
			return nil, err
		}
		return objectOf(page)
	case "ExportProject":
		var req struct {
			ProjectID string `json:"projectId"`
		}
		if err := decodeObject(data, &req); err != nil {
			return nil, err
		}
		if err := checkID(req.ProjectID); err != nil {
			return nil, err
		}
		export, err := s.Store.Export(ctx, req.ProjectID)
		if err != nil {
			return nil, err
		}
		return objectOf(export)
	default:
		return nil, ErrNotFound
	}
}

func connectError(err error) error {
	switch {
	case errors.Is(err, ErrNotFound):
		return connect.NewError(connect.CodeNotFound, err)
	case errors.Is(err, ErrConflict):
		return connect.NewError(connect.CodeAborted, err)
	case errors.Is(err, ErrInvalid):
		return connect.NewError(connect.CodeInvalidArgument, err)
	case errors.Is(err, context.Canceled):
		return connect.NewError(connect.CodeCanceled, errors.New("request canceled"))
	case errors.Is(err, context.DeadlineExceeded):
		return connect.NewError(connect.CodeDeadlineExceeded, errors.New("request timed out"))
	default:
		return connect.NewError(connect.CodeInternal, errors.New("annotation storage request failed"))
	}
}

func (s *Service) rpc(ctx context.Context, method string, req *connect.Request[annotationv1.APIRequest]) (*connect.Response[annotationv1.APIResponse], error) {
	data := Object{}
	if req.Msg.Data != nil {
		data = req.Msg.Data.AsMap()
	}
	result, err := s.Call(ctx, method, data)
	if err != nil {
		return nil, connectError(err)
	}
	message, err := structpb.NewStruct(result)
	if err != nil {
		logInternalServiceError(method, serviceStage("connect_struct", err))
		return nil, connectError(err)
	}
	return connect.NewResponse(&annotationv1.APIResponse{Data: message}), nil
}
