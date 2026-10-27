package migration

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"strings"
	"sync"

	"github.com/benebsworth/addr/internal/annotation"
)

// LocalSceneFiles reads one live scene directory under Root/data/scenes/SceneID.
// Overrides substitute bytes for a repository path (for example the compact
// review head that replaces review.json in a pushed generation). Refs are
// content-addressed exactly like frozen bundle refs; files over the asset
// limit become chunked refs.
type LocalSceneFiles struct {
	Root      string
	SceneID   string
	overrides map[string][]byte
	mu        sync.Mutex
	refs      map[string]annotation.Object
}

func NewLocalSceneFiles(root, sceneID string, overrides map[string][]byte) *LocalSceneFiles {
	if overrides == nil {
		overrides = map[string][]byte{}
	}
	return &LocalSceneFiles{Root: root, SceneID: sceneID, overrides: overrides, refs: map[string]annotation.Object{}}
}

// Overrides returns the substituted bytes for a path, if any.
func (f *LocalSceneFiles) Override(name string) ([]byte, bool) {
	data, ok := f.overrides[name]
	return data, ok
}

func (f *LocalSceneFiles) filePath(name string) (string, error) {
	if !safePath(name) || !strings.HasPrefix(name, "data/scenes/"+f.SceneID+"/") {
		return "", fmt.Errorf("path %q is outside scene %s", name, f.SceneID)
	}
	return filepath.Join(f.Root, filepath.FromSlash(name)), nil
}

func (f *LocalSceneFiles) Has(name string) bool {
	if _, ok := f.overrides[name]; ok {
		return true
	}
	p, err := f.filePath(name)
	if err != nil {
		return false
	}
	info, err := os.Stat(p)
	return err == nil && info.Mode().IsRegular()
}

// Open returns a reader over the path's bytes (override or file).
func (f *LocalSceneFiles) Open(name string) (io.ReadCloser, error) {
	if data, ok := f.overrides[name]; ok {
		return io.NopCloser(strings.NewReader(string(data))), nil
	}
	p, err := f.filePath(name)
	if err != nil {
		return nil, err
	}
	return os.Open(p)
}

func (f *LocalSceneFiles) ReadFile(ctx context.Context, name string) ([]byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if data, ok := f.overrides[name]; ok {
		return data, nil
	}
	p, err := f.filePath(name)
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(p)
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > annotation.MaxAssetBytes {
		return nil, fmt.Errorf("JSON source exceeds 64 MiB: %s", name)
	}
	return data, nil
}

func (f *LocalSceneFiles) ReadJSON(ctx context.Context, name string, into any) error {
	data, err := f.ReadFile(ctx, name)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(data, into); err != nil {
		return fmt.Errorf("%s: %w", name, err)
	}
	return nil
}

func contentTypeFor(name string) string {
	switch strings.ToLower(path.Ext(name)) {
	case ".png":
		return "image/png"
	case ".json":
		return "application/json"
	case ".geojson":
		return "application/geo+json"
	case ".ndjson":
		return "application/x-ndjson"
	}
	return "application/octet-stream"
}

// Ref hashes the path (once) and returns a protected content-addressed reference.
func (f *LocalSceneFiles) Ref(name string) (annotation.Object, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if ref, ok := f.refs[name]; ok {
		return cloneObject(ref), nil
	}
	reader, err := f.Open(name)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, fmt.Errorf("required asset missing: %s", name)
		}
		return nil, err
	}
	defer reader.Close()
	whole := sha256.New()
	chunks := []Chunk{}
	var total int64
	buffer := make([]byte, 1<<20)
	chunk := sha256.New()
	var chunkBytes int64
	for {
		n, readErr := reader.Read(buffer)
		if n > 0 {
			data := buffer[:n]
			for len(data) > 0 {
				room := annotation.MaxAssetBytes - chunkBytes
				take := int64(len(data))
				if take > room {
					take = room
				}
				whole.Write(data[:take])
				chunk.Write(data[:take])
				chunkBytes += take
				total += take
				data = data[take:]
				if chunkBytes == annotation.MaxAssetBytes {
					chunks = append(chunks, Chunk{hex.EncodeToString(chunk.Sum(nil)), chunkBytes})
					chunk.Reset()
					chunkBytes = 0
				}
			}
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			return nil, readErr
		}
	}
	if chunkBytes > 0 || len(chunks) == 0 {
		chunks = append(chunks, Chunk{hex.EncodeToString(chunk.Sum(nil)), chunkBytes})
	}
	file := File{Path: name, SHA256: hex.EncodeToString(whole.Sum(nil)), Bytes: total, ContentType: contentTypeFor(name), Chunks: chunks}
	ref := refFor(file)
	f.refs[name] = ref
	return cloneObject(ref), nil
}

// File describes the path as a manifest-style file entry (hash, size, chunks).
func (f *LocalSceneFiles) File(name string) (File, error) {
	ref, err := f.Ref(name)
	if err != nil {
		return File{}, err
	}
	file := File{Path: name, SHA256: ref["sha256"].(string), Bytes: ref["bytes"].(int64), ContentType: ref["contentType"].(string)}
	if raw, ok := ref["chunks"].([]annotation.Object); ok {
		for _, c := range raw {
			file.Chunks = append(file.Chunks, Chunk{c["sha256"].(string), c["bytes"].(int64)})
		}
	} else {
		file.Chunks = []Chunk{{file.SHA256, file.Bytes}}
	}
	return file, nil
}

func refFor(file File) annotation.Object {
	ref := annotation.Object{"sha256": file.SHA256, "bytes": file.Bytes, "contentType": file.ContentType, "path": file.Path}
	if len(file.Chunks) == 1 {
		ref["url"] = "/api/v1/assets/" + file.SHA256
		return ref
	}
	chunks := []annotation.Object{}
	for _, chunk := range file.Chunks {
		chunks = append(chunks, annotation.Object{"url": "/api/v1/assets/" + chunk.SHA256, "sha256": chunk.SHA256, "bytes": chunk.Bytes, "contentType": "application/octet-stream"})
	}
	ref["chunks"] = chunks
	return ref
}

func cloneObject(in annotation.Object) annotation.Object {
	out := annotation.Object{}
	for k, v := range in {
		if list, ok := v.([]annotation.Object); ok {
			copied := make([]annotation.Object, 0, len(list))
			for _, entry := range list {
				copied = append(copied, cloneObject(entry))
			}
			out[k] = copied
			continue
		}
		out[k] = v
	}
	return out
}
