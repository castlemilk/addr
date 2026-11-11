// Command annotate provides the Fieldwork CLI and an MCP stdio bridge.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"time"

	"github.com/benebsworth/addr/internal/annotation/migration"
	"github.com/benebsworth/addr/internal/annotationsync"
)

const maxInputBytes = 64 << 20

type client struct {
	baseURL string
	token   string
	http    *http.Client
}

type options struct {
	url, tokenFile, file, project, id, cursor, query, status, adapter, format, imageURL, directory string
	snapshot, localURL, localTokenFile, planID                                                     string
	root, scenes, runID, reviewHistory, feedbackReceipts, pushReceipts                             string
	all, dryRun, markSuperseded, skipOutsideInventory, allowUnrecovered, allowLive                 bool
	concurrency                                                                                    int
	revision                                                                                       int64
	limit                                                                                          int
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	if err := run(ctx, os.Args[1:], os.Stdin, os.Stdout, os.Stderr); err != nil {
		fmt.Fprintln(os.Stderr, "annotate:", err)
		os.Exit(1)
	}
}

func run(ctx context.Context, args []string, input io.Reader, output, diagnostics io.Writer) error {
	if len(args) == 0 || args[0] == "help" || args[0] == "--help" {
		_, err := fmt.Fprintln(output, helpText)
		return err
	}
	command := args[0]
	args = args[1:]
	method := ""
	if command == "rpc" {
		if len(args) == 0 || strings.HasPrefix(args[0], "-") {
			return errors.New("rpc requires an API method, for example: annotate rpc GetInfo")
		}
		method, args = args[0], args[1:]
	}
	var o options
	fs := flag.NewFlagSet(command, flag.ContinueOnError)
	fs.SetOutput(diagnostics)
	fs.StringVar(&o.url, "url", envOr("ANNOTATION_URL", "http://127.0.0.1:8080"), "Fieldwork server URL")
	fs.StringVar(&o.tokenFile, "token-file", "", "read the workspace token from a file")
	fs.StringVar(&o.file, "file", "-", "JSON input file, or - for stdin")
	fs.StringVar(&o.directory, "dir", "", "local mirror directory (sync)")
	fs.StringVar(&o.snapshot, "snapshot", "", "frozen snapshot bundle (feedback)")
	fs.StringVar(&o.localURL, "local-url", "http://localhost:3008", "local scene API URL (feedback preview/apply)")
	fs.StringVar(&o.localTokenFile, "local-token-file", "", "separate local scene API bearer token file")
	fs.StringVar(&o.planID, "plan-id", "", "preview plan ID required for feedback apply")
	fs.StringVar(&o.project, "project", "", "project ID")
	fs.StringVar(&o.id, "id", "", "item, label, or annotation ID")
	fs.Int64Var(&o.revision, "revision", 0, "expected revision; 0 creates a new document")
	fs.StringVar(&o.cursor, "cursor", "", "pagination or change cursor")
	fs.StringVar(&o.cursor, "after", "", "change cursor (alias for --cursor)")
	fs.IntVar(&o.limit, "limit", 20, "page size")
	fs.StringVar(&o.query, "query", "", "text search")
	fs.StringVar(&o.status, "status", "", "review status")
	fs.StringVar(&o.adapter, "adapter", "", "adapter ID from annotate info")
	fs.StringVar(&o.format, "format", "json", "json or ndjson (export/import)")
	fs.StringVar(&o.imageURL, "image-url", "/examples/fieldwork-scene.svg", "image URL for explicit example seed")
	fs.StringVar(&o.root, "root", ".", "repository root holding data/scenes (push)")
	fs.StringVar(&o.scenes, "scenes", "", "comma-separated scene ids (push; default all local scenes)")
	fs.StringVar(&o.runID, "run", "", "apply or resume a saved push plan by run id")
	fs.StringVar(&o.reviewHistory, "review-history", "omit", "omit or split: publish review.json history as a separate asset (push)")
	fs.StringVar(&o.feedbackReceipts, "feedback-receipts", "", "local app feedback receipts root (default ROOT/data/reviews/fieldwork-feedback)")
	fs.StringVar(&o.pushReceipts, "push-receipts", "", "push receipts directory read as baselines by feedback (default DIR/push; a missing or empty directory keeps the schema-1 packet)")
	fs.BoolVar(&o.all, "all", false, "print current scenes in the push table too")
	fs.BoolVar(&o.dryRun, "dry-run", false, "plan only; no SaveItem, SaveLabel or asset PUT (push)")
	fs.BoolVar(&o.markSuperseded, "mark-superseded", true, "tag older items of a pushed scene as superseded-frame (push)")
	fs.BoolVar(&o.skipOutsideInventory, "skip-outside-inventory", false, "omit local scenes absent from the frozen inventory (push)")
	fs.BoolVar(&o.allowUnrecovered, "allow-unrecovered", false, "push over unpulled Fieldwork edits and record them as unrecoveredFieldwork (push)")
	fs.IntVar(&o.concurrency, "concurrency", 4, "parallel scene uploads (push)")
	fs.BoolVar(&o.allowLive, "allow-live", false, "permit a write command against a non-loopback Fieldwork origin (or set FIELDWORK_PUSH_ALLOW_LIVE=1); reads, --dry-run and loopback --dev servers never need it")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if fs.NArg() != 0 {
		return fmt.Errorf("unexpected argument %q", fs.Arg(0))
	}
	if command == "feedback-preview" || command == "feedback-apply" {
		return localFeedback(ctx, command, o, input, output)
	}
	if command == "push-status" {
		return pushStatus(o, output)
	}
	c, err := newClient(o.url, o.tokenFile)
	if err != nil {
		return err
	}
	if writesRemote(command, method, o) {
		if err := migration.GuardLiveWrite(c.baseURL, "annotate "+strings.TrimSpace(command+" "+method), o.allowLive); err != nil {
			return err
		}
	}
	request := map[string]any{}
	switch command {
	case "feedback":
		return c.feedback(ctx, o, output)
	case "push":
		if !o.dryRun && migration.IsReviewProject(o.project) {
			return migration.ReviewWriteRefusal("annotate push", o.project)
		}
		return c.push(ctx, o, output, diagnostics)
	case "info":
		method = "GetInfo"
	case "projects", "items", "labels":
		method = map[string]string{"projects": "ListProjects", "items": "ListItems", "labels": "ListLabels"}[command]
		if command != "projects" && o.project == "" {
			return errors.New("--project is required")
		}
		request = map[string]any{"projectId": o.project, "cursor": o.cursor, "limit": o.limit, "query": o.query, "status": o.status, "adapter": o.adapter}
	case "get":
		if o.id == "" {
			return errors.New("--id is required")
		}
		method, request = "GetItem", map[string]any{"id": o.id}
	case "save-project", "save-item", "save-label":
		kind := strings.TrimPrefix(command, "save-")
		method = map[string]string{"project": "SaveProject", "item": "SaveItem", "label": "SaveLabel"}[kind]
		document, err := readDocument(o.file, input)
		if err != nil {
			return err
		}
		request = map[string]any{kind: document, "expectedRevision": o.revision}
	case "delete-item", "delete-label":
		if o.id == "" || o.revision <= 0 {
			return errors.New("--id and a positive --revision are required")
		}
		method = map[string]string{"delete-item": "DeleteItem", "delete-label": "DeleteLabel"}[command]
		request = map[string]any{"id": o.id, "expectedRevision": o.revision}
	case "changes":
		method, request = "ListChanges", map[string]any{"projectId": o.project, "cursor": o.cursor, "limit": o.limit}
	case "sync":
		if o.project == "" || o.directory == "" {
			return errors.New("sync requires --project and --dir")
		}
		if o.cursor != "" {
			return errors.New("sync manages its own checkpoint; use a new directory to start at cursor 0")
		}
		summary, err := annotationsync.Sync(ctx, c.fetchChanges, c.baseURL, o.project, o.directory, o.limit)
		if err != nil {
			return err
		}
		return json.NewEncoder(output).Encode(summary)
	case "export":
		if o.project == "" {
			return errors.New("--project is required")
		}
		if o.format != "json" && o.format != "ndjson" {
			return errors.New("--format must be json or ndjson")
		}
		data, err := c.call(ctx, "ExportProject", map[string]any{"projectId": o.project})
		if err != nil {
			var api *apiError
			if !errors.As(err, &api) || api.Status != http.StatusBadRequest || !strings.Contains(api.Message, "export exceeds 32 MiB") {
				return err
			}
			// Large projects use the same committed change feed as incremental
			// sync. The resulting snapshot and cursor have the usual export shape.
			snapshot, _, err := annotationsync.Replay(ctx, c.fetchChanges, o.project, annotationsync.Snapshot{}, o.limit)
			if err != nil {
				return err
			}
			if o.format == "json" {
				return json.NewEncoder(output).Encode(snapshot)
			}
			return writeNDJSONExport(output, snapshot)
		}
		return writeExport(output, data, o.format)
	case "import":
		data, err := readInput(o.file, input)
		if err != nil {
			return err
		}
		return c.importProject(ctx, data, o.format, output)
	case "seed":
		return c.seed(ctx, o.imageURL, output)
	case "mcp":
		return c.mcp(ctx, input, output, diagnostics)
	case "rpc":
		if strings.ContainsAny(method, "/?#") {
			return errors.New("invalid RPC method name")
		}
		data, err := readDocument(o.file, input)
		if err != nil {
			return err
		}
		if err := json.Unmarshal(data, &request); err != nil {
			return err
		}
	default:
		return fmt.Errorf("unknown command %q; run annotate help", command)
	}
	data, err := c.call(ctx, method, request)
	if err != nil {
		return err
	}
	return writeJSON(output, data)
}

