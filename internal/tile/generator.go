package tile

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"time"
)

// Generator requests tiles from the realtime AI tile-generation service.
// Responses are expected to be gzip-compressed MVT payloads (Content-Encoding: gzip);
// auto-decompression is disabled so the compressed bytes are cached and served as-is.
type Generator struct {
	baseURL string
	client  *http.Client

	requests atomic.Uint64
	found    atomic.Uint64
	empty    atomic.Uint64
	failures atomic.Uint64
}

// NewGenerator creates a generator client for the given base URL (e.g. http://localhost:8090).
func NewGenerator(baseURL string, timeout time.Duration) *Generator {
	if timeout <= 0 {
		timeout = 120 * time.Second
	}
	return &Generator{
		baseURL: strings.TrimRight(baseURL, "/"),
		client: &http.Client{
			Timeout: timeout,
			Transport: &http.Transport{
				DisableCompression:    true,
				MaxIdleConns:          16,
				MaxIdleConnsPerHost:   8,
				IdleConnTimeout:       90 * time.Second,
				ResponseHeaderTimeout: timeout,
			},
		},
	}
}

// Generate fetches a single tile. found=false with nil error means the tile was
// generated successfully but contains no buildings (HTTP 204), which callers may
// cache as an empty tile to avoid regenerating.
func (g *Generator) Generate(ctx context.Context, z, x, y uint32) (data []byte, found bool, err error) {
	g.requests.Add(1)
	url := fmt.Sprintf("%s/tile/%d/%d/%d.pbf", g.baseURL, z, x, y)

	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		g.failures.Add(1)
		return nil, false, err
	}

	response, err := g.client.Do(request)
	if err != nil {
		g.failures.Add(1)
		return nil, false, err
	}
	defer response.Body.Close()

	switch response.StatusCode {
	case http.StatusOK:
		payload, readErr := io.ReadAll(io.LimitReader(response.Body, 64<<20))
		if readErr != nil {
			g.failures.Add(1)
			return nil, false, readErr
		}
		g.found.Add(1)
		return payload, true, nil
	case http.StatusNoContent:
		g.empty.Add(1)
		return nil, false, nil
	default:
		body, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
		g.failures.Add(1)
		return nil, false, fmt.Errorf("tile generator returned HTTP %d: %s", response.StatusCode, strings.TrimSpace(string(body)))
	}
}

// Stats returns operational counters for the generator client.
func (g *Generator) Stats() map[string]uint64 {
	return map[string]uint64{
		"requests": g.requests.Load(),
		"found":    g.found.Load(),
		"empty":    g.empty.Load(),
		"failures": g.failures.Load(),
	}
}
