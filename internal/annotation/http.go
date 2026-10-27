package annotation

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"connectrpc.com/connect"
	"github.com/benebsworth/addr/gen/go/annotation/v1/annotationv1connect"
	"github.com/google/uuid"
)

const MaxRequestBytes = 4 << 20

type requestIDKey struct{}

type HTTPConfig struct {
	Token             string
	Dev               bool
	AllowedOrigins    []string
	StaticDir         string
	AssetDir          string
	Workspaces        *Workspaces
	IdentityBridgeKey string
}

func NewHTTPHandler(store Store, config HTTPConfig) (http.Handler, error) {
	if config.Workspaces != nil {
		if config.Dev || len(config.IdentityBridgeKey) < 32 || config.IdentityBridgeKey == config.Token || config.AssetDir != "" {
			return nil, errors.New("shared workspaces require a distinct identity bridge key and edge asset storage")
		}
		store = workspaceStore{root: config.Workspaces.root}
	}
	if !config.Dev && (len(config.Token) < 24 || strings.ContainsAny(config.Token, " \t\r\n")) {
		return nil, errors.New("ANNOTATION_TOKEN must contain at least 24 non-whitespace characters in cloud mode")
	}
	origins := map[string]bool{}
	for _, origin := range config.AllowedOrigins {
		origin = strings.TrimSpace(origin)
		if origin == "" {
			continue
		}
		u, err := url.Parse(origin)
		if err != nil || u.Host == "" || (u.Scheme != "https" && u.Scheme != "http") || u.Path != "" || u.RawQuery != "" || u.Fragment != "" || u.User != nil {
			return nil, errors.New("allowed origins must be exact http(s) origins without paths or wildcards")
		}
		origins[origin] = true
	}
	service := &Service{Store: store, AuthRequired: !config.Dev}
	dataMux := http.NewServeMux()
	path, handler := annotationv1connect.NewAnnotationServiceHandler(service, connect.WithReadMaxBytes(MaxRequestBytes), connect.WithSendMaxBytes(40<<20))
	dataMux.Handle(path, handler)
	dataMux.HandleFunc("/api/v1/", service.serveREST)
	if config.AssetDir != "" {
		assets, err := localAssets(config.AssetDir)
		if err != nil {
			return nil, err
		}
		dataMux.Handle("/api/v1/assets/", assets)
	}
	dataMux.HandleFunc("/mcp", service.serveMCP)
	protected := protect(dataMux, config, origins)
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, http.StatusOK, Object{"status": "ok"}) })
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()
		if store.Ping(ctx) != nil {
			writeJSON(w, http.StatusServiceUnavailable, Object{"status": "unavailable"})
			return
		}
		writeJSON(w, http.StatusOK, Object{"status": "ready"})
	})
	mux.Handle("/api/", protected)
	mux.Handle("/annotation.v1.AnnotationService/", protected)
	mux.Handle("/mcp", protected)
	if config.StaticDir != "" {
		root, err := filepath.Abs(config.StaticDir)
		if err != nil {
			return nil, err
		}
		if _, err = os.Stat(filepath.Join(root, "index.html")); err != nil {
			return nil, errors.New("static directory must contain index.html")
		}
		files := http.FileServer(http.Dir(root))
		mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodGet && r.Method != http.MethodHead {
				w.WriteHeader(http.StatusMethodNotAllowed)
				return
			}
			clean := filepath.Clean("/" + r.URL.Path)
			if _, err := os.Stat(filepath.Join(root, clean)); err == nil {
				files.ServeHTTP(w, r)
				return
			}
			// A missing asset must not return the SPA HTML with a misleading 200.
			if filepath.Ext(clean) != "" {
				http.NotFound(w, r)
				return
			}
			http.ServeFile(w, r, filepath.Join(root, "index.html"))
		})
	} else {
		mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/" {
				http.NotFound(w, r)
				return
			}
			writeJSON(w, http.StatusOK, Object{"name": "Fieldwork annotation API", "version": "1"})
		})
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestID := uuid.NewString()
		w.Header().Set("X-Request-ID", requestID)
		r = r.WithContext(context.WithValue(r.Context(), requestIDKey{}, requestID))
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "strict-origin-when-cross-origin")
		w.Header().Set("X-Frame-Options", "DENY")
		mux.ServeHTTP(w, r)
	}), nil
}