// writesRemote names the commands that can mutate a Fieldwork project, so the
// live-origin guard runs before any request. rpc is fail-closed: only known
// read methods skip the guard. The mcp bridge forwards whatever the connected
// client sends and is not covered here; its writes to a review project are
// refused in mcp.go (reviewguard.go), as every annotate write there is.
func writesRemote(command, method string, o options) bool {
	switch command {
	case "push":
		return !o.dryRun
	case "save-project", "save-item", "save-label", "delete-item", "delete-label", "import", "seed":
		return true
	case "rpc":
		switch method {
		case "GetInfo", "GetProject", "ListProjects", "GetItem", "ListItems", "GetLabel", "ListLabels", "GetAnnotation", "ListAnnotations", "ListChanges", "ExportProject":
			return false
		}
		return true
	}
	return false
}

func newClient(baseURL, tokenFile string) (*client, error) {
	u, err := url.Parse(baseURL)
	if err != nil || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, errors.New("--url must be a server URL without credentials, query, or fragment")
	}
	if u.Scheme != "https" {
		ip := net.ParseIP(u.Hostname())
		if u.Scheme != "http" || !(u.Hostname() == "localhost" || ip != nil && ip.IsLoopback()) {
			return nil, errors.New("use HTTPS for remote servers; HTTP is supported only on localhost")
		}
	}
	token := os.Getenv("ANNOTATION_TOKEN")
	if tokenFile != "" {
		data, err := os.ReadFile(tokenFile)
		if err != nil {
			return nil, fmt.Errorf("read token file: %w", err)
		}
		token = strings.TrimSpace(string(data))
	}
	return &client{baseURL: strings.TrimRight(baseURL, "/"), token: token, http: &http.Client{
		Timeout:       60 * time.Second,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse },
	}}, nil
}

