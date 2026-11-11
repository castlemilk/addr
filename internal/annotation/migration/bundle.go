// Package migration preserves a frozen local dataset as immutable assets and
// projects its editable scene state into Fieldwork documents.
package migration

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/benebsworth/addr/internal/annotation"
)

var digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
var idPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)

type Chunk struct {
	SHA256 string `json:"sha256"`
	Bytes  int64  `json:"bytes"`
}
type File struct {
	Path        string  `json:"path"`
	SHA256      string  `json:"sha256"`
	Bytes       int64   `json:"bytes"`
	ContentType string  `json:"contentType"`
	Chunks      []Chunk `json:"chunks"`
	MtimeNs     string  `json:"mtimeNs,omitempty"`
}
type Manifest struct {
	SchemaVersion int               `json:"schemaVersion"`
	SnapshotID    string            `json:"snapshotId"`
	CreatedAt     string            `json:"createdAt"`
	SceneIDs      []string          `json:"sceneIds"`
	Files         []File            `json:"files"`
	Summary       annotation.Object `json:"summary"`
	Exclusions    any               `json:"exclusions"`
}
type Bundle struct {
	Directory     string
	Manifest      Manifest
	ManifestBytes []byte
	Files         map[string]File
	Chunks        map[string]Chunk
	ContentTypes  map[string]string
}

func Digest(data []byte) string { sum := sha256.Sum256(data); return hex.EncodeToString(sum[:]) }

func safePath(value string) bool {
	return value != "" && value != "." && value == path.Clean(value) && !strings.HasPrefix(value, "/") && !strings.HasPrefix(value, "../") && !strings.ContainsAny(value, "\\:\x00")
}

func OpenBundle(directory string) (*Bundle, error) {
	data, err := os.ReadFile(filepath.Join(directory, "manifest.json"))
	if err != nil {
		return nil, err
	}
	if len(data) > int(annotation.MaxAssetBytes) {
		return nil, fmt.Errorf("manifest exceeds asset limit")
	}
	b := &Bundle{Directory: directory, ManifestBytes: data, Files: map[string]File{}, Chunks: map[string]Chunk{}, ContentTypes: map[string]string{}}
	if err := json.Unmarshal(data, &b.Manifest); err != nil {
		return nil, fmt.Errorf("manifest: %w", err)
	}
	if b.Manifest.SchemaVersion != 1 || !digestPattern.MatchString(b.Manifest.SnapshotID) {
		return nil, fmt.Errorf("unsupported manifest schema or snapshot ID")
	}
	if _, err := time.Parse(time.RFC3339Nano, b.Manifest.CreatedAt); err != nil {
		return nil, fmt.Errorf("invalid manifest createdAt")
	}
	scenes := map[string]bool{}
	for _, id := range b.Manifest.SceneIDs {
		if !idPattern.MatchString("addr-scene-"+id) || scenes[id] {
			return nil, fmt.Errorf("invalid or repeated scene ID %q", id)
		}
		scenes[id] = true
	}
	for _, file := range b.Manifest.Files {
		if !safePath(file.Path) || !digestPattern.MatchString(file.SHA256) || file.Bytes < 0 || len(file.Chunks) == 0 {
			return nil, fmt.Errorf("invalid manifest file %q", file.Path)
		}
		if _, exists := b.Files[file.Path]; exists {
			return nil, fmt.Errorf("duplicate manifest path %q", file.Path)
		}
		if file.MtimeNs != "" {
			if _, err := strconv.ParseInt(file.MtimeNs, 10, 64); err != nil {
				return nil, fmt.Errorf("invalid mtimeNs: %s", file.Path)
			}
		}
		var total int64
		for _, chunk := range file.Chunks {
			if !digestPattern.MatchString(chunk.SHA256) || chunk.Bytes < 0 || chunk.Bytes > annotation.MaxAssetBytes {
				return nil, fmt.Errorf("invalid chunk for %s", file.Path)
			}
			total += chunk.Bytes
			if previous, ok := b.Chunks[chunk.SHA256]; ok && previous.Bytes != chunk.Bytes {
				return nil, fmt.Errorf("inconsistent chunk length")
			}
			b.Chunks[chunk.SHA256] = chunk
			contentType := "application/octet-stream"
			if len(file.Chunks) == 1 {
				contentType = file.ContentType
			}
			// The same bytes may have several filenames. Prefer a useful safe type.
			if old := b.ContentTypes[chunk.SHA256]; old == "" || old == "application/octet-stream" {
				b.ContentTypes[chunk.SHA256] = contentType
			}
		}
		if total != file.Bytes {
			return nil, fmt.Errorf("chunk lengths do not sum to file length: %s", file.Path)
		}
		b.Files[file.Path] = file
	}
	return b, nil
}

