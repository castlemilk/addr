package tile_test

import (
	"crypto/rand"
	"sync"
	"testing"

	"github.com/benebsworth/addr/internal/tile"
)

func generateDummyTile(size int) tile.Tile {
	data := make([]byte, size)
	_, _ = rand.Read(data)
	return tile.Tile{
		Data:        data,
		ContentType: "application/vnd.mapbox-vector-tile",
		ETag:        `W/"dummy-etag"`,
	}
}

func TestShardedLRUCache_Basic(t *testing.T) {
	// 1 MB cache
	c := tile.NewShardedLRUCache(1024 * 1024)

	coord := tile.TileCoord{Z: 14, X: 15120, Y: 10040}
	t1 := generateDummyTile(1024) // 1 KB

	// Miss before Put
	if _, ok := c.Get(coord); ok {
		t.Fatalf("expected cache miss before Put")
	}

	// Put and Get hit
	c.Put(coord, t1)
	got, ok := c.Get(coord)
	if !ok {
		t.Fatalf("expected cache hit after Put")
	}
	if len(got.Data) != len(t1.Data) {
		t.Fatalf("got data len %d; want %d", len(got.Data), len(t1.Data))
	}

	stats := c.Stats()
	if stats.Hits != 1 || stats.Misses != 1 {
		t.Fatalf("unexpected stats: %+v", stats)
	}
}

func TestShardedLRUCache_EvictionBounds(t *testing.T) {
	// Small cache: 64 KB total, ~2 KB per shard
	maxBytes := int64(64 * 1024)
	c := tile.NewShardedLRUCache(maxBytes)

	// Insert 200 tiles of 1 KB each (total 200 KB > 64 KB)
	for i := uint32(0); i < 200; i++ {
		coord := tile.TileCoord{Z: 14, X: i, Y: i * 2}
		c.Put(coord, generateDummyTile(1024))
	}

	stats := c.Stats()
	t.Logf("Cache stats after 200 inserts: %s", c.String())

	// Verify strict memory boundedness (Rule 120)
	if stats.TotalBytes > maxBytes {
		t.Fatalf("total bytes %d exceeded max quota %d", stats.TotalBytes, maxBytes)
	}
	if stats.Evictions == 0 {
		t.Fatalf("expected evictions under memory pressure, got 0")
	}
}

func TestShardedLRUCache_ConcurrentStress(t *testing.T) {
	c := tile.NewShardedLRUCache(16 * 1024 * 1024) // 16 MB

	var wg sync.WaitGroup
	workers := 16
	opsPerWorker := 1000

	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(workerID int) {
			defer wg.Done()
			for i := 0; i < opsPerWorker; i++ {
				x := uint32((workerID*opsPerWorker + i) % 100)
				coord := tile.TileCoord{Z: 12, X: x, Y: x}
				if i%2 == 0 {
					c.Put(coord, generateDummyTile(512))
				} else {
					c.Get(coord)
				}
			}
		}(w)
	}

	wg.Wait()
	t.Logf("Stress test completed: %s", c.String())
}

// BenchmarkLRU_HotGet measures single-threaded hot hit latency.
func BenchmarkLRU_HotGet(b *testing.B) {
	c := tile.NewShardedLRUCache(64 * 1024 * 1024)
	coord := tile.TileCoord{Z: 14, X: 15120, Y: 10040}
	c.Put(coord, generateDummyTile(2048))

	b.ResetTimer()
	for b.Loop() {
		_, _ = c.Get(coord)
	}
}

// BenchmarkLRU_ParallelGet measures multi-core concurrent reads.
func BenchmarkLRU_ParallelGet(b *testing.B) {
	c := tile.NewShardedLRUCache(64 * 1024 * 1024)
	coords := make([]tile.TileCoord, 32)
	for i := range coords {
		coords[i] = tile.TileCoord{Z: 14, X: uint32(15000 + i), Y: uint32(10000 + i)}
		c.Put(coords[i], generateDummyTile(2048))
	}

	b.ResetTimer()
	b.RunParallel(func(pb *testing.PB) {
		idx := 0
		for pb.Next() {
			_, _ = c.Get(coords[idx%len(coords)])
			idx++
		}
	})
}

// BenchmarkLRU_PutWithEviction measures write throughput under continuous eviction churn.
func BenchmarkLRU_PutWithEviction(b *testing.B) {
	// Small cache: 512 KB
	c := tile.NewShardedLRUCache(512 * 1024)
	tPayload := generateDummyTile(4096) // 4 KB

	b.ResetTimer()
	i := uint32(0)
	for b.Loop() {
		coord := tile.TileCoord{Z: 14, X: i % 1000, Y: (i * 3) % 1000}
		c.Put(coord, tPayload)
		i++
	}
}