type apiError struct {
	Status  int
	Message string
}

func (e *apiError) Error() string { return fmt.Sprintf("HTTP %d: %s", e.Status, e.Message) }

func (c *client) post(ctx context.Context, path string, data []byte, session string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(data))
	if err != nil {
		return nil, errors.New("cannot create API request")
	}
	req.Header.Set("Content-Type", "application/json")
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	if path == "/mcp" {
		req.Header.Set("Accept", "application/json, text/event-stream")
		req.Header.Set("MCP-Protocol-Version", "2025-11-25")
		if session != "" {
			req.Header.Set("Mcp-Session-Id", session)
		}
	} else {
		req.Header.Set("Accept", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("server request failed: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		defer resp.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 8192))
		message := http.StatusText(resp.StatusCode)
		var detail struct {
			Message string `json:"message"`
			Error   string `json:"error"`
		}
		if json.Unmarshal(body, &detail) == nil {
			if detail.Message != "" {
				message = detail.Message
			} else if detail.Error != "" {
				message = detail.Error
			}
		}
		if resp.StatusCode == http.StatusUnauthorized {
			message += "; set ANNOTATION_TOKEN or --token-file"
		}
		return nil, &apiError{resp.StatusCode, message}
	}
	return resp, nil
}

func (c *client) call(ctx context.Context, method string, request any) (json.RawMessage, error) {
	if err := c.guardReviewWrite(ctx, method, request); err != nil {
		return nil, err
	}
	body, err := json.Marshal(request)
	if err != nil {
		return nil, err
	}
	resp, err := c.post(ctx, "/api/v1/rpc/"+method, body, "")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxInputBytes+1))
	if err != nil {
		return nil, err
	}
	if len(data) > maxInputBytes {
		return nil, errors.New("API response exceeds 64 MiB")
	}
	if !json.Valid(data) {
		return nil, errors.New("server returned invalid JSON")
	}
	return data, nil
}

