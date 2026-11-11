// Command annotation-migrate imports and restores verified frozen local datasets.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strings"

	"github.com/benebsworth/addr/internal/annotation/migration"
)

const help = `annotation-migrate preserves frozen Mooroolbark state and editable projections.

Commands:
  plan     Verify all blobs and file reconstruction; validate every scene projection.
  import   Verify, upload missing immutable assets, then create matching documents.
  upload   Verify and upload immutable assets only (resumes by SHA-256).
  verify   Compare remote documents and all asset metadata with the frozen bundle.
  pull     Download raw assets, latest editable documents and append-only history.
  restore  Reconstruct original relative files and mtimes into a new directory.

Examples:
  annotation-migrate plan --bundle data/annotation-snapshots/BUNDLE --plan-file /tmp/plan.json
  annotation-migrate import --bundle PATH --url https://fieldwork.example --token-file PATH --allow-live
  (upload/import refuse a non-loopback --url unless --allow-live or FIELDWORK_PUSH_ALLOW_LIVE=1 is set)
  annotation-migrate verify --bundle PATH --url URL --deep
  annotation-migrate pull --url URL --project mooroolbark --output /tmp/pulled-bundle
  annotation-migrate restore --bundle /tmp/pulled-bundle --output /tmp/restored-source

Auth uses ANNOTATION_TOKEN or --token-file. Server defaults to ANNOTATION_URL or
http://127.0.0.1:8080. Local plan/restore never read authentication credentials.
Imports refuse non-identical existing documents and never promote machine output
to human approval. A changed snapshot requires a new project or explicit review.
Pull emits fieldwork-state.ndjson and fieldwork-changes.ndjson beside manifest.json.
Restore recovers the frozen files; the separate editable state retains later edits.`

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	if err := run(ctx, os.Args[1:], os.Stdout, os.Stderr); err != nil {
		fmt.Fprintln(os.Stderr, "annotation-migrate:", err)
		os.Exit(1)
	}
}

func run(ctx context.Context, args []string, output, diagnostics io.Writer) error {
	if len(args) == 0 || args[0] == "help" || args[0] == "--help" {
		_, err := fmt.Fprintln(output, help)
		return err
	}
	command := args[0]
	switch command {
	case "plan", "import", "upload", "verify", "pull", "restore":
	default:
		return fmt.Errorf("unknown command %q", command)
	}
	var directory, server, tokenFile, project, destination, planFile string
	var workers int
	var deep, allowLive bool
	fs := flag.NewFlagSet(command, flag.ContinueOnError)
	fs.SetOutput(diagnostics)
	fs.StringVar(&directory, "bundle", "", "frozen bundle directory")
	fs.StringVar(&server, "url", env("ANNOTATION_URL", "http://127.0.0.1:8080"), "Fieldwork server origin")
	fs.StringVar(&tokenFile, "token-file", "", "workspace token file (remote commands only)")
	fs.StringVar(&project, "project", "mooroolbark", "target project ID")
	fs.StringVar(&destination, "output", "", "new destination directory (pull or restore)")
	fs.StringVar(&planFile, "plan-file", "", "write complete projected documents to JSON")
	fs.IntVar(&workers, "concurrency", 4, "concurrent asset transfers (1-64; uploads reserve at most 2 workers for files over 8 MiB)")
	fs.BoolVar(&deep, "deep", false, "GET and hash every remote asset during verify")
	fs.BoolVar(&allowLive, "allow-live", false, "permit upload/import against a non-loopback origin (or set FIELDWORK_PUSH_ALLOW_LIVE=1)")
	if err := fs.Parse(args[1:]); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if fs.NArg() != 0 {
		return fmt.Errorf("unexpected argument %q", fs.Arg(0))
	}
	if workers < 1 || workers > 64 {
		return fmt.Errorf("concurrency must be 1-64")
	}
	write := func(value any) error {
		enc := json.NewEncoder(output)
		enc.SetIndent("", "  ")
		return enc.Encode(value)
	}
	var bundle *migration.Bundle
	var plan *migration.Plan
	if command != "pull" {
		if directory == "" {
			return fmt.Errorf("--bundle is required")
		}
		var err error
		bundle, err = migration.OpenBundle(directory)
		if err != nil {
			return err
		}
		fmt.Fprintln(diagnostics, "Verifying frozen assets and whole-file reconstruction…")
		if err := bundle.Verify(ctx); err != nil {
			return err
		}
		if command == "restore" {
			if destination == "" {
				return fmt.Errorf("--output is required")
			}
			if err := bundle.Restore(ctx, destination); err != nil {
				return err
			}
			return write(map[string]any{"snapshotId": bundle.Manifest.SnapshotID, "restoredFiles": len(bundle.Files), "directory": destination})
		}
		if command != "upload" {
			plan, err = migration.BuildPlan(ctx, bundle, project)
			if err != nil {
				return err
			}
			if planFile != "" {
				data, err := json.Marshal(plan)
				if err != nil {
					return err
				}
				if err := os.WriteFile(planFile, data, 0600); err != nil {
					return err
				}
			}
		}
		if command == "plan" {
			return write(map[string]any{"snapshotId": plan.SnapshotID, "manifest": plan.Manifest, "projectId": plan.Project.ID, "stats": plan.Stats, "planFile": planFile})
		}
	}
	token := os.Getenv("ANNOTATION_TOKEN")
	if tokenFile != "" {
		data, err := os.ReadFile(tokenFile)
		if err != nil {
			return fmt.Errorf("read token file: %w", err)
		}
		token = strings.TrimSpace(string(data))
	}
	if command == "upload" || command == "import" {
		if err := migration.GuardLiveWrite(server, "annotation-migrate "+command, allowLive); err != nil {
			return err
		}
	}
	client, err := migration.NewClient(server, token)
	if err != nil {
		return err
	}
	switch command {
	case "upload":
		result, err := client.Upload(ctx, bundle, workers, diagnostics)
		if err != nil {
			return err
		}
		return write(result)
	case "import":
		result, err := client.Upload(ctx, bundle, workers, diagnostics)
		if err != nil {
			return err
		}
		applied, err := client.Apply(ctx, plan)
		if err != nil {
			return err
		}
		result.CreatedDocuments = applied.CreatedDocuments
		result.ExistingDocuments = applied.ExistingDocuments
		return write(result)
	case "verify":
		result, err := client.Verify(ctx, bundle, plan, workers, deep)
		if err != nil {
			return err
		}
		return write(result)
	case "pull":
		if destination == "" {
			return fmt.Errorf("--output is required")
		}
		result, err := client.Pull(ctx, project, destination, workers)
		if err != nil {
			return err
		}
		return write(result)
	}
	return nil
}

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