func loopbackHost(value string) bool {
	host := value
	if parsed, _, err := net.SplitHostPort(value); err == nil {
		host = parsed
	}
	host = strings.Trim(host, "[]")
	return strings.EqualFold(host, "localhost") || net.ParseIP(host) != nil && net.ParseIP(host).IsLoopback()
}

func protect(next http.Handler, config HTTPConfig, allowed map[string]bool) http.Handler {
	expected := sha256.Sum256([]byte(config.Token))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if config.Dev && (!loopbackHost(r.Host) || !loopbackHost(r.RemoteAddr)) {
			writeJSON(w, http.StatusForbidden, Object{"code": "permission_denied", "message": "local development accepts only loopback requests"})
			return
		}
		origin := r.Header.Get("Origin")
		if origin != "" {
			u, err := url.Parse(origin)
			valid := err == nil && u.User == nil && u.Path == "" && u.RawQuery == "" && u.Fragment == "" && (u.Scheme == "https" || u.Scheme == "http") && (u.Host == r.Host || allowed[origin] || config.Dev && loopbackHost(u.Host))
			if !valid {
				writeJSON(w, http.StatusForbidden, Object{"code": "permission_denied", "message": "origin is not allowed"})
				return
			}
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Add("Vary", "Origin")
			w.Header().Set("Access-Control-Allow-Headers", "Authorization,Content-Type,Connect-Protocol-Version,Connect-Timeout-Ms,Grpc-Timeout,X-Grpc-Web,X-User-Agent,MCP-Protocol-Version")
			w.Header().Set("Access-Control-Allow-Methods", "GET,HEAD,POST,PUT,DELETE,OPTIONS")
			w.Header().Set("Access-Control-Expose-Headers", "Grpc-Status,Grpc-Message,Connect-Content-Encoding,MCP-Protocol-Version,X-Content-SHA256,ETag,Content-Length")
		}
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		var workspace Workspace
		actorName := "workspace"
		if config.Workspaces != nil && r.Header.Get(IdentityHeader) != "" {
			identity, err := verifyBridge(r, config.IdentityBridgeKey, time.Now())
			if err != nil {
				writeJSON(w, http.StatusUnauthorized, Object{"code": "unauthenticated", "message": "a valid service identity is required"})
				return
			}
			workspace, err = config.Workspaces.resolve(r.Context(), identity)
			if err != nil {
				writeJSON(w, http.StatusServiceUnavailable, Object{"code": "unavailable", "message": "Your private workspace could not be opened. Retry shortly."})
				return
			}
			actorName = "user:" + identity.UID
		} else if !config.Dev {
			provided := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			hash := sha256.Sum256([]byte(provided))
			if !strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ") || subtle.ConstantTimeCompare(hash[:], expected[:]) != 1 {
				w.Header().Set("WWW-Authenticate", `Bearer realm="annotation"`)
				writeJSON(w, http.StatusUnauthorized, Object{"code": "unauthenticated", "message": "a valid annotation bearer token is required"})
				return
			}
			if config.Workspaces != nil {
				workspace = config.Workspaces.legacy()
			}
		}
		if config.Dev {
			actorName = "local-dev"
		}
		ctx, cancel := context.WithTimeout(context.WithValue(r.Context(), actorKey{}, actorName), 30*time.Second)
		defer cancel()
		if config.Workspaces != nil {
			ctx = context.WithValue(ctx, workspaceKey{}, workspace)
		}
		bodyLimit := int64(MaxRequestBytes)
		if strings.HasPrefix(r.URL.Path, "/api/v1/assets/") {
			bodyLimit = MaxAssetBytes
		}
		r.Body = http.MaxBytesReader(w, r.Body, bodyLimit)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, err error) {
	status, code, message := http.StatusInternalServerError, "internal", "annotation storage request failed"
	switch {
	case errors.Is(err, ErrInvalid):
		status, code, message = http.StatusBadRequest, "invalid_argument", err.Error()
	case errors.Is(err, ErrNotFound):
		status, code, message = http.StatusNotFound, "not_found", err.Error()
	case errors.Is(err, ErrConflict):
		status, code, message = http.StatusConflict, "aborted", err.Error()
	case errors.Is(err, context.DeadlineExceeded):
		status, code, message = http.StatusGatewayTimeout, "deadline_exceeded", "request timed out"
	}
	writeJSON(w, status, Object{"code": code, "message": message})
}