func readInput(filename string, input io.Reader) ([]byte, error) {
	if filename != "-" {
		f, err := os.Open(filename)
		if err != nil {
			return nil, err
		}
		defer f.Close()
		input = f
	}
	data, err := io.ReadAll(io.LimitReader(input, maxInputBytes+1))
	if err != nil {
		return nil, err
	}
	if len(data) > maxInputBytes {
		return nil, errors.New("input exceeds 64 MiB")
	}
	return data, nil
}

func readDocument(filename string, input io.Reader) (json.RawMessage, error) {
	data, err := readInput(filename, input)
	if err != nil {
		return nil, err
	}
	var object map[string]json.RawMessage
	if json.Unmarshal(data, &object) != nil || object == nil {
		return nil, errors.New("input must be one JSON object")
	}
	return data, nil
}

func writeJSON(output io.Writer, data json.RawMessage) error {
	var pretty bytes.Buffer
	if err := json.Indent(&pretty, data, "", "  "); err != nil {
		return err
	}
	pretty.WriteByte('\n')
	_, err := pretty.WriteTo(output)
	return err
}

type exportDocument = annotationsync.Snapshot
type exportLine struct {
	Kind          string          `json:"kind"`
	Document      json.RawMessage `json:"document,omitempty"`
	SchemaVersion int             `json:"schemaVersion,omitempty"`
	Cursor        string          `json:"cursor,omitempty"`
}

func writeExport(output io.Writer, data json.RawMessage, format string) error {
	if format == "json" {
		return json.NewEncoder(output).Encode(data)
	}
	var snapshot exportDocument
	if err := json.Unmarshal(data, &snapshot); err != nil {
		return err
	}
	return writeNDJSONExport(output, snapshot)
}

func writeNDJSONExport(output io.Writer, snapshot exportDocument) error {
	encoder := json.NewEncoder(output)
	if err := encoder.Encode(exportLine{Kind: "project", Document: snapshot.Project}); err != nil {
		return err
	}
	for _, doc := range snapshot.Labels {
		if err := encoder.Encode(exportLine{Kind: "label", Document: doc}); err != nil {
			return err
		}
	}
	for _, doc := range snapshot.Items {
		if err := encoder.Encode(exportLine{Kind: "item", Document: doc}); err != nil {
			return err
		}
	}
	return encoder.Encode(exportLine{Kind: "checkpoint", SchemaVersion: snapshot.SchemaVersion, Cursor: snapshot.Cursor})
}

func (c *client) fetchChanges(ctx context.Context, project, cursor string, limit int) (annotationsync.Page, error) {
	data, err := c.call(ctx, "ListChanges", map[string]any{"projectId": project, "cursor": cursor, "limit": limit})
	if err != nil {
		return annotationsync.Page{}, err
	}
	var page annotationsync.Page
	if err := json.Unmarshal(data, &page); err != nil {
		return page, fmt.Errorf("decode change page: %w", err)
	}
	return page, nil
}

