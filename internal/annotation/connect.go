// Connect method bindings delegate to the shared application boundary.
package annotation

import (
	"connectrpc.com/connect"
	"context"
	annotationv1 "github.com/benebsworth/addr/gen/go/annotation/v1"
)

func (s *Service) GetInfo(ctx context.Context, req *connect.Request[annotationv1.APIRequest]) (*connect.Response[annotationv1.APIResponse], error) {
	return s.rpc(ctx, "GetInfo", req)
}

func (s *Service) ListProjects(ctx context.Context, req *connect.Request[annotationv1.APIRequest]) (*connect.Response[annotationv1.APIResponse], error) {
	return s.rpc(ctx, "ListProjects", req)
}

func (s *Service) SaveProject(ctx context.Context, req *connect.Request[annotationv1.APIRequest]) (*connect.Response[annotationv1.APIResponse], error) {
	return s.rpc(ctx, "SaveProject", req)
}

func (s *Service) ListItems(ctx context.Context, req *connect.Request[annotationv1.APIRequest]) (*connect.Response[annotationv1.APIResponse], error) {
	return s.rpc(ctx, "ListItems", req)
}

func (s *Service) GetItem(ctx context.Context, req *connect.Request[annotationv1.APIRequest]) (*connect.Response[annotationv1.APIResponse], error) {
	return s.rpc(ctx, "GetItem", req)
}

func (s *Service) SaveItem(ctx context.Context, req *connect.Request[annotationv1.APIRequest]) (*connect.Response[annotationv1.APIResponse], error) {
	return s.rpc(ctx, "SaveItem", req)
}

func (s *Service) DeleteItem(ctx context.Context, req *connect.Request[annotationv1.APIRequest]) (*connect.Response[annotationv1.APIResponse], error) {
	return s.rpc(ctx, "DeleteItem", req)
}

func (s *Service) ListLabels(ctx context.Context, req *connect.Request[annotationv1.APIRequest]) (*connect.Response[annotationv1.APIResponse], error) {
	return s.rpc(ctx, "ListLabels", req)
}

func (s *Service) SaveLabel(ctx context.Context, req *connect.Request[annotationv1.APIRequest]) (*connect.Response[annotationv1.APIResponse], error) {
	return s.rpc(ctx, "SaveLabel", req)
}

func (s *Service) DeleteLabel(ctx context.Context, req *connect.Request[annotationv1.APIRequest]) (*connect.Response[annotationv1.APIResponse], error) {
	return s.rpc(ctx, "DeleteLabel", req)
}

func (s *Service) SaveAnnotation(ctx context.Context, req *connect.Request[annotationv1.APIRequest]) (*connect.Response[annotationv1.APIResponse], error) {
	return s.rpc(ctx, "SaveAnnotation", req)
}

func (s *Service) DeleteAnnotation(ctx context.Context, req *connect.Request[annotationv1.APIRequest]) (*connect.Response[annotationv1.APIResponse], error) {
	return s.rpc(ctx, "DeleteAnnotation", req)
}

func (s *Service) ListChanges(ctx context.Context, req *connect.Request[annotationv1.APIRequest]) (*connect.Response[annotationv1.APIResponse], error) {
	return s.rpc(ctx, "ListChanges", req)
}

func (s *Service) ExportProject(ctx context.Context, req *connect.Request[annotationv1.APIRequest]) (*connect.Response[annotationv1.APIResponse], error) {
	return s.rpc(ctx, "ExportProject", req)
}