func (s *Service) serveREST(w http.ResponseWriter, r *http.Request) {
	method := ""
	data := Object{}
	for key, values := range r.URL.Query() {
		if len(values) == 0 {
			continue
		}
		if key == "limit" || key == "expectedRevision" {
			n, err := strconv.ParseInt(values[0], 10, 64)
			if err != nil {
				writeError(w, invalid("%s must be an integer", key))
				return
			}
			data[key] = n
		} else {
			data[key] = values[0]
		}
	}
	if after, ok := data["after"]; ok {
		data["cursor"] = after
		delete(data, "after")
	}
	if r.Method == http.MethodPost || r.Method == http.MethodPut {
		decoder := json.NewDecoder(r.Body)
		if err := decoder.Decode(&data); err != nil {
			writeError(w, invalid("request must contain one JSON object"))
			return
		}
		if decoder.Decode(new(any)) != io.EOF {
			writeError(w, invalid("request must contain one JSON object"))
			return
		}
	}
	path := strings.Trim(strings.TrimPrefix(r.URL.Path, "/api/v1/"), "/")
	parts := strings.Split(path, "/")
	if len(parts) == 2 && parts[0] == "rpc" && r.Method == http.MethodPost {
		method = parts[1]
	} else {
		switch {
		case path == "info" && r.Method == http.MethodGet:
			method = "GetInfo"
		case path == "projects" && r.Method == http.MethodGet:
			method = "ListProjects"
		case path == "projects" && r.Method == http.MethodPost:
			method = "SaveProject"
		case path == "items" && r.Method == http.MethodGet:
			method = "ListItems"
		case path == "items" && r.Method == http.MethodPost:
			method = "SaveItem"
		case path == "labels" && r.Method == http.MethodGet:
			method = "ListLabels"
		case path == "labels" && r.Method == http.MethodPost:
			method = "SaveLabel"
		case path == "changes" && r.Method == http.MethodGet:
			method = "ListChanges"
		case path == "export" && r.Method == http.MethodGet:
			method = "ExportProject"
		case len(parts) == 3 && parts[0] == "projects" && parts[2] == "items" && r.Method == http.MethodGet:
			method = "ListItems"
			data["projectId"] = parts[1]
		case len(parts) == 3 && parts[0] == "projects" && parts[2] == "labels" && r.Method == http.MethodGet:
			method = "ListLabels"
			data["projectId"] = parts[1]
		case len(parts) == 2 && parts[0] == "items" && r.Method == http.MethodGet:
			method = "GetItem"
			data["id"] = parts[1]
		case len(parts) == 2 && parts[0] == "items" && r.Method == http.MethodDelete:
			method = "DeleteItem"
			data["id"] = parts[1]
		case len(parts) == 2 && parts[0] == "labels" && r.Method == http.MethodDelete:
			method = "DeleteLabel"
			data["id"] = parts[1]
		case len(parts) == 2 && (parts[0] == "items" || parts[0] == "labels" || parts[0] == "projects") && r.Method == http.MethodPut:
			entity := strings.TrimSuffix(parts[0], "s")
			doc, ok := data[entity].(map[string]any)
			if !ok {
				writeError(w, invalid("%s object is required", entity))
				return
			}
			if id, ok := doc["id"].(string); ok && id != parts[1] {
				writeError(w, invalid("path and document ids differ"))
				return
			}
			doc["id"] = parts[1]
			method = "Save" + strings.ToUpper(entity[:1]) + entity[1:]
		}
	}
	if method == "" {
		http.NotFound(w, r)
		return
	}
	result, err := s.Call(r.Context(), method, data)
	if err != nil {
		writeError(w, err)
		return
	}
	if method == "ExportProject" && r.URL.Query().Get("format") == "ndjson" {
		writeNDJSON(w, result)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func writeNDJSON(w http.ResponseWriter, export Object) {
	w.Header().Set("Content-Type", "application/x-ndjson")
	enc := json.NewEncoder(w)
	_ = enc.Encode(Object{"kind": "project", "document": export["project"]})
	for _, kind := range []string{"labels", "items"} {
		if values, ok := export[kind].([]any); ok {
			for _, doc := range values {
				_ = enc.Encode(Object{"kind": strings.TrimSuffix(kind, "s"), "document": doc})
			}
		}
	}
	_ = enc.Encode(Object{"kind": "checkpoint", "schemaVersion": export["schemaVersion"], "cursor": export["cursor"]})
}
