package migration

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"sync/atomic"
	"testing"
)

func TestUploadRetriesTransientOperationsWithFreshBodies(t *testing.T) {
	content := []byte("immutable source bytes")
	hash := Digest(content)
	var heads, puts, opens atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer explicit-test-credential" {
			t.Error("credential header changed during retry")
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		if r.Method == http.MethodHead {
			if heads.Add(1) == 1 {
				w.WriteHeader(http.StatusServiceUnavailable)
			} else {
				w.WriteHeader(http.StatusNotFound)
			}
			return
		}
		body, _ := io.ReadAll(r.Body)
		if !bytes.Equal(body, content) || r.ContentLength != int64(len(content)) {
			t.Error("retry did not replay the complete immutable body")
		}
		if puts.Add(1) == 1 {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{"sha256": hash, "bytes": len(content)})
	}))
	defer server.Close()
	client, _ := NewClient(server.URL, "explicit-test-credential")
	created, err := client.upload(context.Background(), hash, int64(len(content)), "application/octet-stream", func() (io.ReadCloser, error) {
		opens.Add(1)
		return io.NopCloser(bytes.NewReader(content)), nil
	})
	if err != nil || !created || heads.Load() != 3 || puts.Load() != 2 || opens.Load() != 2 {
		t.Fatalf("unexpected retry result: created=%v heads=%d puts=%d opens=%d err=%v", created, heads.Load(), puts.Load(), opens.Load(), err)
	}
}

func TestUploadChecksForCommitAfterLostResponse(t *testing.T) {
	content := []byte("committed before transient response")
	hash := Digest(content)
	var committed atomic.Bool
	var puts atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodHead {
			if !committed.Load() {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			w.Header().Set("Content-Length", strconv.Itoa(len(content)))
			w.Header().Set("X-Content-SHA256", hash)
			return
		}
		puts.Add(1)
		body, _ := io.ReadAll(r.Body)
		if !bytes.Equal(body, content) {
			t.Error("incorrect source bytes")
		}
		committed.Store(true)
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer server.Close()
	client, _ := NewClient(server.URL, "")
	created, err := client.upload(context.Background(), hash, int64(len(content)), "application/octet-stream", func() (io.ReadCloser, error) { return io.NopCloser(bytes.NewReader(content)), nil })
	if err != nil || created || puts.Load() != 1 {
		t.Fatalf("committed asset was rewritten: created=%v puts=%d err=%v", created, puts.Load(), err)
	}
}

func TestAssetFailuresAreNotRetriedWhenPermanent(t *testing.T) {
	for _, status := range []int{400, 401, 403, 409} {
		t.Run(strconv.Itoa(status), func(t *testing.T) {
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				w.WriteHeader(status)
			}))
			defer server.Close()
			client, _ := NewClient(server.URL, "")
			_, err := client.head(context.Background(), Digest(nil), 0)
			if err == nil || calls.Load() != 1 {
				t.Fatalf("permanent failure retried: calls=%d err=%v", calls.Load(), err)
			}
		})
	}
	var calls int
	ctx, cancel := context.WithCancel(context.Background())
	err := retryAsset(ctx, func() error { calls++; cancel(); return &APIError{Status: 503} })
	if err == nil || calls != 1 {
		t.Fatalf("canceled transfer retried: calls=%d err=%v", calls, err)
	}
}

func TestDownloadRetryDiscardsPartialBytes(t *testing.T) {
	for _, target := range []string{"buffer", "file"} {
		t.Run(target, func(t *testing.T) {
			content := []byte("complete verified evidence")
			var gets atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Length", strconv.Itoa(len(content)))
				if gets.Add(1) == 1 {
					_, _ = w.Write(content[:5])
					w.(http.Flusher).Flush()
					connection, _, _ := w.(http.Hijacker).Hijack()
					_ = connection.Close()
					return
				}
				_, _ = w.Write(content)
			}))
			defer server.Close()
			client, _ := NewClient(server.URL, "")
			var output io.Writer
			buffer := bytes.NewBufferString("prefix:")
			var file *os.File
			if target == "file" {
				var err error
				file, err = os.Create(filepath.Join(t.TempDir(), "download"))
				if err != nil {
					t.Fatal(err)
				}
				defer file.Close()
				_, _ = file.WriteString("prefix:")
				output = file
			} else {
				output = buffer
			}
			if err := client.download(context.Background(), Digest(content), int64(len(content)), output); err != nil {
				t.Fatal(err)
			}
			actual := buffer.Bytes()
			if file != nil {
				actual, _ = os.ReadFile(file.Name())
			}
			if !bytes.Equal(actual, append([]byte("prefix:"), content...)) || gets.Load() != 2 {
				t.Fatalf("partial response remained in download: %q, attempts=%d", actual, gets.Load())
			}
		})
	}
}
