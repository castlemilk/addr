package annotation

import (
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
)

func TestAssetGzipPreservesOriginalBytesAndRejectsInvalidStreams(t *testing.T) {
	handler, err := NewHTTPHandler(openTestStore(t), HTTPConfig{Token: testToken, AssetDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	compress := func(data []byte) []byte {
		var out bytes.Buffer
		writer, _ := gzip.NewWriterLevel(&out, gzip.BestSpeed)
		_, _ = writer.Write(data)
		_ = writer.Close()
		return out.Bytes()
	}
	pathFor := func(data []byte) string {
		hash := sha256.Sum256(data)
		return "/api/v1/assets/" + hex.EncodeToString(hash[:])
	}
	send := func(method, path string, body []byte, headers map[string]string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, bytes.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+testToken)
		req.Header.Set("Content-Type", "application/json")
		for key, value := range headers {
			req.Header.Set(key, value)
		}
		out := httptest.NewRecorder()
		handler.ServeHTTP(out, req)
		return out
	}
	data := bytes.Repeat([]byte("source evidence\n"), 1000)
	path := pathFor(data)
	if missing := send("HEAD", path, nil, nil); missing.Code != 404 || missing.Header().Get("Accept-Encoding") != "gzip" {
		t.Fatalf("missing asset did not advertise gzip: %d %v", missing.Code, missing.Header())
	}
	if out := send("PUT", path, compress(data), map[string]string{"Content-Encoding": "gzip", "X-Uncompressed-Length": strconv.Itoa(len(data))}); out.Code != 201 {
		t.Fatalf("gzip upload=%d %s", out.Code, out.Body)
	}
	if out := send("GET", path, nil, nil); out.Code != 200 || !bytes.Equal(out.Body.Bytes(), data) || out.Header().Get("Content-Encoding") != "identity" {
		t.Fatalf("gzip changed stored representation: %d", out.Code)
	}
	if out := send("PUT", path, data, nil); out.Code != 200 {
		t.Fatalf("raw repeat after gzip=%d", out.Code)
	}
	for _, scenario := range []string{"malformed", "truncated", "checksum", "trailing", "concatenated", "too-short", "too-long", "missing-length", "oversized", "wrong-sha"} {
		t.Run(scenario, func(t *testing.T) {
			original := bytes.Repeat([]byte(scenario+" evidence\n"), 50)
			path := pathFor(original)
			body := compress(original)
			length := strconv.Itoa(len(original))
			want := 400
			switch scenario {
			case "malformed":
				body = []byte("not gzip")
			case "truncated":
				body = body[:len(body)-5]
			case "checksum":
				body[len(body)-8] ^= 1
			case "trailing":
				body = append(body, []byte("trailing")...)
			case "concatenated":
				body = append(body, compress(nil)...)
			case "too-short":
				length = strconv.Itoa(len(original) - 1)
			case "too-long":
				length = strconv.Itoa(len(original) + 1)
			case "missing-length":
				length = ""
			case "oversized":
				length, want = strconv.FormatInt(MaxAssetBytes+1, 10), 413
			case "wrong-sha":
				path = "/api/v1/assets/" + strings.Repeat("a", 64)
			}
			out := send("PUT", path, body, map[string]string{"Content-Encoding": "gzip", "X-Uncompressed-Length": length})
			if out.Code != want {
				t.Fatalf("invalid gzip accepted: status=%d body=%s", out.Code, out.Body)
			}
			if out := send("HEAD", path, nil, nil); out.Code != 404 {
				t.Fatalf("invalid gzip published an object: %d", out.Code)
			}
		})
	}
	if out := send("PUT", pathFor(nil), compress(nil), map[string]string{"Content-Encoding": "gzip", "X-Uncompressed-Length": "0"}); out.Code != 201 {
		t.Fatalf("empty gzip upload=%d %s", out.Code, out.Body)
	}
}

func TestAssetsProtectedImmutableAndVerified(t *testing.T) {
	handler, err := NewHTTPHandler(openTestStore(t), HTTPConfig{Token: testToken, AssetDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	data := `{"evidence":"raw review"}`
	sum := sha256.Sum256([]byte(data))
	path := "/api/v1/assets/" + hex.EncodeToString(sum[:])
	send := func(method, body, token string) *httptest.ResponseRecorder {
		t.Helper()
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		out := httptest.NewRecorder()
		handler.ServeHTTP(out, req)
		return out
	}
	if out := send("PUT", data, ""); out.Code != 401 {
		t.Fatalf("unprotected upload: %d", out.Code)
	}
	if out := send("PUT", "wrong", testToken); out.Code != 400 {
		t.Fatalf("checksum mismatch accepted: %d", out.Code)
	}
	if out := send("PUT", data, testToken); out.Code != 201 {
		t.Fatalf("upload=%d %s", out.Code, out.Body)
	}
	if out := send("PUT", data, testToken); out.Code != 200 {
		t.Fatalf("immutable repeat=%d", out.Code)
	}
	if out := send("GET", "", ""); out.Code != 401 {
		t.Fatalf("unprotected read=%d", out.Code)
	}
	if out := send("GET", "", testToken); out.Code != 200 || out.Body.String() != data || out.Header().Get("X-Content-SHA256") != hex.EncodeToString(sum[:]) {
		t.Fatalf("read=%d %s", out.Code, out.Body)
	}
	if out := send("HEAD", "", testToken); out.Code != 200 || out.Body.Len() != 0 || out.Header().Get("Content-Length") != "25" {
		t.Fatalf("head=%d %v", out.Code, out.Header())
	}
	req := httptest.NewRequest(http.MethodPut, path, strings.NewReader(""))
	req.ContentLength = MaxAssetBytes + 1
	req.Header.Set("Authorization", "Bearer "+testToken)
	out := httptest.NewRecorder()
	handler.ServeHTTP(out, req)
	if out.Code != 413 {
		t.Fatalf("oversized upload=%d", out.Code)
	}
}

func TestAddrSceneRequiresProtectedFrameReferences(t *testing.T) {
	hash := strings.Repeat("a", 64)
	ref := Object{"url": "/api/v1/assets/" + hash, "sha256": hash}
	item := Item{Source: Object{"sceneId": "example", "image": ref, "model": ref, "width": float64(100), "height": float64(100)}, Annotations: []Annotation{{ID: "object", Properties: Object{"addr": Object{"objectId": "object"}}, Geometry: Geometry{Type: "Polygon", Coordinates: []byte(`[[[0,0],[1,0],[1,1],[0,0]]]`)}}}}
	if err := (addrSceneAdapter{}).Validate(&item); err != nil {
		t.Fatal(err)
	}
	item.Source["model"] = Object{"url": "https://public.example/raw", "sha256": hash}
	if err := (addrSceneAdapter{}).Validate(&item); err == nil {
		t.Fatal("public raw model URL accepted")
	}
}
