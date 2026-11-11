package migration

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/benebsworth/addr/internal/annotation"
)

type Client struct {
	baseURL, token string
	http           *http.Client
}
type APIError struct {
	Status  int
	Message string
	Headers http.Header
}

func (e *APIError) Error() string { return fmt.Sprintf("HTTP %d: %s", e.Status, e.Message) }

func NewClient(baseURL, token string) (*Client, error) {
	u, err := url.Parse(baseURL)
	if err != nil || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Path != "" && u.Path != "/" {
		return nil, fmt.Errorf("URL must be an HTTPS server origin or loopback HTTP origin")
	}
	if u.Scheme != "https" {
		ip := net.ParseIP(u.Hostname())
		if u.Scheme != "http" || !(u.Hostname() == "localhost" || ip != nil && ip.IsLoopback()) {
			return nil, fmt.Errorf("remote servers require HTTPS")
		}
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxIdleConns = 64
	transport.MaxIdleConnsPerHost = 64
	transport.MaxConnsPerHost = 64
	// A maximum-size asset can take several minutes on an asymmetric uplink.
	// API calls retain a shorter deadline below; redirects remain forbidden.
	return &Client{baseURL: strings.TrimRight(baseURL, "/"), token: token, http: &http.Client{Transport: transport, Timeout: 10 * time.Minute, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

func (c *Client) request(ctx context.Context, method, path, contentType string, reader io.Reader, length int64, extraHeaders ...http.Header) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reader)
	if err != nil {
		return nil, err
	}
	if reader != nil {
		req.ContentLength = length
		if length == 0 {
			req.Body = http.NoBody
		}
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Accept-Encoding", "identity")
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	for _, headers := range extraHeaders {
		for key, values := range headers {
			req.Header[key] = append([]string(nil), values...)
		}
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("Fieldwork request failed: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		defer resp.Body.Close()
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		var api struct {
			Message string `json:"message"`
		}
		_ = json.Unmarshal(data, &api)
		if api.Message == "" {
			api.Message = http.StatusText(resp.StatusCode)
		}
		return nil, &APIError{Status: resp.StatusCode, Message: api.Message, Headers: resp.Header.Clone()}
	}
	return resp, nil
}

func (c *Client) call(ctx context.Context, method string, input any) (annotation.Object, error) {
	ctx, cancel := context.WithTimeout(ctx, 120*time.Second)
	defer cancel()
	data, err := json.Marshal(input)
	if err != nil {
		return nil, err
	}
	if err := RefuseDeclaredReviewWrite(method, data); err != nil {
		return nil, err
	}
	resp, err := c.request(ctx, http.MethodPost, "/api/v1/rpc/"+method, "application/json", bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var out annotation.Object
	err = json.NewDecoder(io.LimitReader(resp.Body, 9<<20)).Decode(&out)
	return out, err
}

// Call issues one JSON RPC method against the Fieldwork API.
func (c *Client) Call(ctx context.Context, method string, input any) (annotation.Object, error) {
	return c.call(ctx, method, input)
}

// CallRaw issues one JSON RPC method and returns the undecoded response body,
// preserving the server's exact number spelling for patch-not-replace writes.
func (c *Client) CallRaw(ctx context.Context, method string, input any) (json.RawMessage, error) {
	ctx, cancel := context.WithTimeout(ctx, 120*time.Second)
	defer cancel()
	data, err := json.Marshal(input)
	if err != nil {
		return nil, err
	}
	if err := RefuseDeclaredReviewWrite(method, data); err != nil {
		return nil, err
	}
	resp, err := c.request(ctx, http.MethodPost, "/api/v1/rpc/"+method, "application/json", bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 9<<20+1))
	if err != nil {
		return nil, err
	}
	if len(body) > 9<<20 || !json.Valid(body) {
		return nil, fmt.Errorf("invalid or oversized API response")
	}
	return body, nil
}

// HeadAsset reports whether a content-addressed asset with this exact length exists.
func (c *Client) HeadAsset(ctx context.Context, hash string, size int64) (bool, error) {
	return c.head(ctx, hash, size)
}

// UploadAsset stores an asset if missing; created is true only for a new 201.
func (c *Client) UploadAsset(ctx context.Context, hash string, size int64, contentType string, open func() (io.ReadCloser, error)) (bool, error) {
	return c.upload(ctx, hash, size, contentType, open)
}

// DownloadAsset streams and verifies an asset.
func (c *Client) DownloadAsset(ctx context.Context, hash string, size int64, out io.Writer) error {
	return c.download(ctx, hash, size, out)
}

// Origin is the server base URL this client is bound to.
func (c *Client) Origin() string { return c.baseURL }

// Retry only transport failures and transient server responses. Authentication,
// validation, and integrity failures must remain visible without another write.
func retryAsset(ctx context.Context, operation func() error) error {
	for attempt := 0; ; attempt++ {
		err := operation()
		if err == nil || ctx.Err() != nil || attempt == 3 || !transientAssetError(err) {
			return err
		}
		timer := time.NewTimer(time.Second << attempt)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
}

func transientAssetError(err error) bool {
	var api *APIError
	if errors.As(err, &api) {
		switch api.Status {
		case 408, 425, 429, 500, 502, 503, 504:
			return true
		default:
			return false
		}
	}
	var request *url.Error
	var network net.Error
	return errors.As(err, &request) || errors.As(err, &network) || errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF)
}

func (c *Client) head(ctx context.Context, hash string, size int64) (exists bool, err error) {
	err = retryAsset(ctx, func() error {
		metadata, attemptErr := c.headOnce(ctx, hash, size)
		exists = metadata.exists
		return attemptErr
	})
	return exists, err
}

type assetHead struct {
	exists      bool
	acceptsGzip bool
}

func (c *Client) headOnce(ctx context.Context, hash string, size int64) (assetHead, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	resp, err := c.request(ctx, http.MethodHead, "/api/v1/assets/"+hash, "", nil, 0)
	if err != nil {
		var api *APIError
		if errors.As(err, &api) && api.Status == 404 {
			return assetHead{acceptsGzip: acceptsGzip(api.Headers.Get("Accept-Encoding"))}, nil
		}
		return assetHead{}, err
	}
	resp.Body.Close()
	if resp.ContentLength != size || resp.Header.Get("X-Content-SHA256") != hash {
		return assetHead{}, fmt.Errorf("remote asset metadata mismatch: %s", hash)
	}
	return assetHead{exists: true, acceptsGzip: acceptsGzip(resp.Header.Get("Accept-Encoding"))}, nil
}

func (c *Client) upload(ctx context.Context, hash string, size int64, contentType string, open func() (io.ReadCloser, error)) (created bool, err error) {
	err = retryAsset(ctx, func() error {
		var attemptErr error
		created, attemptErr = c.uploadOnce(ctx, hash, size, contentType, open)
		return attemptErr
	})
	return created, err
}

func (c *Client) uploadOnce(ctx context.Context, hash string, size int64, contentType string, open func() (io.ReadCloser, error)) (bool, error) {
	// Recheck after an ambiguous response: the first request may have committed.
	metadata, err := c.headOnce(ctx, hash, size)
	if err != nil || metadata.exists {
		return false, err
	}
	var reader io.ReadCloser
	wireSize := size
	headers := http.Header{}
	// A server must explicitly advertise request gzip support on this HEAD,
	// including its authenticated 404. Older servers keep receiving raw bytes.
	if metadata.acceptsGzip && size > 256<<10 && size <= annotation.MaxAssetBytes && !strings.HasPrefix(strings.ToLower(contentType), "image/") {
		compressed, compressedSize, err := gzipAsset(ctx, size, open)
		if err != nil {
			return false, err
		}
		if compressed != nil {
			defer os.Remove(compressed.Name())
			reader, wireSize = compressed, compressedSize
			headers.Set("Content-Encoding", "gzip")
			headers.Set("X-Uncompressed-Length", strconv.FormatInt(size, 10))
		}
	}
	if reader == nil {
		reader, err = open()
		if err != nil {
			return false, err
		}
	}
	defer reader.Close()
	resp, err := c.request(ctx, http.MethodPut, "/api/v1/assets/"+hash, contentType, reader, wireSize, headers)
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()
	var result struct {
		SHA256 string `json:"sha256"`
		Bytes  int64  `json:"bytes"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return false, err
	}
	if result.SHA256 != hash || result.Bytes != size {
		return false, fmt.Errorf("upload response integrity mismatch")
	}
	return resp.StatusCode == 201, nil
}

func acceptsGzip(value string) bool {
	for _, entry := range strings.Split(value, ",") {
		parts := strings.Split(entry, ";")
		if !strings.EqualFold(strings.TrimSpace(parts[0]), "gzip") {
			continue
		}
		quality := 1.0
		for _, parameter := range parts[1:] {
			key, value, ok := strings.Cut(strings.TrimSpace(parameter), "=")
			if !ok || !strings.EqualFold(strings.TrimSpace(key), "q") {
				return false
			}
			var err error
			quality, err = strconv.ParseFloat(strings.TrimSpace(value), 64)
			if err != nil || math.IsNaN(quality) || quality < 0 || quality > 1 {
				return false
			}
		}
		return quality > 0
	}
	return false
}

// gzipAsset creates a rewindable body for one attempt. At most one bounded
// source chunk is read; a retry opens it again and produces identical gzip
// bytes. Every failed or unhelpful compression removes its temporary file.
func gzipAsset(ctx context.Context, size int64, open func() (io.ReadCloser, error)) (_ *os.File, compressedSize int64, err error) {
	if err := ctx.Err(); err != nil {
		return nil, 0, err
	}
	if size < 0 || size > annotation.MaxAssetBytes {
		return nil, 0, fmt.Errorf("asset exceeds compression bounds")
	}
	source, err := open()
	if err != nil {
		return nil, 0, err
	}
	defer source.Close()
	file, err := os.CreateTemp("", "fieldwork-upload-*.gz")
	if err != nil {
		return nil, 0, err
	}
	keep := false
	defer func() {
		if !keep {
			file.Close()
			os.Remove(file.Name())
		}
	}()
	compressed, err := gzip.NewWriterLevel(file, gzip.BestSpeed)
	if err != nil {
		return nil, 0, err
	}
	n, copyErr := io.Copy(compressed, io.LimitReader(contextReader{ctx: ctx, reader: source}, size+1))
	closeErr := compressed.Close()
	if copyErr != nil {
		return nil, 0, copyErr
	}
	if closeErr != nil {
		return nil, 0, closeErr
	}
	if n != size {
		return nil, 0, fmt.Errorf("source asset byte length changed during compression")
	}
	info, err := file.Stat()
	if err != nil {
		return nil, 0, err
	}
	// Avoid spending protocol overhead for negligible savings on binary data.
	if info.Size() >= size-size/10 {
		return nil, 0, nil
	}
	if _, err := file.Seek(0, io.SeekStart); err != nil {
		return nil, 0, err
	}
	keep = true
	return file, info.Size(), nil
}

type contextReader struct {
	ctx    context.Context
	reader io.Reader
}

func (r contextReader) Read(p []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	return r.reader.Read(p)
}

type Result struct {
	SnapshotID        string `json:"snapshotId"`
	UploadedAssets    int    `json:"uploadedAssets"`
	ExistingAssets    int    `json:"existingAssets"`
	CreatedDocuments  int    `json:"createdDocuments"`
	ExistingDocuments int    `json:"existingDocuments"`
	VerifiedDocuments int    `json:"verifiedDocuments"`
	VerifiedAssets    int    `json:"verifiedAssets"`
	UploadedBytes     int64  `json:"uploadedBytes"`
	ExistingBytes     int64  `json:"existingBytes"`
}

func parallel(ctx context.Context, keys []string, workers int, work func(context.Context, string) error) error {
	if workers < 1 || workers > 64 {
		return fmt.Errorf("concurrency must be between 1 and 64")
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	jobs := make(chan string)
	var wg sync.WaitGroup
	var first error
	var once sync.Once
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for key := range jobs {
				if ctx.Err() != nil {
					return
				}
				if err := work(ctx, key); err != nil {
					once.Do(func() { first = err; cancel() })
					return
				}
			}
		}()
	}
loop:
	for _, key := range keys {
		select {
		case jobs <- key:
		case <-ctx.Done():
			break loop
		}
	}
	close(jobs)
	wg.Wait()
	if first != nil {
		return first
	}
	return ctx.Err()
}

// Keep large bodies from occupying every connection or starving small assets.
// Both pools together use at most the caller's requested worker count.
func parallelUploads(ctx context.Context, b *Bundle, workers int, work func(context.Context, string) error) error {
	if workers < 1 || workers > 64 {
		return fmt.Errorf("concurrency must be between 1 and 64")
	}
	keys := b.ChunkIDs()
	sort.SliceStable(keys, func(i, j int) bool { return b.Chunks[keys[i]].Bytes < b.Chunks[keys[j]].Bytes })
	var small, large []string
	for _, key := range keys {
		if b.Chunks[key].Bytes > 8<<20 {
			large = append(large, key)
		} else {
			small = append(small, key)
		}
	}
	if workers == 1 || len(large) == 0 {
		return parallel(ctx, keys, workers, work)
	}
	largeWorkers := min(2, workers-1)
	if len(small) == 0 {
		return parallel(ctx, large, largeWorkers, work)
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	errors := make(chan error, 2)
	go func() { errors <- parallel(ctx, small, workers-largeWorkers, work) }()
	go func() { errors <- parallel(ctx, large, largeWorkers, work) }()
	var first error
	for range 2 {
		if err := <-errors; err != nil && first == nil {
			first = err
			cancel()
		}
	}
	return first
}

func (c *Client) Upload(ctx context.Context, b *Bundle, workers int, progress io.Writer) (Result, error) {
	result := Result{SnapshotID: b.Manifest.SnapshotID}
	// A pulled backup also contains assets introduced after the frozen snapshot,
	// including evidence retained only by an earlier or deleted revision.
	var err error
	b, err = bundleWithCatalog(ctx, b)
	if err != nil {
		return result, err
	}
	var lock sync.Mutex
	started := time.Now()
	err = parallelUploads(ctx, b, workers, func(ctx context.Context, hash string) error {
		chunk := b.Chunks[hash]
		created, err := c.upload(ctx, hash, chunk.Bytes, b.ContentTypes[hash], func() (io.ReadCloser, error) { return os.Open(filepath.Join(b.Directory, "blobs", hash)) })
		if err != nil {
			return err
		}
		lock.Lock()
		defer lock.Unlock()
		if created {
			result.UploadedAssets++
			result.UploadedBytes += chunk.Bytes
		} else {
			result.ExistingAssets++
			result.ExistingBytes += chunk.Bytes
		}
		done := result.UploadedAssets + result.ExistingAssets
		if done%100 == 0 && progress != nil {
			fmt.Fprintf(progress, "Assets checked: %d/%d (uploaded %d bytes; reused %d bytes; elapsed %s)\n", done, len(b.Chunks), result.UploadedBytes, result.ExistingBytes, time.Since(started).Round(time.Second))
		}
		return nil
	})
	if err != nil {
		return result, err
	}
	created, err := c.upload(ctx, Digest(b.ManifestBytes), int64(len(b.ManifestBytes)), "application/json", func() (io.ReadCloser, error) { return io.NopCloser(bytes.NewReader(b.ManifestBytes)), nil })
	if created {
		result.UploadedAssets++
		result.UploadedBytes += int64(len(b.ManifestBytes))
	} else if err == nil {
		result.ExistingAssets++
		result.ExistingBytes += int64(len(b.ManifestBytes))
	}
	return result, err
}

// Validate the complete optional pull catalog before sending any upload. Keep
// the immutable manifest and the caller's bundle unchanged; only the transfer
// inventory grows to include independently hash-verified referenced blobs.
func bundleWithCatalog(ctx context.Context, b *Bundle) (*Bundle, error) {
	f, err := os.Open(filepath.Join(b.Directory, "fieldwork-assets.json"))
	if os.IsNotExist(err) {
		return b, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, annotation.MaxAssetBytes+1))
	if err != nil {
		return nil, err
	}
	var catalog struct {
		SchemaVersion int              `json:"schemaVersion"`
		SnapshotID    string           `json:"snapshotId"`
		Assets        []map[string]any `json:"assets"`
		ChunkedFiles  map[string]File  `json:"chunkedFiles"`
	}
	if len(data) > int(annotation.MaxAssetBytes) || json.Unmarshal(data, &catalog) != nil || catalog.SchemaVersion != 1 || catalog.SnapshotID != b.Manifest.SnapshotID || catalog.Assets == nil {
		return nil, fmt.Errorf("invalid asset catalog or snapshot binding")
	}
	refs := &protectedReferences{assets: map[string]*protectedAsset{}, files: catalog.ChunkedFiles}
	for _, asset := range catalog.Assets {
		chunk, err := refs.add(asset, false)
		if err != nil || chunk.Bytes < 0 {
			return nil, fmt.Errorf("asset catalog requires canonical references with exact byte lengths")
		}
	}
	merged := *b
	merged.Chunks, merged.ContentTypes = map[string]Chunk{}, map[string]string{}
	for hash, chunk := range b.Chunks {
		merged.Chunks[hash], merged.ContentTypes[hash] = chunk, b.ContentTypes[hash]
	}
	manifestHash := Digest(b.ManifestBytes)
	for hash, ref := range refs.assets {
		if hash == manifestHash {
			if ref.Bytes != int64(len(b.ManifestBytes)) {
				return nil, fmt.Errorf("asset catalog manifest length mismatch")
			}
			continue // Upload already sends the exact manifest bytes once below.
		}
		chunk := Chunk{hash, ref.Bytes}
		if known, exists := merged.Chunks[hash]; exists && known.Bytes != ref.Bytes {
			return nil, fmt.Errorf("asset catalog conflicts with snapshot: %s", hash)
		}
		if err := b.copyFile(ctx, File{Path: hash, SHA256: hash, Bytes: ref.Bytes, Chunks: []Chunk{chunk}}, io.Discard); err != nil {
			return nil, fmt.Errorf("asset catalog blob %s: %w", hash, err)
		}
		merged.Chunks[hash] = chunk
		if merged.ContentTypes[hash] == "" || merged.ContentTypes[hash] == "application/octet-stream" {
			merged.ContentTypes[hash] = ref.ContentType
			if merged.ContentTypes[hash] == "" {
				merged.ContentTypes[hash] = "application/octet-stream"
			}
		}
	}
	for hash, file := range catalog.ChunkedFiles {
		if !digestPattern.MatchString(hash) || file.SHA256 != hash || file.Bytes < 0 || len(file.Chunks) == 0 {
			return nil, fmt.Errorf("invalid asset catalog chunked file")
		}
		for _, chunk := range file.Chunks {
			ref, exists := refs.assets[chunk.SHA256]
			if !exists || chunk.Bytes != ref.Bytes {
				return nil, fmt.Errorf("asset catalog chunk missing or length mismatch")
			}
		}
		if err := b.copyFile(ctx, file, io.Discard); err != nil {
			return nil, fmt.Errorf("asset catalog chunked file %s: %w", hash, err)
		}
	}
	return &merged, nil
}

func document(value any) (annotation.Object, error) {
	data, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	var doc annotation.Object
	err = json.Unmarshal(data, &doc)
	return doc, err
}
func comparable(doc annotation.Object) annotation.Object {
	out := annotation.Object{}
	for key, value := range doc {
		if key != "revision" && key != "createdAt" && key != "updatedAt" {
			out[key] = value
		}
	}
	return out
}

func (c *Client) list(ctx context.Context, kind, project string) (map[string]annotation.Object, error) {
	methods := map[string]string{"project": "ListProjects", "label": "ListLabels", "item": "ListItems"}
	result := map[string]annotation.Object{}
	cursor := ""
	for {
		data, err := c.call(ctx, methods[kind], annotation.Object{"projectId": project, "cursor": cursor, "limit": 20})
		if err != nil {
			return nil, err
		}
		entries, ok := data[kind+"s"].([]any)
		if !ok {
			return nil, fmt.Errorf("invalid %s list response", kind)
		}
		for _, entry := range entries {
			doc, ok := entry.(map[string]any)
			if !ok {
				return nil, fmt.Errorf("invalid document")
			}
			id, ok := doc["id"].(string)
			if !ok {
				return nil, fmt.Errorf("document has no id")
			}
			result[id] = doc
		}
		next, _ := data["nextCursor"].(string)
		if next == "" {
			return result, nil
		}
		if next == cursor {
			return nil, fmt.Errorf("pagination cursor did not advance")
		}
		cursor = next
	}
}

func desiredDocuments(p *Plan) map[string][]any {
	docs := map[string][]any{"project": {p.Project}, "label": {}, "item": {}}
	for _, label := range p.Labels {
		docs["label"] = append(docs["label"], label)
	}
	for _, item := range p.Items {
		docs["item"] = append(docs["item"], item)
	}
	return docs
}

// Apply preflights every existing document before any write. Creation uses
// expectedRevision=0, so races and non-identical reimports fail closed.
func (c *Client) Apply(ctx context.Context, p *Plan) (Result, error) {
	result := Result{SnapshotID: p.SnapshotID}
	desired := desiredDocuments(p)
	existing := map[string]map[string]annotation.Object{}
	for _, kind := range []string{"project", "label", "item"} {
		docs, err := c.list(ctx, kind, p.Project.ID)
		if err != nil {
			return result, err
		}
		existing[kind] = docs
		for _, value := range desired[kind] {
			want, _ := document(value)
			id := want["id"].(string)
			if old, ok := docs[id]; ok && !equalJSON(comparable(old), comparable(want)) {
				return result, fmt.Errorf("refusing to overwrite changed %s %s; use a separate project for a different snapshot", kind, id)
			}
		}
	}
	for _, kind := range []string{"project", "label", "item"} {
		method := map[string]string{"project": "SaveProject", "label": "SaveLabel", "item": "SaveItem"}[kind]
		for _, value := range desired[kind] {
			want, _ := document(value)
			id := want["id"].(string)
			if _, ok := existing[kind][id]; ok {
				result.ExistingDocuments++
				continue
			}
			if _, err := c.call(ctx, method, annotation.Object{kind: want, "expectedRevision": 0}); err != nil {
				return result, fmt.Errorf("create %s %s: %w", kind, id, err)
			}
			result.CreatedDocuments++
		}
	}
	return result, nil
}

func (c *Client) Verify(ctx context.Context, b *Bundle, p *Plan, workers int, deep bool) (Result, error) {
	result := Result{SnapshotID: p.SnapshotID}
	desired := desiredDocuments(p)
	for _, kind := range []string{"project", "label", "item"} {
		docs, err := c.list(ctx, kind, p.Project.ID)
		if err != nil {
			return result, err
		}
		for _, value := range desired[kind] {
			want, _ := document(value)
			id := want["id"].(string)
			old, ok := docs[id]
			if !ok || !equalJSON(comparable(old), comparable(want)) {
				return result, fmt.Errorf("remote %s %s does not match frozen projection", kind, id)
			}
			result.VerifiedDocuments++
		}
	}
	keys := b.ChunkIDs()
	manifestHash := Digest(b.ManifestBytes)
	keys = append(keys, manifestHash)
	var lock sync.Mutex
	err := parallel(ctx, keys, workers, func(ctx context.Context, hash string) error {
		size := b.Chunks[hash].Bytes
		if hash == manifestHash {
			size = int64(len(b.ManifestBytes))
		}
		exists, err := c.head(ctx, hash, size)
		if err != nil {
			return err
		}
		if !exists {
			return fmt.Errorf("remote asset missing: %s", hash)
		}
		if deep {
			if err := c.download(ctx, hash, size, io.Discard); err != nil {
				return err
			}
		}
		lock.Lock()
		result.VerifiedAssets++
		lock.Unlock()
		return nil
	})
	return result, err
}

func (c *Client) download(ctx context.Context, hash string, size int64, out io.Writer) error {
	var reset func() error
	switch target := out.(type) {
	case *os.File:
		start, err := target.Seek(0, io.SeekCurrent)
		if err != nil {
			return err
		}
		reset = func() error {
			if err := target.Truncate(start); err != nil {
				return err
			}
			_, err := target.Seek(start, io.SeekStart)
			return err
		}
	case *bytes.Buffer:
		start := target.Len()
		reset = func() error { target.Truncate(start); return nil }
	default:
		if out == io.Discard {
			reset = func() error { return nil }
		}
	}
	if reset == nil {
		return c.downloadOnce(ctx, hash, size, out)
	}
	return retryAsset(ctx, func() error {
		if err := reset(); err != nil {
			return err
		}
		return c.downloadOnce(ctx, hash, size, out)
	})
}

func (c *Client) downloadOnce(ctx context.Context, hash string, size int64, out io.Writer) error {
	resp, err := c.request(ctx, http.MethodGet, "/api/v1/assets/"+hash, "", nil, 0)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	h := sha256.New()
	n, err := io.Copy(io.MultiWriter(out, h), io.LimitReader(resp.Body, size+1))
	if err != nil {
		return err
	}
	if n != size || hex.EncodeToString(h.Sum(nil)) != hash {
		return fmt.Errorf("download failed integrity check: %s", hash)
	}
	return nil
}

type protectedAsset struct {
	SHA256      string `json:"sha256"`
	Bytes       int64  `json:"bytes"`
	URL         string `json:"url"`
	ContentType string `json:"contentType,omitempty"`
}

// References in every revision matter: an asset used only by an edited or deleted
// document still belongs in a backup that includes that document's history.
type protectedReferences struct {
	assets map[string]*protectedAsset
	files  map[string]File
}

func referenceSize(value any) (int64, error) {
	if value == nil {
		return -1, nil // An optional size is resolved by an authenticated HEAD.
	}
	n, ok := value.(float64)
	if !ok || math.IsNaN(n) || math.IsInf(n, 0) || n < 0 || n != math.Trunc(n) || n > 1<<53 {
		return 0, fmt.Errorf("asset reference has an invalid byte length")
	}
	return int64(n), nil
}

func (r *protectedReferences) add(ref map[string]any, chunk bool) (Chunk, error) {
	hash, _ := ref["sha256"].(string)
	address, _ := ref["url"].(string)
	if chunk && address == "" {
		address = "/api/v1/assets/" + hash
	}
	if !digestPattern.MatchString(hash) || address != "/api/v1/assets/"+hash {
		return Chunk{}, fmt.Errorf("backup requires a canonical protected asset reference; external or malformed references cannot be preserved")
	}
	size, err := referenceSize(ref["bytes"])
	if err != nil || size > annotation.MaxAssetBytes {
		return Chunk{}, fmt.Errorf("invalid asset size for %s", hash)
	}
	if previous, ok := r.assets[hash]; ok {
		if previous.Bytes >= 0 && size >= 0 && previous.Bytes != size {
			return Chunk{}, fmt.Errorf("conflicting asset lengths for %s", hash)
		}
		if previous.Bytes < 0 {
			previous.Bytes = size
		}
	} else {
		contentType, _ := ref["contentType"].(string)
		r.assets[hash] = &protectedAsset{hash, size, address, contentType}
	}
	return Chunk{hash, size}, nil
}

func (r *protectedReferences) collect(value any) error {
	switch value := value.(type) {
	case map[string]any:
		hash, hasHash := value["sha256"].(string)
		address, _ := value["url"].(string)
		if address != "" && (hasHash || strings.Contains(address, "/api/v1/assets/")) {
			if _, err := r.add(value, false); err != nil {
				return err
			}
		}
		if rawChunks, exists := value["chunks"]; exists && hasHash {
			chunks, ok := rawChunks.([]any)
			if !ok || len(chunks) == 0 || !digestPattern.MatchString(hash) {
				return fmt.Errorf("invalid chunked asset reference")
			}
			size, err := referenceSize(value["bytes"])
			if err != nil {
				return err
			}
			file := File{SHA256: hash, Bytes: size}
			for _, raw := range chunks {
				ref, ok := raw.(map[string]any)
				if !ok {
					return fmt.Errorf("invalid chunk reference")
				}
				chunk, err := r.add(ref, true)
				if err != nil {
					return err
				}
				file.Chunks = append(file.Chunks, chunk)
			}
			if previous, ok := r.files[hash]; ok && !equalJSON(previous, file) {
				return fmt.Errorf("conflicting chunk manifests for %s", hash)
			}
			r.files[hash] = file
		}
		for _, child := range value {
			if err := r.collect(child); err != nil {
				return err
			}
		}
	case []any:
		for _, child := range value {
			if err := r.collect(child); err != nil {
				return err
			}
		}
	}
	return nil
}

func (c *Client) pullReferences(ctx context.Context, directory string, b *Bundle, refs *protectedReferences, workers int) (int, error) {
	keys := []string{}
	manifestHash := Digest(b.ManifestBytes)
	for hash, ref := range refs.assets {
		known, exists := b.Chunks[hash]
		if hash == manifestHash {
			known, exists = Chunk{manifestHash, int64(len(b.ManifestBytes))}, true
		}
		if exists {
			if ref.Bytes >= 0 && ref.Bytes != known.Bytes {
				return 0, fmt.Errorf("asset reference length conflicts with snapshot: %s", hash)
			}
			ref.Bytes = known.Bytes
		} else {
			keys = append(keys, hash)
		}
	}
	sort.Strings(keys)
	err := parallel(ctx, keys, workers, func(ctx context.Context, hash string) error {
		ref := refs.assets[hash]
		if ref.Bytes < 0 {
			response, err := c.request(ctx, http.MethodHead, ref.URL, "", nil, 0)
			if err != nil {
				return err
			}
			response.Body.Close()
			if response.ContentLength < 0 || response.ContentLength > annotation.MaxAssetBytes || response.Header.Get("X-Content-SHA256") != hash {
				return fmt.Errorf("remote asset metadata mismatch: %s", hash)
			}
			ref.Bytes = response.ContentLength
		}
		out, err := os.OpenFile(filepath.Join(directory, "blobs", hash), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if err != nil {
			return err
		}
		err = c.download(ctx, hash, ref.Bytes, out)
		closeErr := out.Close()
		if err != nil {
			return err
		}
		return closeErr
	})
	if err != nil {
		return 0, err
	}
	for hash, file := range refs.files {
		var total int64
		for i := range file.Chunks {
			file.Chunks[i].Bytes = refs.assets[file.Chunks[i].SHA256].Bytes
			total += file.Chunks[i].Bytes
		}
		if file.Bytes < 0 {
			file.Bytes = total
		}
		if err := b.copyFile(ctx, file, io.Discard); err != nil {
			return 0, fmt.Errorf("referenced file %s: %w", hash, err)
		}
		refs.files[hash] = file
	}
	return len(keys), nil
}

// Pull downloads a new self-contained bundle plus the latest editable documents.
// Change replay closes races from paginated reads and emits a durable checkpoint.
func (c *Client) Pull(ctx context.Context, project, directory string, workers int) (annotation.Object, error) {
	if _, err := os.Stat(directory); err == nil {
		return nil, fmt.Errorf("pull destination must not exist")
	} else if !os.IsNotExist(err) {
		return nil, err
	}
	data, err := c.call(ctx, "GetItem", annotation.Object{"id": ReportID(project)})
	if err != nil {
		return nil, err
	}
	item, ok := data["item"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("dataset report missing")
	}
	source, ok := item["source"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("dataset report source missing")
	}
	ref, ok := source["manifest"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("manifest reference missing")
	}
	hash, _ := ref["sha256"].(string)
	size, _ := ref["bytes"].(float64)
	if !digestPattern.MatchString(hash) || size < 0 || size > float64(annotation.MaxAssetBytes) {
		return nil, fmt.Errorf("invalid manifest reference")
	}
	var raw bytes.Buffer
	if err := c.download(ctx, hash, int64(size), &raw); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Join(directory, "blobs"), 0700); err != nil {
		return nil, err
	}
	if err := os.WriteFile(filepath.Join(directory, "manifest.json"), raw.Bytes(), 0600); err != nil {
		return nil, err
	}
	b, err := OpenBundle(directory)
	if err != nil {
		return nil, err
	}
	if err := parallel(ctx, b.ChunkIDs(), workers, func(ctx context.Context, hash string) error {
		out, err := os.OpenFile(filepath.Join(directory, "blobs", hash), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if err != nil {
			return err
		}
		err = c.download(ctx, hash, b.Chunks[hash].Bytes, out)
		closeErr := out.Close()
		if err != nil {
			return err
		}
		return closeErr
	}); err != nil {
		return nil, err
	}
	if err := b.Verify(ctx); err != nil {
		return nil, err
	}
	// The manifest is itself a protected source reference, not only a side file.
	if err := os.WriteFile(filepath.Join(directory, "blobs", Digest(b.ManifestBytes)), b.ManifestBytes, 0600); err != nil {
		return nil, err
	}
	// Reading the full append-only history also preserves edited and deleted
	// documents and their provenance independently of the materialized state.
	state := map[string]map[string]annotation.Object{"project": {}, "label": {}, "item": {}}
	cursor := ""
	changeCount := 0
	refs := &protectedReferences{assets: map[string]*protectedAsset{}, files: map[string]File{}}
	out, err := os.OpenFile(filepath.Join(directory, "fieldwork-changes.ndjson"), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return nil, err
	}
	defer out.Close()
	encoder := json.NewEncoder(out)
	for pages := 0; ; pages++ {
		if pages > 100000 {
			return nil, fmt.Errorf("change history exceeds pull bound")
		}
		response, err := c.call(ctx, "ListChanges", annotation.Object{"projectId": project, "cursor": cursor, "limit": 20})
		if err != nil {
			return nil, err
		}
		changes, _ := response["changes"].([]any)
		for _, raw := range changes {
			change, ok := raw.(map[string]any)
			if !ok {
				return nil, fmt.Errorf("invalid change record")
			}
			if err := encoder.Encode(change); err != nil {
				return nil, err
			}
			if err := refs.collect(change["document"]); err != nil {
				return nil, fmt.Errorf("change asset references: %w", err)
			}
			kind, _ := change["kind"].(string)
			id, _ := change["id"].(string)
			bucket, ok := state[kind]
			if !ok {
				return nil, fmt.Errorf("unsupported change entity")
			}
			if change["operation"] == "delete" {
				delete(bucket, id)
			} else {
				doc, ok := change["document"].(map[string]any)
				if !ok {
					return nil, fmt.Errorf("change lacks document")
				}
				bucket[id] = doc
			}
			changeCount++
		}
		next, _ := response["nextCursor"].(string)
		if len(changes) > 0 && next == cursor {
			return nil, fmt.Errorf("change cursor did not advance")
		}
		cursor = next
		more, _ := response["hasMore"].(bool)
		if !more {
			break
		}
	}
	additional, err := c.pullReferences(ctx, directory, b, refs, workers)
	if err != nil {
		return nil, err
	}
	assets := make([]*protectedAsset, 0, len(refs.assets))
	for _, ref := range refs.assets {
		assets = append(assets, ref)
	}
	sort.Slice(assets, func(i, j int) bool { return assets[i].SHA256 < assets[j].SHA256 })
	catalog, err := json.MarshalIndent(annotation.Object{"schemaVersion": 1, "projectId": project, "snapshotId": b.Manifest.SnapshotID, "cursor": cursor, "assets": assets, "chunkedFiles": refs.files, "additionalBlobs": additional, "scope": "Protected references in every change document through the saved cursor"}, "", "  ")
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(filepath.Join(directory, "fieldwork-assets.json"), catalog, 0600); err != nil {
		return nil, err
	}
	stateFile, err := os.OpenFile(filepath.Join(directory, "fieldwork-state.ndjson"), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return nil, err
	}
	defer stateFile.Close()
	enc := json.NewEncoder(stateFile)
	count := 0
	for _, kind := range []string{"project", "label", "item"} {
		ids := []string{}
		for id := range state[kind] {
			ids = append(ids, id)
		}
		sort.Strings(ids)
		for _, id := range ids {
			if err := enc.Encode(annotation.Object{"kind": kind, "document": state[kind][id]}); err != nil {
				return nil, err
			}
			count++
		}
	}
	if err := enc.Encode(annotation.Object{"kind": "checkpoint", "schemaVersion": 1, "cursor": cursor}); err != nil {
		return nil, err
	}
	verified := len(b.Chunks) + additional
	if _, exists := b.Chunks[Digest(b.ManifestBytes)]; !exists {
		verified++
	}
	return annotation.Object{"snapshotId": b.Manifest.SnapshotID, "files": len(b.Files), "verifiedBlobs": verified, "referencedBlobs": len(refs.assets), "additionalBlobs": additional, "assetCatalog": "fieldwork-assets.json", "documents": count, "changes": changeCount, "cursor": cursor, "directory": directory}, nil
}