// copyFile reconstructs and checks whole-file identity, including ordered chunks.
func (b *Bundle) copyFile(ctx context.Context, file File, target io.Writer) error {
	hash := sha256.New()
	writer := io.MultiWriter(target, hash)
	var size int64
	for _, chunk := range file.Chunks {
		if err := ctx.Err(); err != nil {
			return err
		}
		f, err := os.Open(filepath.Join(b.Directory, "blobs", chunk.SHA256))
		if err != nil {
			return err
		}
		n, err := io.Copy(writer, io.LimitReader(f, chunk.Bytes+1))
		f.Close()
		if err != nil {
			return err
		}
		if n != chunk.Bytes {
			return fmt.Errorf("chunk length mismatch: %s", chunk.SHA256)
		}
		size += n
	}
	if size != file.Bytes || hex.EncodeToString(hash.Sum(nil)) != file.SHA256 {
		return fmt.Errorf("file integrity mismatch: %s", file.Path)
	}
	return nil
}

func (b *Bundle) ReadFile(ctx context.Context, name string) ([]byte, error) {
	file, ok := b.Files[name]
	if !ok {
		return nil, fmt.Errorf("required file missing from manifest: %s", name)
	}
	if file.Bytes > annotation.MaxAssetBytes {
		return nil, fmt.Errorf("JSON source exceeds 64 MiB: %s", name)
	}
	var out bytes.Buffer
	out.Grow(int(file.Bytes))
	if err := b.copyFile(ctx, file, &out); err != nil {
		return nil, err
	}
	return out.Bytes(), nil
}

func (b *Bundle) ReadJSON(ctx context.Context, name string, into any) error {
	data, err := b.ReadFile(ctx, name)
	if err != nil {
		return err
	}
	if err = json.Unmarshal(data, into); err != nil {
		return fmt.Errorf("%s: %w", name, err)
	}
	return nil
}

func (b *Bundle) Verify(ctx context.Context) error {
	for _, key := range b.ChunkIDs() {
		if err := ctx.Err(); err != nil {
			return err
		}
		chunk := b.Chunks[key]
		f, err := os.Open(filepath.Join(b.Directory, "blobs", key))
		if err != nil {
			return err
		}
		hash := sha256.New()
		n, err := io.Copy(hash, io.LimitReader(f, chunk.Bytes+1))
		f.Close()
		if err != nil {
			return err
		}
		if n != chunk.Bytes || hex.EncodeToString(hash.Sum(nil)) != key {
			return fmt.Errorf("corrupt blob: %s", key)
		}
	}
	for _, file := range b.Manifest.Files {
		if err := b.copyFile(ctx, file, io.Discard); err != nil {
			return err
		}
	}
	return nil
}

func (b *Bundle) ChunkIDs() []string {
	keys := make([]string, 0, len(b.Chunks))
	for id := range b.Chunks {
		keys = append(keys, id)
	}
	sort.Strings(keys)
	return keys
}

// Restore refuses existing output paths and restores original repository-relative
// filenames. Credentials excluded by the snapshot are never synthesized.
func (b *Bundle) Restore(ctx context.Context, directory string) error {
	if _, err := os.Stat(directory); err == nil {
		return fmt.Errorf("restore destination must not exist")
	} else if !os.IsNotExist(err) {
		return err
	}
	if err := os.MkdirAll(directory, 0700); err != nil {
		return err
	}
	for _, file := range b.Manifest.Files {
		target := filepath.Join(directory, filepath.FromSlash(file.Path))
		if err := os.MkdirAll(filepath.Dir(target), 0700); err != nil {
			return err
		}
		out, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if err != nil {
			return err
		}
		err = b.copyFile(ctx, file, out)
		closeErr := out.Close()
		if err != nil {
			return err
		}
		if closeErr != nil {
			return closeErr
		}
		if file.MtimeNs != "" {
			n, _ := strconv.ParseInt(file.MtimeNs, 10, 64)
			mtime := time.Unix(0, n)
			if err := os.Chtimes(target, mtime, mtime); err != nil {
				return err
			}
		}
	}
	return nil
}

func (b *Bundle) Ref(name string) (annotation.Object, error) {
	file, ok := b.Files[name]
	if !ok {
		return nil, fmt.Errorf("required asset missing: %s", name)
	}
	ref := annotation.Object{"sha256": file.SHA256, "bytes": file.Bytes, "contentType": file.ContentType, "path": file.Path}
	if len(file.Chunks) == 1 {
		ref["url"] = "/api/v1/assets/" + file.SHA256
		return ref, nil
	}
	chunks := []annotation.Object{}
	for _, chunk := range file.Chunks {
		chunks = append(chunks, annotation.Object{"url": "/api/v1/assets/" + chunk.SHA256, "sha256": chunk.SHA256, "bytes": chunk.Bytes, "contentType": "application/octet-stream"})
	}
	ref["chunks"] = chunks
	return ref, nil
}

func (b *Bundle) ManifestRef() annotation.Object {
	hash := Digest(b.ManifestBytes)
	return annotation.Object{"url": "/api/v1/assets/" + hash, "sha256": hash, "bytes": len(b.ManifestBytes), "contentType": "application/json"}
}
