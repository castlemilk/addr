package tile

import (
	"container/list"
	"fmt"
	"sync"
	"sync/atomic"
)

// TileCoord represents a standard Web Mercator XYZ tile coordinate.
type TileCoord struct {
	Z uint32
	X uint32
	Y uint32
}

// Key returns a unique 64-bit integer identifier for the tile.
func (tc TileCoord) Key() uint64 {
	return (uint64(tc.Z) << 56) | (uint64(tc.X) << 28) | uint64(tc.Y)
}

// Tile represents a cached vector or raster tile payload.
type Tile struct {
	Data        []byte
	ContentType string
	ETag        string
	Generated   bool
}

// CacheStats tracks high-level operational metrics for observability.
type CacheStats struct {
	Hits       uint64  `json:"hits"`
	Misses     uint64  `json:"misses"`
	HitRatio   float64 `json:"hit_ratio"`
	Evictions  uint64  `json:"evictions"`
	TotalBytes int64   `json:"total_bytes"`
	TotalItems int64   `json:"total_items"`
	MaxBytes   int64   `json:"max_bytes"`
}

type cacheEntry struct {
	key       uint64
	tile      Tile
	sizeBytes int64
}

// lruShard is a single concurrent shard of the LRU cache.
type lruShard struct {
	mu           sync.Mutex
	items        map[uint64]*list.Element
	evictList    *list.List
	currentBytes int64
	maxBytes     int64
}

// ShardedLRUCache implements a concurrent, lock-striped in-memory LRU tile cache.
// Sharding across 32 independent partitions eliminates global mutex contention.
type ShardedLRUCache struct {
	shards    [32]*lruShard
	hits      atomic.Uint64
	misses    atomic.Uint64
	evictions atomic.Uint64
	maxBytes  int64
}

// NewShardedLRUCache creates an in-memory LRU cache bounded by totalMaxBytes.
func NewShardedLRUCache(totalMaxBytes int64) *ShardedLRUCache {
	if totalMaxBytes <= 0 {
		totalMaxBytes = 256 * 1024 * 1024 // default 256 MB
	}

	c := &ShardedLRUCache{
		maxBytes: totalMaxBytes,
	}

	bytesPerShard := totalMaxBytes / 32
	for i := 0; i < 32; i++ {
		c.shards[i] = &lruShard{
			items:     make(map[uint64]*list.Element),
			evictList: list.New(),
			maxBytes:  bytesPerShard,
		}
	}

	return c
}

// shardIndex computes the partition index using FNV-1a style bit mixing.
func (c *ShardedLRUCache) shardIndex(key uint64) int {
	key ^= key >> 33
	key *= 0xff51afd7ed558ccd
	key ^= key >> 33
	key *= 0xc4ceb9fe1a85ec53
	key ^= key >> 33
	return int(key & 31)
}

// Get retrieves a tile from the cache, promoting it to the head of the LRU.
func (c *ShardedLRUCache) Get(coord TileCoord) (Tile, bool) {
	key := coord.Key()
	shard := c.shards[c.shardIndex(key)]

	shard.mu.Lock()
	if elem, ok := shard.items[key]; ok {
		shard.evictList.MoveToFront(elem)
		t := elem.Value.(*cacheEntry).tile
		shard.mu.Unlock()
		c.hits.Add(1)
		return t, true
	}
	shard.mu.Unlock()

	c.misses.Add(1)
	return Tile{}, false
}

// Put adds or updates a tile in the cache, evicting least-recently used tiles
// when the shard exceeds its memory quota.
func (c *ShardedLRUCache) Put(coord TileCoord, tile Tile) {
	key := coord.Key()
	entrySize := int64(len(tile.Data)) + 128 // payload + struct overhead
	shard := c.shards[c.shardIndex(key)]

	shard.mu.Lock()
	defer shard.mu.Unlock()

	// Update existing item
	if elem, ok := shard.items[key]; ok {
		shard.evictList.MoveToFront(elem)
		entry := elem.Value.(*cacheEntry)
		shard.currentBytes += (entrySize - entry.sizeBytes)
		entry.tile = tile
		entry.sizeBytes = entrySize
		c.evictIfNeeded(shard)
		return
	}

	// Insert new item
	entry := &cacheEntry{
		key:       key,
		tile:      tile,
		sizeBytes: entrySize,
	}
	elem := shard.evictList.PushFront(entry)
	shard.items[key] = elem
	shard.currentBytes += entrySize

	c.evictIfNeeded(shard)
}

// evictIfNeeded evicts the oldest items at the tail until within maxBytes.
func (c *ShardedLRUCache) evictIfNeeded(shard *lruShard) {
	for shard.currentBytes > shard.maxBytes && shard.evictList.Len() > 0 {
		oldest := shard.evictList.Back()
		if oldest == nil {
			break
		}

		entry := oldest.Value.(*cacheEntry)
		delete(shard.items, entry.key)
		shard.evictList.Remove(oldest)
		shard.currentBytes -= entry.sizeBytes
		c.evictions.Add(1)
	}
}

// Clear flushes all cached tiles across all shards.
func (c *ShardedLRUCache) Clear() {
	for _, shard := range c.shards {
		shard.mu.Lock()
		shard.items = make(map[uint64]*list.Element)
		shard.evictList.Init()
		shard.currentBytes = 0
		shard.mu.Unlock()
	}
}

// Stats returns a snapshot of cache metrics.
func (c *ShardedLRUCache) Stats() CacheStats {
	hits := c.hits.Load()
	misses := c.misses.Load()
	totalLookups := hits + misses
	var ratio float64
	if totalLookups > 0 {
		ratio = float64(hits) / float64(totalLookups)
	}

	var totalBytes, totalItems int64
	for _, shard := range c.shards {
		shard.mu.Lock()
		totalBytes += shard.currentBytes
		totalItems += int64(len(shard.items))
		shard.mu.Unlock()
	}

	return CacheStats{
		Hits:       hits,
		Misses:     misses,
		HitRatio:   ratio,
		Evictions:  c.evictions.Load(),
		TotalBytes: totalBytes,
		TotalItems: totalItems,
		MaxBytes:   c.maxBytes,
	}
}

// String formats the stats as human-readable text.
func (c *ShardedLRUCache) String() string {
	s := c.Stats()
	return fmt.Sprintf("LRUCache[items=%d, bytes=%d/%d, hit_ratio=%.2f%%, hits=%d, misses=%d, evictions=%d]",
		s.TotalItems, s.TotalBytes, s.MaxBytes, s.HitRatio*100.0, s.Hits, s.Misses, s.Evictions)
}