func (c *client) importProject(ctx context.Context, data []byte, format string, output io.Writer) error {
	var snapshot exportDocument
	switch format {
	case "json":
		if err := json.Unmarshal(data, &snapshot); err != nil {
			return fmt.Errorf("decode export: %w", err)
		}
	case "ndjson":
		scanner := bufio.NewScanner(bytes.NewReader(data))
		scanner.Buffer(make([]byte, 4096), maxInputBytes)
		for scanner.Scan() {
			if len(bytes.TrimSpace(scanner.Bytes())) == 0 {
				continue
			}
			var line exportLine
			if err := json.Unmarshal(scanner.Bytes(), &line); err != nil {
				return fmt.Errorf("decode NDJSON: %w", err)
			}
			switch line.Kind {
			case "checkpoint", "manifest":
				snapshot.SchemaVersion = line.SchemaVersion
			case "project":
				if snapshot.Project != nil {
					return errors.New("import supports one project per file")
				}
				snapshot.Project = line.Document
			case "label":
				snapshot.Labels = append(snapshot.Labels, line.Document)
			case "item":
				snapshot.Items = append(snapshot.Items, line.Document)
			default:
				return fmt.Errorf("unsupported import kind %q", line.Kind)
			}
		}
		if err := scanner.Err(); err != nil {
			return err
		}
	default:
		return errors.New("--format must be json or ndjson")
	}
	if snapshot.SchemaVersion != 1 || len(snapshot.Project) == 0 {
		return errors.New("expected a Fieldwork export with schemaVersion 1 and a project")
	}
	entries := []exportLine{{Kind: "project", Document: snapshot.Project}}
	for _, doc := range snapshot.Labels {
		entries = append(entries, exportLine{Kind: "label", Document: doc})
	}
	for _, doc := range snapshot.Items {
		entries = append(entries, exportLine{Kind: "item", Document: doc})
	}
	// Every write creates with revision 0: imports cannot overwrite existing work.
	for index, entry := range entries {
		method := map[string]string{"project": "SaveProject", "label": "SaveLabel", "item": "SaveItem"}[entry.Kind]
		if _, err := c.call(ctx, method, map[string]any{entry.Kind: entry.Document, "expectedRevision": 0}); err != nil {
			return fmt.Errorf("import stopped after %d documents; prior creates remain saved: %w", index, err)
		}
	}
	return json.NewEncoder(output).Encode(map[string]any{"imported": len(entries), "items": len(snapshot.Items), "labels": len(snapshot.Labels)})
}

