// annotation-server serves the generic Fieldwork annotation API independently of
// the address/roof stack. Cloud mode requires Postgres and a bearer credential.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/benebsworth/addr/internal/annotation"
)

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func main() {
	if err := run(); err != nil {
		log.Print(err)
		os.Exit(1)
	}
}

func run() error {
	flags := flag.NewFlagSet("annotation-server", flag.ContinueOnError)
	listen := flags.String("listen", env("ANNOTATION_LISTEN", ":8080"), "HTTP listen address; --dev defaults to 127.0.0.1:8080")
	dev := flags.Bool("dev", false, "Explicit loopback-only development mode (no bearer required)")
	sqlite := flags.String("sqlite", "", "Explicit SQLite development database path; requires --dev")
	migrate := flags.Bool("migrate", false, "Apply embedded migrations and exit; Postgres uses DATABASE_URL_UNPOOLED")
	static := flags.String("static", os.Getenv("ANNOTATION_STATIC_DIR"), "Optional built SPA directory containing index.html")
	assets := flags.String("asset-dir", os.Getenv("ANNOTATION_ASSET_DIR"), "Optional local content-addressed asset directory (cloud Worker uses R2)")
	if err := flags.Parse(os.Args[1:]); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if flags.NArg() != 0 {
		return errors.New("unexpected arguments; see --help")
	}
	if *dev {
		if *listen == ":8080" {
			*listen = "127.0.0.1:8080"
		}
		host, _, err := net.SplitHostPort(*listen)
		ip := net.ParseIP(host)
		if err != nil || (host != "localhost" && (ip == nil || !ip.IsLoopback())) {
			return errors.New("--dev requires a loopback listen address")
		}
	} else if *sqlite != "" {
		return errors.New("--sqlite requires explicit --dev; cloud mode always uses Postgres")
	}
	driver, dsn := "postgres", os.Getenv("DATABASE_URL")
	if *sqlite != "" {
		driver, dsn = "sqlite", *sqlite
	}
	if *migrate && driver == "postgres" {
		dsn = os.Getenv("DATABASE_URL_UNPOOLED")
		if dsn == "" {
			return errors.New("DATABASE_URL_UNPOOLED is required for migrations")
		}
		u, err := url.Parse(dsn)
		if err != nil || strings.Contains(u.Hostname(), "-pooler") {
			return errors.New("migrations require a direct Postgres URL, not a pooled hostname")
		}
	}
	if dsn == "" {
		return errors.New("DATABASE_URL is required; for explicit local development use --dev --sqlite /path/to/annotations.db")
	}
	if !*dev && !*migrate && len(os.Getenv("ANNOTATION_TOKEN")) < 24 {
		return errors.New("ANNOTATION_TOKEN must contain at least 24 characters in cloud mode")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	store, err := annotation.OpenStore(ctx, driver, dsn, *migrate || *dev && driver == "sqlite")
	cancel()
	if err != nil {
		return err
	}
	defer store.Close()
	if *migrate {
		fmt.Println("Annotation schema version 1 is ready.")
		return nil
	}
	var workspaces *annotation.Workspaces
	if os.Getenv("FIELDWORK_SHARED_WORKSPACES") == "true" {
		workspaceContext, cancelWorkspace := context.WithTimeout(context.Background(), 30*time.Second)
		workspaces, err = annotation.NewWorkspaces(workspaceContext, store, os.Getenv("FIELDWORK_LEGACY_OWNER_EMAIL"))
		cancelWorkspace()
		if err != nil {
			return errors.New("cannot initialize private workspace registry")
		}
	}
	handler, err := annotation.NewHTTPHandler(store, annotation.HTTPConfig{Workspaces: workspaces, IdentityBridgeKey: os.Getenv("FIELDWORK_IDENTITY_BRIDGE_KEY"), Token: os.Getenv("ANNOTATION_TOKEN"), Dev: *dev, AllowedOrigins: strings.Split(os.Getenv("ANNOTATION_ALLOWED_ORIGINS"), ","), StaticDir: *static, AssetDir: *assets})
	if err != nil {
		return err
	}
	server := &http.Server{Addr: *listen, Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 35 * time.Second, WriteTimeout: 35 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 32 << 10}
	errCh := make(chan error, 1)
	go func() { errCh <- server.ListenAndServe() }()
	log.Printf("Fieldwork annotation API listening on %s (storage=%s, local-dev=%t)", *listen, driver, *dev)
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(stop)
	select {
	case err := <-errCh:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-stop:
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return server.Shutdown(ctx)
	}
}
