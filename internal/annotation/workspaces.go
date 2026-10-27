package annotation

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"regexp"
	"strings"
	"time"
)

// Workspace identifiers are generated here, never selected by request headers,
// document fields or query parameters. The original workspace keeps its tables.
type Workspace struct {
	ID    string `json:"id"`
	Name  string `json:"name"`
	store *SQLStore
}
type workspaceKey struct{}
type Workspaces struct {
	root             *SQLStore
	legacyOwnerEmail string
}

var namespacePattern = regexp.MustCompile(`^fw_[a-f0-9]{32}$`)
var annotationTables = []string{"annotation_documents_project", "annotation_changes_project", "annotation_migrations", "annotation_documents", "annotation_tombstones", "annotation_clock", "annotation_changes"}

func (s *SQLStore) query(query string) string {
	if s.namespace == "" {
		return query
	}
	if !namespacePattern.MatchString(s.namespace) {
		panic("invalid internal workspace namespace")
	}
	args := []string{}
	for _, table := range annotationTables {
		target := s.namespace + "_" + table
		if s.driver == "postgres" {
			target = `"` + s.namespace + `".` + table
		}
		args = append(args, table, target)
	}
	return strings.NewReplacer(args...).Replace(query)
}

// NewWorkspaces installs only an additive registry. Core schema version 1 and
// all legacy records, IDs, revisions and change cursors remain untouched.
func NewWorkspaces(ctx context.Context, root *SQLStore, legacyOwnerEmail string) (*Workspaces, error) {
	_, err := root.db.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS fieldwork_workspaces (
		id TEXT PRIMARY KEY, owner_uid TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
		namespace TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)`)
	if err != nil {
		return nil, err
	}
	return &Workspaces{root: root, legacyOwnerEmail: strings.ToLower(legacyOwnerEmail)}, nil
}

func (w *Workspaces) legacy() Workspace {
	return Workspace{ID: "legacy", Name: "Fieldwork", store: w.root}
}

func (w *Workspaces) resolve(ctx context.Context, identity BridgeIdentity) (Workspace, error) {
	if identity.UID == "" || len(identity.UID) > 128 {
		return Workspace{}, errors.New("missing service identity")
	}
	tx, err := w.root.db.BeginTx(ctx, nil)
	if err != nil {
		return Workspace{}, err
	}
	defer tx.Rollback()
	// Serialize first-time workspace creation, including the one-time owner
	// claim. Existing requests only take this lock for the short registry read.
	if w.root.driver == "postgres" {
		if _, err = tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock(716124915)"); err != nil {
			return Workspace{}, err
		}
	}
	var result Workspace
	var namespace string
	err = tx.QueryRowContext(ctx, "SELECT id,name,namespace FROM fieldwork_workspaces WHERE owner_uid=$1", identity.UID).Scan(&result.ID, &result.Name, &namespace)
	if errors.Is(err, sql.ErrNoRows) {
		var random [16]byte
		if _, err = rand.Read(random[:]); err != nil {
			return Workspace{}, err
		}
		suffix := hex.EncodeToString(random[:])
		result.ID, result.Name, namespace = "ws_"+suffix, "My workspace", "fw_"+suffix
		if w.legacyOwnerEmail != "" && identity.EmailVerified && identity.Provider == "google.com" && strings.EqualFold(identity.Email, w.legacyOwnerEmail) {
			// namespace is UNIQUE: a different UID cannot later claim the old
			// owner's workspace through a duplicate/changed email identity.
			result, namespace = w.legacy(), ""
		}
		if namespace != "" {
			if err = w.createTables(ctx, tx, namespace); err != nil {
				return Workspace{}, err
			}
		}
		_, err = tx.ExecContext(ctx, "INSERT INTO fieldwork_workspaces(id,owner_uid,name,namespace,created_at) VALUES($1,$2,$3,$4,$5)", result.ID, identity.UID, result.Name, namespace, time.Now().UTC().Format(time.RFC3339Nano))
	}
	if err != nil {
		return Workspace{}, err
	}
	if namespace != "" && !namespacePattern.MatchString(namespace) {
		return Workspace{}, errors.New("invalid stored workspace namespace")
	}
	if err = tx.Commit(); err != nil {
		return Workspace{}, err
	}
	result.store = &SQLStore{db: w.root.db, driver: w.root.driver, adapters: w.root.adapters, namespace: namespace}
	return result, nil
}

func (w *Workspaces) createTables(ctx context.Context, tx *sql.Tx, namespace string) error {
	if !namespacePattern.MatchString(namespace) {
		return errors.New("invalid workspace namespace")
	}
	data, err := migrations.ReadFile("migrations/001_" + w.root.driver + ".sql")
	if err != nil {
		return err
	}
	query := string(data)
	originalSearchPath := ""
	if w.root.driver == "postgres" {
		if err = tx.QueryRowContext(ctx, "SHOW search_path").Scan(&originalSearchPath); err != nil {
			return err
		}
		if _, err = tx.ExecContext(ctx, `CREATE SCHEMA "`+namespace+`"`); err != nil {
			return err
		}
		// SET LOCAL affects only this transaction, including through a pool.
		if _, err = tx.ExecContext(ctx, `SET LOCAL search_path TO "`+namespace+`"`); err != nil {
			return err
		}
	} else {
		query = (&SQLStore{driver: "sqlite", namespace: namespace}).query(query)
	}
	_, err = tx.ExecContext(ctx, query)
	if err != nil {
		return err
	}
	if w.root.driver == "postgres" {
		// The registry is in the connection's original schema. Restore the
		// original search path before inserting its record.
		_, err = tx.ExecContext(ctx, "SELECT set_config('search_path',$1,true)", originalSearchPath)
	}
	return err
}

// A secure Store refuses a missing scope. It never silently falls back to the
// original owner workspace if a transport drops the authenticated context.
type workspaceStore struct{ root *SQLStore }

func scoped(ctx context.Context) (*SQLStore, error) {
	w, ok := ctx.Value(workspaceKey{}).(Workspace)
	if !ok || w.store == nil {
		return nil, errors.New("authenticated workspace context is required")
	}
	return w.store, nil
}
func (s workspaceStore) Get(c context.Context, k, id string) (json.RawMessage, error) {
	v, e := scoped(c)
	if e != nil {
		return nil, e
	}
	return v.Get(c, k, id)
}
func (s workspaceStore) List(c context.Context, k string, o ListOptions) ([]json.RawMessage, string, error) {
	v, e := scoped(c)
	if e != nil {
		return nil, "", e
	}
	return v.List(c, k, o)
}
func (s workspaceStore) Save(c context.Context, k, id string, r int64, a string, d json.RawMessage) (json.RawMessage, error) {
	v, e := scoped(c)
	if e != nil {
		return nil, e
	}
	return v.Save(c, k, id, r, a, d)
}
func (s workspaceStore) Delete(c context.Context, k, id string, r int64, a string) error {
	v, e := scoped(c)
	if e != nil {
		return e
	}
	return v.Delete(c, k, id, r, a)
}
func (s workspaceStore) Changes(c context.Context, p, n string, l int) (ChangePage, error) {
	v, e := scoped(c)
	if e != nil {
		return ChangePage{}, e
	}
	return v.Changes(c, p, n, l)
}
func (s workspaceStore) Export(c context.Context, p string) (Export, error) {
	v, e := scoped(c)
	if e != nil {
		return Export{}, e
	}
	return v.Export(c, p)
}
func (s workspaceStore) Ping(c context.Context) error { return s.root.Ping(c) }
func (s workspaceStore) Close() error                 { return nil }
func (s workspaceStore) Driver() string               { return s.root.Driver() }
func (s workspaceStore) AdapterInfos() []AdapterInfo  { return s.root.AdapterInfos() }
