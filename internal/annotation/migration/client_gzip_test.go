package migration

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"io"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"
)

func TestGzipUploadNegotiatesAndRetriesExactBodies(t *testing.T) {
	temporary := t.TempDir()
	t.Setenv("TMPDIR", temporary)
	content := bytes.Repeat([]byte(`{"scene":"source","coordinates":[1,2,3]}`), 20000)
	hash := Digest(content)
	var heads, puts, opens int
	var firstBody []byte
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer explicit-test-credential" {
			t.Error("compression changed authentication")
		}
		if r.Method == http.MethodHead {
			heads++
			w.Header().Set("Accept-Encoding", "identity, gzip;q=1")
			w.WriteHeader(http.StatusNotFound)
			return
		}
		puts++
		wire, err := io.ReadAll(r.Body)
		if err != nil || r.Header.Get("Content-Encoding") != "gzip" || r.Header.Get("X-Uncompressed-Length") != strconv.Itoa(len(content)) || r.ContentLength != int64(len(wire)) || len(wire) >= len(content)*9/10 {
			t.Errorf("incorrect gzip transport: headers=%v bytes=%d err=%v", r.Header, len(wire), err)
		}
		decoded, err := gzip.NewReader(bytes.NewReader(wire))
		if err != nil {
			t.Error(err)
			w.WriteHeader(400)
			return
		}
		plain, err := io.ReadAll(decoded)
		decoded.Close()
		if err != nil || !bytes.Equal(plain, content) || !strings.HasSuffix(r.URL.Path, Digest(plain)) {
			t.Error("gzip upload changed the original bytes or digest")
		}
		if puts == 1 {
			firstBody = wire
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		if !bytes.Equal(firstBody, wire) {
			t.Error("retry did not replay the exact compressed body")
		}
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{"sha256": hash, "bytes": len(content)})
	}))
	defer server.Close()
	client, _ := NewClient(server.URL, "explicit-test-credential")
	created, err := client.upload(context.Background(), hash, int64(len(content)), "application/json", func() (io.ReadCloser, error) {
		opens++
		return io.NopCloser(bytes.NewReader(content)), nil
	})
	if err != nil || !created || puts != 2 || heads != 2 || opens != 2 {
		t.Fatalf("gzip retry result: created=%v puts=%d heads=%d opens=%d err=%v", created, puts, heads, opens, err)
	}
	assertNoGzipTemps(t, temporary)
}

func TestGzipUploadKeepsIdentityForLegacyServersAndUnhelpfulCompression(t *testing.T) {
	compressible := bytes.Repeat([]byte("repeatable source text\n"), 20000)
	noise := make([]byte, 400000)
	_, _ = rand.New(rand.NewSource(42)).Read(noise)
	for _, scenario := range []struct {
		name, accepts, contentType string
		content                    []byte
	}{
		{"legacy", "", "application/json", compressible},
		{"disabled", "gzip;q=0", "application/json", compressible},
		{"image", "gzip", "image/png", compressible},
		{"small", "gzip", "application/json", []byte("small")},
		{"incompressible", "gzip", "application/octet-stream", noise},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			temporary := t.TempDir()
			t.Setenv("TMPDIR", temporary)
			hash := Digest(scenario.content)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method == http.MethodHead {
					w.Header().Set("Accept-Encoding", scenario.accepts)
					w.WriteHeader(http.StatusNotFound)
					return
				}
				body, _ := io.ReadAll(r.Body)
				if !bytes.Equal(body, scenario.content) || r.Header.Get("Content-Encoding") != "" || r.Header.Get("X-Uncompressed-Length") != "" || r.ContentLength != int64(len(scenario.content)) {
					t.Errorf("expected an unchanged identity upload: %v", r.Header)
				}
				w.WriteHeader(http.StatusCreated)
				_ = json.NewEncoder(w).Encode(map[string]any{"sha256": hash, "bytes": len(scenario.content)})
			}))
			defer server.Close()
			client, _ := NewClient(server.URL, "")
			created, err := client.upload(context.Background(), hash, int64(len(scenario.content)), scenario.contentType, func() (io.ReadCloser, error) { return io.NopCloser(bytes.NewReader(scenario.content)), nil })
			if err != nil || !created {
				t.Fatalf("identity fallback: created=%v err=%v", created, err)
			}
			assertNoGzipTemps(t, temporary)
		})
	}
}

func TestGzipPreparationCleansUpFailedAndCanceledInputs(t *testing.T) {
	for _, scenario := range []string{"source-error", "wrong-size", "canceled"} {
		t.Run(scenario, func(t *testing.T) {
			temporary := t.TempDir()
			t.Setenv("TMPDIR", temporary)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			var source io.Reader = bytes.NewReader([]byte("too short"))
			if scenario == "source-error" {
				source = io.MultiReader(source, readFunc(func([]byte) (int, error) { return 0, errors.New("source read failed") }))
			}
			if scenario == "canceled" {
				source = readFunc(func(p []byte) (int, error) {
					cancel()
					return copy(p, "cancel after this chunk"), nil
				})
			}
			file, _, err := gzipAsset(ctx, 400000, func() (io.ReadCloser, error) { return io.NopCloser(source), nil })
			if err == nil || file != nil {
				t.Fatalf("invalid input accepted: file=%v err=%v", file, err)
			}
			if scenario == "canceled" && !errors.Is(err, context.Canceled) {
				t.Fatalf("cancellation was lost: %v", err)
			}
			assertNoGzipTemps(t, temporary)
		})
	}
}

type readFunc func([]byte) (int, error)

func (f readFunc) Read(p []byte) (int, error) { return f(p) }

func assertNoGzipTemps(t *testing.T, directory string) {
	t.Helper()
	entries, err := os.ReadDir(directory)
	if err != nil || len(entries) != 0 {
		t.Fatalf("upload left temporary files: %v, %v", entries, err)
	}
}
