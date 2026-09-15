package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	connectcors "connectrpc.com/cors"
	"github.com/benebsworth/addr/gen/go/geo/v1/geov1connect"
	"github.com/benebsworth/addr/internal/geo"
	"github.com/benebsworth/addr/internal/service"
	"github.com/benebsworth/addr/internal/tile"
	"github.com/rs/cors"
	"golang.org/x/net/http2"
	"golang.org/x/net/http2/h2c"
)

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	if err := run(ctx, os.Args[1:], os.Getenv, os.Stdout, os.Stderr); err != nil {
		if !errors.Is(err, context.Canceled) {
			slog.Error("server exited with error", "error", err)
			os.Exit(1)
		}
	}
}

// run is the composition root, structured for testability.
func run(ctx context.Context, args []string, getenv func(string) string, stdout, stderr io.Writer) error {
	fs := flag.NewFlagSet("geoserver", flag.ContinueOnError)
	fs.SetOutput(stderr)

	var (
		port                 = fs.String("port", "8080", "Port to listen on")
		dbPath               = fs.String("db", "data/australia_geo.db", "Path to SQLite database")
		mbtilesPath          = fs.String("tiles", "data/australia_tiles.mbtiles", "Path to MBTiles database")
		tileCacheMB          = fs.Int64("tile-cache-mb", 256, "Tile in-memory LRU cache size in MB")
		tileGeneratorURL     = fs.String("tile-generator-url", "", "Base URL of the realtime AI tile generation service (e.g. http://localhost:8090)")
		tileGeneratorTimeout = fs.Duration("tile-generator-timeout", 120*time.Second, "Timeout for realtime tile generation requests")
	)

	if err := fs.Parse(args); err != nil {
		return err
	}

	if envPort := getenv("PORT"); envPort != "" {
		*port = envPort
	}
	if envDB := getenv("DB_PATH"); envDB != "" {
		*dbPath = envDB
	}
	if envTiles := getenv("TILES_PATH"); envTiles != "" {
		*mbtilesPath = envTiles
	}
	if envCacheMB := getenv("TILE_CACHE_MB"); envCacheMB != "" {
		if mb, err := strconv.ParseInt(envCacheMB, 10, 64); err == nil && mb > 0 {
			*tileCacheMB = mb
		}
	}
	if envGenerator := getenv("TILE_GENERATOR_URL"); envGenerator != "" {
		*tileGeneratorURL = envGenerator
	}
	if envGeneratorTimeout := getenv("TILE_GENERATOR_TIMEOUT"); envGeneratorTimeout != "" {
		if timeout, err := time.ParseDuration(envGeneratorTimeout); err == nil && timeout > 0 {
			*tileGeneratorTimeout = timeout
		}
	}

	logger := slog.New(slog.NewJSONHandler(stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	slog.Info("initializing offline geo store", "db", *dbPath)
	store, err := geo.NewStore(*dbPath)
	if err != nil {
		return fmt.Errorf("initializing store: %w", err)
	}
	defer store.Close()

	geoServer := service.NewGeoServer(store)
	mux := http.NewServeMux()

	// Mount Connect-Go service handler
	path, handler := geov1connect.NewGeoServiceHandler(geoServer)
	mux.Handle(path, handler)

	// Mount in-memory LRU vector tile store
	tileStore, err := tile.NewStore(*mbtilesPath, *tileCacheMB*1024*1024)
	if err != nil {
		slog.Warn("tile store initialization warning", "error", err)
	} else {
		defer tileStore.Close()
		if *tileGeneratorURL != "" {
			tileStore.SetGenerator(tile.NewGenerator(*tileGeneratorURL, *tileGeneratorTimeout))
			slog.Info("realtime AI tile generation enabled",
				"generator", *tileGeneratorURL,
				"timeout", tileGeneratorTimeout.String(),
			)
		}
		mux.Handle("/tiles/", tileStore.Handler())
	}

	// Kubernetes Liveness & Readiness endpoints
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ready"))
	})

	// Setup CORS for Connect-Web frontend
	corsHandler := cors.New(cors.Options{
		AllowedOrigins: []string{"*"},
		AllowedMethods: connectcors.AllowedMethods(),
		AllowedHeaders: connectcors.AllowedHeaders(),
		ExposedHeaders: connectcors.ExposedHeaders(),
		MaxAge:         7200,
	})

	// Wrap in h2c (HTTP/2 cleartext) for gRPC, Connect, and HTTP/1.1
	h2Handler := h2c.NewHandler(corsHandler.Handler(mux), &http2.Server{})

	server := &http.Server{
		Addr:              net.JoinHostPort("", *port),
		Handler:           h2Handler,
		ReadHeaderTimeout: 5 * time.Second,
		BaseContext: func(net.Listener) context.Context {
			return ctx
		},
	}

	serverErr := make(chan error, 1)
	go func() {
		slog.Info("starting connect-go geo microservice", "port", *port, "protocol", "connect/grpc/http2")
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serverErr <- err
		}
	}()

	select {
	case <-ctx.Done():
		slog.Info("shutdown signal received, draining active requests...")
		shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer shutdownCancel()
		return server.Shutdown(shutdownCtx)
	case err := <-serverErr:
		return fmt.Errorf("http server failed: %w", err)
	}
}
