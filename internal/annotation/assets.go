package annotation

import (
	"bufio"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

const MaxAssetBytes int64 = 64 << 20

var assetDigest = regexp.MustCompile(`^[0-9a-f]{64}$`)

// localAssets mirrors the authenticated R2 API for local development. Bytes are
// immutable by SHA-256; a hard-link publishes a completed upload atomically.
func localAssets(directory string) (http.Handler, error) {
	root, err := filepath.Abs(directory)
	if err != nil {
		return nil, err
	}
	if err = os.MkdirAll(root, 0700); err != nil {
		return nil, err
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Authentication wraps this handler, so missing HEAD responses can safely
		// advertise the upload capability without changing stored representations.
		w.Header().Set("Accept-Encoding", "gzip")
		digest := strings.TrimPrefix(r.URL.Path, "/api/v1/assets/")
		if !assetDigest.MatchString(digest) {
			writeError(w, invalid("asset path must contain a lowercase SHA-256 digest"))
			return
		}
		path := filepath.Join(root, digest)
		switch r.Method {
		case http.MethodGet, http.MethodHead:
			file, err := os.Open(path)
			if errors.Is(err, os.ErrNotExist) {
				writeError(w, ErrNotFound)
				return
			}
			if err != nil {
				writeError(w, err)
				return
			}
			defer file.Close()
			info, err := file.Stat()
			if err != nil {
				writeError(w, err)
				return
			}
			contentType := "application/octet-stream"
			if data, err := os.ReadFile(path + ".type"); err == nil {
				contentType = string(data)
			}
			w.Header().Set("Content-Type", contentType)
			w.Header().Set("Content-Encoding", "identity")
			w.Header().Set("Content-Length", strconv.FormatInt(info.Size(), 10))
			w.Header().Set("X-Content-SHA256", digest)
			w.Header().Set("ETag", `"`+digest+`"`)
			if !inlineAssetType(contentType) {
				w.Header().Set("Content-Disposition", `attachment; filename="`+digest+`"`)
			}
			if r.Method == http.MethodHead {
				w.WriteHeader(http.StatusOK)
				return
			}
			http.ServeContent(w, r, digest, info.ModTime(), file)
		case http.MethodPut:
			if r.ContentLength < 0 {
				writeJSON(w, http.StatusLengthRequired, Object{"code": "invalid_argument", "message": "Content-Length is required"})
				return
			}
			if r.ContentLength > MaxAssetBytes {
				writeJSON(w, http.StatusRequestEntityTooLarge, Object{"code": "invalid_argument", "message": "asset exceeds 64 MiB; upload content-addressed chunks"})
				return
			}
			encoding := strings.ToLower(strings.TrimSpace(r.Header.Get("Content-Encoding")))
			length := r.ContentLength
			if encoding != "" && encoding != "identity" && encoding != "gzip" {
				writeJSON(w, http.StatusUnsupportedMediaType, Object{"code": "invalid_argument", "message": "asset uploads support identity or gzip Content-Encoding"})
				return
			}
			if encoding == "gzip" {
				rawLength := r.Header.Get("X-Uncompressed-Length")
				if rawLength == "" || strings.IndexFunc(rawLength, func(r rune) bool { return r < '0' || r > '9' }) >= 0 {
					writeError(w, invalid("gzip uploads require X-Uncompressed-Length"))
					return
				}
				var lengthErr error
				length, lengthErr = strconv.ParseInt(rawLength, 10, 64)
				if lengthErr != nil || length > MaxAssetBytes {
					writeJSON(w, http.StatusRequestEntityTooLarge, Object{"code": "invalid_argument", "message": "decoded assets may be at most 64 MiB"})
					return
				}
			}
			contentType, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
			if err != nil {
				writeError(w, invalid("a valid Content-Type is required"))
				return
			}
			if !inlineAssetType(contentType) {
				contentType = "application/octet-stream"
			}
			wire := &io.LimitedReader{R: r.Body, N: r.ContentLength + 1}
			var body io.Reader = wire
			var encoded *bufio.Reader
			if encoding == "gzip" {
				encoded = bufio.NewReader(wire)
				decoder, decodeErr := gzip.NewReader(encoded)
				if decodeErr != nil {
					writeError(w, invalid("invalid gzip asset body"))
					return
				}
				defer decoder.Close()
				decoder.Multistream(false)
				body = decoder
			}
			temp, err := os.CreateTemp(root, ".upload-")
			if err != nil {
				writeError(w, err)
				return
			}
			defer os.Remove(temp.Name())
			h := sha256.New()
			n, copyErr := io.Copy(io.MultiWriter(temp, h), io.LimitReader(body, length+1))
			closeErr := temp.Close()
			if copyErr != nil || closeErr != nil || n != length {
				writeError(w, invalid("asset body or decoded length is invalid"))
				return
			}
			if encoded != nil {
				if _, trailingErr := encoded.ReadByte(); trailingErr != io.EOF {
					writeError(w, invalid("gzip asset has trailing data"))
					return
				}
			}
			if r.ContentLength+1-wire.N != r.ContentLength {
				writeError(w, invalid("asset wire length does not match Content-Length"))
				return
			}
			if hex.EncodeToString(h.Sum(nil)) != digest {
				writeError(w, invalid("asset bytes do not match the URL SHA-256"))
				return
			}
			status := http.StatusCreated
			if err = os.Link(temp.Name(), path); errors.Is(err, os.ErrExist) {
				status = http.StatusOK
				info, statErr := os.Stat(path)
				if statErr != nil || info.Size() != n {
					writeError(w, errors.New("stored asset failed integrity check"))
					return
				}
				stored, openErr := os.Open(path)
				if openErr != nil {
					writeError(w, openErr)
					return
				}
				storedHash := sha256.New()
				_, hashErr := io.Copy(storedHash, stored)
				stored.Close()
				if hashErr != nil || hex.EncodeToString(storedHash.Sum(nil)) != digest {
					writeError(w, errors.New("stored asset failed integrity check"))
					return
				}
				if old, err := os.ReadFile(path + ".type"); err == nil {
					contentType = string(old)
				}
			} else if err != nil {
				writeError(w, err)
				return
			} else {
				if err = os.WriteFile(path+".type", []byte(contentType), 0600); err != nil {
					writeError(w, err)
					return
				}
			}
			writeJSON(w, status, Object{"url": "/api/v1/assets/" + digest, "sha256": digest, "bytes": n, "contentType": contentType})
		default:
			w.Header().Set("Allow", "GET,HEAD,PUT")
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	}), nil
}

func inlineAssetType(value string) bool {
	switch value {
	case "image/png", "image/jpeg", "image/webp", "image/avif", "image/gif", "text/plain", "application/json", "application/geo+json", "application/x-ndjson", "application/gzip", "application/zip":
		return true
	}
	return false
}
