// Command crmctl manages CRM users, roles, and API keys.
//
// Examples:
//
//	crmctl create-user --name alice --roles admin
//	crmctl create-key  --user alice --label laptop [--scopes crm.read,crm.poi.write]
//	crmctl list-users
//	crmctl list-keys   [--user alice]
//	crmctl revoke-key  --id 3
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"strings"

	"github.com/benebsworth/addr/internal/crm"
)

func main() {
	if err := run(context.Background(), os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func run(ctx context.Context, args []string) error {
	if len(args) == 0 {
		return usage()
	}
	command, rest := args[0], args[1:]
	switch command {
	case "create-user":
		return createUser(ctx, rest)
	case "list-users":
		return listUsers(ctx, rest)
	case "create-key":
		return createKey(ctx, rest)
	case "list-keys":
		return listKeys(ctx, rest)
	case "revoke-key":
		return revokeKey(ctx, rest)
	case "roles":
		for role, scopes := range crm.RoleScopes {
			fmt.Printf("%-8s %s\n", role, strings.Join(scopes, ", "))
		}
		return nil
	default:
		return usage()
	}
}

func openStore(dbPath string) (*crm.Store, error) {
	if dbPath == "" {
		dbPath = envOr("DB_PATH", "data/australia_geo.db")
	}
	return crm.NewStore(dbPath)
}

func usage() error {
	return fmt.Errorf(`usage:
  crmctl create-user --name NAME [--roles admin,editor,viewer] [--db PATH]
  crmctl list-users [--db PATH]
  crmctl create-key  --user NAME [--label LABEL] [--scopes scope1,scope2] [--db PATH]
  crmctl list-keys   [--user NAME] [--db PATH]
  crmctl revoke-key  --id N [--db PATH]
  crmctl roles`)
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func createUser(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("create-user", flag.ContinueOnError)
	db := fs.String("db", "", "Path to SQLite database")
	name := fs.String("name", "", "user name")
	roles := fs.String("roles", "viewer", "comma-separated roles (viewer, editor, admin)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *name == "" {
		return fmt.Errorf("--name is required")
	}
	store, err := openStore(*db)
	if err != nil {
		return err
	}
	defer store.Close()
	user, err := store.CreateUser(ctx, *name, splitList(*roles))
	if err != nil {
		return err
	}
	fmt.Printf("created user %d %q roles=%v\n", user.ID, user.Name, user.Roles)
	return nil
}

func listUsers(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("list-users", flag.ContinueOnError)
	db := fs.String("db", "", "Path to SQLite database")
	if err := fs.Parse(args); err != nil {
		return err
	}
	store, err := openStore(*db)
	if err != nil {
		return err
	}
	defer store.Close()
	users, err := store.ListUsers(ctx)
	if err != nil {
		return err
	}
	for _, user := range users {
		status := "active"
		if !user.Active {
			status = "disabled"
		}
		fmt.Printf("%3d  %-20s %-9s roles=%s\n", user.ID, user.Name, status, strings.Join(user.Roles, ","))
	}
	return nil
}

func createKey(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("create-key", flag.ContinueOnError)
	db := fs.String("db", "", "Path to SQLite database")
	user := fs.String("user", "", "user name")
	label := fs.String("label", "", "key label (e.g. laptop)")
	scopes := fs.String("scopes", "", "optional comma-separated scope restriction")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *user == "" {
		return fmt.Errorf("--user is required")
	}
	store, err := openStore(*db)
	if err != nil {
		return err
	}
	defer store.Close()
	secret, key, err := store.CreateAPIKey(ctx, *user, *label, splitList(*scopes))
	if err != nil {
		return err
	}
	fmt.Printf("created key %d for %s\n", key.ID, key.UserName)
	fmt.Printf("API key (store it now, it is not shown again):\n  %s\n", secret)
	return nil
}

func listKeys(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("list-keys", flag.ContinueOnError)
	db := fs.String("db", "", "Path to SQLite database")
	user := fs.String("user", "", "filter by user name")
	if err := fs.Parse(args); err != nil {
		return err
	}
	store, err := openStore(*db)
	if err != nil {
		return err
	}
	defer store.Close()
	keys, err := store.ListAPIKeys(ctx, *user)
	if err != nil {
		return err
	}
	for _, key := range keys {
		status := "active"
		if key.RevokedAt != nil {
			status = "revoked"
		}
		scopes := "inherit"
		if len(key.Scopes) > 0 {
			scopes = strings.Join(key.Scopes, ",")
		}
		fmt.Printf("%3d  %-14s %-20s %-8s %-8s scopes=%s last_used=%s\n",
			key.ID, key.Prefix+"…", key.UserName, key.Label, status, scopes,
			derefOr(key.LastUsedAt, "never"))
	}
	return nil
}

func revokeKey(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("revoke-key", flag.ContinueOnError)
	db := fs.String("db", "", "Path to SQLite database")
	id := fs.Int64("id", 0, "key id")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *id <= 0 {
		return fmt.Errorf("--id is required")
	}
	store, err := openStore(*db)
	if err != nil {
		return err
	}
	defer store.Close()
	if err := store.RevokeAPIKey(ctx, *id); err != nil {
		return err
	}
	fmt.Printf("revoked key %d\n", *id)
	return nil
}

func splitList(value string) []string {
	var out []string
	for _, part := range strings.Split(value, ",") {
		if trimmed := strings.TrimSpace(part); trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}

func derefOr(value *string, fallback string) string {
	if value == nil {
		return fallback
	}
	return *value
}