func (c *client) seed(ctx context.Context, imageURL string, output io.Writer) error {
	const projectID = "example-fieldwork"
	cursor := ""
	for {
		data, err := c.call(ctx, "ListProjects", map[string]any{"cursor": cursor, "limit": 500})
		if err != nil {
			return err
		}
		var page struct {
			Projects []struct {
				ID string `json:"id"`
			} `json:"projects"`
			NextCursor string `json:"nextCursor"`
		}
		if err := json.Unmarshal(data, &page); err != nil {
			return err
		}
		for _, project := range page.Projects {
			if project.ID == projectID {
				return json.NewEncoder(output).Encode(map[string]any{"skipped": true, "projectId": projectID, "reason": "Example project already exists; existing work preserved."})
			}
		}
		if page.NextCursor == "" {
			break
		}
		cursor = page.NextCursor
	}
	project := map[string]any{"id": projectID, "name": "Example — Fieldwork", "description": "Illustrative map and image annotations. These examples are synthetic and are not imported address or ML labels."}
	label := map[string]any{"id": "example-region", "projectId": projectID, "name": "Example region", "color": "#3a7054"}
	mapItem := map[string]any{"id": "example-map", "projectId": projectID, "title": "Example — Melbourne map", "adapter": "map", "source": map[string]any{"center": []float64{144.9631, -37.8136}, "zoom": 15}, "metadata": map[string]any{"example": true}, "tags": []string{"example"}, "status": "unreviewed", "notes": "Illustrative area for trying the map adapter; not a verified property boundary.", "annotations": []any{map[string]any{"id": "example-map-region", "labelId": "example-region", "geometry": map[string]any{"type": "Polygon", "coordinates": [][][]float64{{{144.9618, -37.8130}, {144.9632, -37.8130}, {144.9632, -37.8140}, {144.9618, -37.8140}, {144.9618, -37.8130}}}}, "notes": "Synthetic example polygon"}}}
	imageItem := map[string]any{"id": "example-image", "projectId": projectID, "title": "Example — Image workspace", "adapter": "image", "source": map[string]any{"url": imageURL}, "metadata": map[string]any{"example": true}, "tags": []string{"example"}, "status": "unreviewed", "notes": "An illustrative image for trying normalized points, polygons, labels and crops.", "annotations": []any{map[string]any{"id": "example-image-region", "labelId": "example-region", "geometry": map[string]any{"type": "Polygon", "coordinates": [][][]float64{{{0.25, 0.25}, {0.65, 0.25}, {0.65, 0.65}, {0.25, 0.65}, {0.25, 0.25}}}}, "notes": "Synthetic example polygon"}}}
	for _, entry := range []struct {
		method, kind string
		document     any
	}{{"SaveProject", "project", project}, {"SaveLabel", "label", label}, {"SaveItem", "item", mapItem}, {"SaveItem", "item", imageItem}} {
		if _, err := c.call(ctx, entry.method, map[string]any{entry.kind: entry.document, "expectedRevision": 0}); err != nil {
			return fmt.Errorf("seed %s failed; prior example creates remain saved: %w", entry.kind, err)
		}
	}
	return json.NewEncoder(output).Encode(map[string]any{"projectId": projectID, "items": 2, "labels": 1, "example": true})
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

const helpText = `Fieldwork annotation CLI

Set ANNOTATION_URL and ANNOTATION_TOKEN, or pass --url and --token-file.
All output is JSON; errors go to stderr and return a nonzero exit status.

  annotate info
  annotate projects [--query TEXT] [--cursor CURSOR]
  annotate items --project ID [--adapter ID] [--status STATUS]  (discover IDs with annotate info)
  annotate get --id ID
  annotate labels --project ID
  annotate save-project --file project.json [--revision N]
  annotate save-item --file item.json [--revision N]
  annotate save-label --file label.json [--revision N]
  annotate delete-item --id ID --revision N
  annotate delete-label --id ID --revision N
  annotate changes [--project ID] [--after CURSOR] [--limit N]
  annotate sync --project ID --dir PATH [--limit N]
  annotate feedback --project ID --dir PATH --snapshot BUNDLE --file PACKET [--id ITEM_ID] [--push-receipts DIR/push]
  annotate feedback-preview --file PACKET [--id ITEM_ID] [--local-url http://localhost:3008] [--local-token-file PATH]
  annotate feedback-apply --file PACKET --plan-id ID [--id ITEM_ID] [--local-url URL] [--local-token-file PATH]
  annotate push --project ID --dir PATH [--root .] [--scenes a,b] [--dry-run] [--run RUNID]
                [--mark-superseded=true] [--skip-outside-inventory] [--allow-unrecovered]
                [--review-history omit|split] [--concurrency 4] [--feedback-receipts PATH] [--all]
                [--allow-live]
  annotate push-status --dir PATH [--scenes a,b]
  annotate export --project ID [--format json|ndjson]
  annotate import --file export.json [--format json|ndjson]
  annotate seed [--image-url /examples/fieldwork-scene.svg]
  annotate rpc METHOD --file request.json
  annotate mcp

Use --file - to read JSON from stdin. List/changes commands return one page and
its nextCursor. Save revision 0 creates; pass the current revision to update.
Sync pulls documents and deletions into PATH/mirror.json and resumes its saved
cursor. The mirror is bound to this server URL and project; writes are atomic.
Feedback syncs the mirror and writes a verified packet of current scene edits;
it never changes local reviews or cloud data. Repeated pulls retain unapplied edits.
When push receipts exist (DIR/push, or --push-receipts) the packet is schema 2:
items our push wrote are compared against their receipt (own writes skipped,
generation items baselined on the receipt document, each entry bound to its
receipt); without receipts the packet stays schema 1.
Local preview/apply use separate credentials and never read ANNOTATION_TOKEN.
Push syncs the mirror, plans every local scene (table on stderr, JSON on stdout),
then creates one new immutable item per changed scene and marks the older items
superseded-frame. It never edits Fieldwork overrides, never deletes, and refuses
scenes with unpulled Fieldwork edits. --dry-run plans only; --run resumes a plan.
Pushed values are model estimates plus local review state, not human labels.
Write commands (push without --dry-run, save-*, delete-*, import, seed, and rpc
methods other than the known reads) refuse any origin that is not localhost or a
loopback IP unless --allow-live is passed or FIELDWORK_PUSH_ALLOW_LIVE=1 is set.
Export automatically pages the change feed when a project exceeds 32 MiB.
Imports create documents only and stop on conflicts without overwriting.
Seed adds explicitly labelled examples and preserves an existing example project.
The mcp command proxies newline-delimited JSON-RPC over authenticated HTTP.
Agents never edit a review project (an id ending in -review, e.g.
mooroolbark-review): the reviewer marks it in the Fieldwork UI and
scripts/ml/fieldwork_review.py pull reads those marks as the reviewer's. Every
write to such a project is refused here (save-*, delete-*, rpc, import, push and
the mcp bridge's write tools); only fieldwork_review.py push creates its items.`
