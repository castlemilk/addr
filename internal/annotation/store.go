package annotation

import (
	"context"
	"database/sql"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/lib/pq"
	_ "modernc.org/sqlite"
)

//go:embed migrations/*.sql
var migrations embed.FS

const MaxDocumentBytes = 2 << 20
const MaxSafeInteger int64 = 9007199254740991

// Store is shared by Connect, REST, CLI and MCP. SQLite is an explicitly selected
// development implementation of the same SQL semantics; production uses Postgres.
type Store interface {
	Get(context.Context, string, string) (json.RawMessage, error)
	List(context.Context, string, ListOptions) ([]json.RawMessage, string, error)
	Save(context.Context, string, string, int64, string, json.RawMessage) (json.RawMessage, error)
	Delete(context.Context, string, string, int64, string) error
	Changes(context.Context, string, string, int) (ChangePage, error)
	Export(context.Context, string) (Export, error)
	Ping(context.Context) error
	Close() error
	Driver() string
	AdapterInfos() []AdapterInfo
}

type ListOptions struct {
	ProjectID, Cursor, Query, Status, Adapter, Tag string
	Limit                                          int
}
type SQLStore struct {
	db        *sql.DB
	driver    string
	adapters  map[string]Adapter
	namespace string
}

type StoreOption func(*SQLStore)

// WithAdapters registers additional source validators at startup. Adapters must
// use a distinct ID; callers choose extensions explicitly, never through data.
func WithAdapters(adapters ...Adapter) StoreOption {
	return func(s *SQLStore) {
		for _, adapter := range adapters {
			s.adapters[adapter.Info().ID] = adapter
		}
	}
}

func OpenStore(ctx context.Context, driver, dsn string, migrate bool, options ...StoreOption) (*SQLStore, error) {
	if driver != "postgres" && driver != "sqlite" {
		return nil, invalid("database driver must be postgres or sqlite")
	}
	var db *sql.DB
	var err error
	if driver == "postgres" {
		// Let pq parse both URL and keyword DSNs, including escaped credentials.
		config, configErr := pq.NewConfig(dsn)
		if configErr != nil {
			return nil, errors.New("cannot configure annotation database")
		}
		// Keep Parse/Bind/Execute in one round trip. A transaction pool may
		// reassign the backend after a separate Describe/Sync, losing or
		// replacing the unnamed statement before its parameters are bound.
		config.BinaryParameters = true
		connector, configErr := pq.NewConnectorConfig(config)
		if configErr != nil {
			return nil, errors.New("cannot configure annotation database")
		}
		db = sql.OpenDB(connector)
	} else {
		db, err = sql.Open(driver, dsn)
		if err != nil {
			return nil, errors.New("cannot configure annotation database")
		}
	}
	if driver == "sqlite" {
		db.SetMaxOpenConns(1)
	} else {
		db.SetMaxOpenConns(10)
		db.SetMaxIdleConns(2)
		db.SetConnMaxLifetime(5 * time.Minute)
	}
	s := &SQLStore{db: db, driver: driver, adapters: DefaultAdapters()}
	for _, option := range options {
		option(s)
	}
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, errors.New("annotation database connection failed; check DATABASE_URL and network access")
	}
	if driver == "sqlite" {
		if _, err = db.ExecContext(ctx, "PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;"); err != nil {
			db.Close()
			return nil, err
		}
	}
	if migrate {
		err = s.Migrate(ctx)
	} else {
		var version int
		err = db.QueryRowContext(ctx, "SELECT MAX(version) FROM annotation_migrations").Scan(&version)
		if err != nil || version != 1 {
			err = errors.New("annotation schema is missing or unsupported; run annotation-server --migrate with DATABASE_URL_UNPOOLED")
		}
	}
	if err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *SQLStore) Migrate(ctx context.Context) error {
	data, err := migrations.ReadFile("migrations/001_" + s.driver + ".sql")
	if err != nil {
		return err
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if s.driver == "postgres" {
		if _, err := tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock(716124914)"); err != nil {
			return err
		}
	}
	if _, err := tx.ExecContext(ctx, string(data)); err != nil {
		return fmt.Errorf("apply annotation migration: %w", err)
	}
	return tx.Commit()
}

func (s *SQLStore) Driver() string { return s.driver }
func (s *SQLStore) AdapterInfos() []AdapterInfo {
	result := []AdapterInfo{}
	for _, a := range s.adapters {
		result = append(result, a.Info())
	}
	return result
}
func (s *SQLStore) Ping(ctx context.Context) error { return s.db.PingContext(ctx) }
func (s *SQLStore) Close() error                   { return s.db.Close() }

type queryer interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}

func (s *SQLStore) getDocument(ctx context.Context, q queryer, kind, id string) (json.RawMessage, error) {
	var data []byte
	err := q.QueryRowContext(ctx, s.query("SELECT body FROM annotation_documents WHERE kind=$1 AND id=$2"), kind, id).Scan(&data)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return data, err
}

func (s *SQLStore) Get(ctx context.Context, kind, id string) (json.RawMessage, error) {
	return s.getDocument(ctx, s.db, kind, id)
}

func boundedLimit(n int) int {
	if n <= 0 {
		return 100
	}
	if n > 500 {
		return 500
	}
	return n
}

func (s *SQLStore) List(ctx context.Context, kind string, opts ListOptions) ([]json.RawMessage, string, error) {
	return s.list(ctx, s.db, kind, opts)
}

func (s *SQLStore) list(ctx context.Context, q queryer, kind string, opts ListOptions) ([]json.RawMessage, string, error) {
	args := []any{kind}
	where := "kind=$1"
	add := func(expr string, arg any) { args = append(args, arg); where += " AND " + fmt.Sprintf(expr, len(args)) }
	if opts.ProjectID != "" {
		add("project_id=$%d", opts.ProjectID)
	}
	if opts.Cursor != "" {
		if err := checkID(opts.Cursor); err != nil {
			return nil, "", err
		}
		add("id>$%d", opts.Cursor)
	}
	field := func(name string) string {
		if s.driver == "postgres" {
			return "body->>'" + name + "'"
		}
		return "json_extract(body, '$." + name + "')"
	}
	if opts.Status != "" {
		add(field("status")+"=$%d", opts.Status)
	}
	if opts.Adapter != "" {
		add(field("adapter")+"=$%d", opts.Adapter)
	}
	if opts.Query != "" {
		// LIKE wildcards are escaped: search is a literal substring.
		query := strings.NewReplacer("\\", "\\\\", "%", "\\%", "_", "\\_").Replace(strings.ToLower(opts.Query))
		searchField := "title"
		if kind == "project" || kind == "label" {
			searchField = "name"
		}
		add("LOWER("+field(searchField)+") LIKE $%d ESCAPE '\\'", "%"+query+"%")
	}
	if opts.Tag != "" {
		// Exact tag, case-insensitive (the web queue normalises tags to lower case).
		if s.driver == "postgres" {
			add("EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE(body->'tags','[]'::jsonb)) AS t(v) WHERE LOWER(t.v)=LOWER($%d))", opts.Tag)
		} else {
			add("EXISTS (SELECT 1 FROM json_each(body,'$.tags') WHERE LOWER(value)=LOWER($%d))", opts.Tag)
		}
	}
	limit := boundedLimit(opts.Limit)
	args = append(args, limit+1)
	rows, err := q.QueryContext(ctx, s.query("SELECT id,body FROM annotation_documents WHERE ")+where+fmt.Sprintf(" ORDER BY id LIMIT $%d", len(args)), args...)
	if err != nil {
		return nil, "", err
	}
	defer rows.Close()
	result := []json.RawMessage{}
	next := ""
	last := ""
	total := 0
	for rows.Next() {
		var id string
		var data []byte
		if err := rows.Scan(&id, &data); err != nil {
			return nil, "", err
		}
		if len(result) == limit || total+len(data) > 8<<20 {
			next = last
			break
		}
		result = append(result, json.RawMessage(data))
		last = id
		total += len(data)
	}
	return result, next, rows.Err()
}

// beginWrite locks the singleton change clock before any document is read or
// modified. This gives commits their cursor in commit order, including across
// server replicas. A bare Postgres sequence could skip a late-committing event.
func (s *SQLStore) beginWrite(ctx context.Context) (*sql.Tx, int64, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, 0, err
	}
	var cursor int64
	err = tx.QueryRowContext(ctx, s.query("UPDATE annotation_clock SET cursor=cursor+1 WHERE id=1 RETURNING cursor")).Scan(&cursor)
	if err != nil {
		tx.Rollback()
		return nil, 0, err
	}
	return tx, cursor, nil
}

func (s *SQLStore) Save(ctx context.Context, kind, id string, expected int64, actor string, input json.RawMessage) (json.RawMessage, error) {
	if err := checkID(id); err != nil {
		return nil, err
	}
	if expected < 0 || expected >= MaxSafeInteger {
		return nil, invalid("expectedRevision is outside the supported range")
	}
	if len(input) > MaxDocumentBytes {
		return nil, invalid("document exceeds 2 MiB")
	}
	tx, cursor, err := s.beginWrite(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	previous, err := s.getDocument(ctx, tx, kind, id)
	var base Base
	if errors.Is(err, ErrNotFound) {
		if expected != 0 {
			return nil, ErrConflict
		}
		var deletedRevision int64
		err = tx.QueryRowContext(ctx, s.query("SELECT revision FROM annotation_tombstones WHERE kind=$1 AND id=$2"), kind, id).Scan(&deletedRevision)
		if err == nil {
			return nil, ErrConflict
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
	} else if err != nil {
		return nil, err
	} else {
		if err = json.Unmarshal(previous, &base); err != nil {
			return nil, err
		}
		if base.Revision != expected {
			return nil, ErrConflict
		}
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	base.ID = id
	base.Revision = expected + 1
	base.UpdatedAt = now
	if base.CreatedAt == "" {
		base.CreatedAt = now
	}
	data, project, err := s.validateDocument(ctx, tx, kind, base, input, previous)
	if err != nil {
		return nil, err
	}
	_, err = tx.ExecContext(ctx, s.query("INSERT INTO annotation_documents (kind,id,project_id,revision,body) VALUES ($1,$2,$3,$4,$5) ON CONFLICT (kind,id) DO UPDATE SET revision=excluded.revision,body=excluded.body"), kind, id, project, base.Revision, string(data))
	if err != nil {
		return nil, err
	}
	op := "update"
	if expected == 0 {
		op = "create"
	}
	if err = s.appendChange(ctx, tx, cursor, Change{ProjectID: project, Kind: kind, ID: id, Operation: op, Revision: base.Revision, Actor: actor, Timestamp: now, Document: data}); err != nil {
		return nil, err
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return data, nil
}

func (s *SQLStore) validateDocument(ctx context.Context, tx *sql.Tx, kind string, base Base, input, previous json.RawMessage) (json.RawMessage, string, error) {
	var doc any
	project := ""
	switch kind {
	case "project":
		var p Project
		if json.Unmarshal(input, &p) != nil {
			return nil, "", invalid("invalid project")
		}
		p.Base = base
		if err := checkText("name", p.Name, 1, 200); err != nil {
			return nil, "", err
		}
		if len(p.Description) > 10000 {
			return nil, "", invalid("description too long")
		}
		doc = p
		project = base.ID
	case "label":
		var l Label
		if json.Unmarshal(input, &l) != nil {
			return nil, "", invalid("invalid label")
		}
		l.Base = base
		project = l.ProjectID
		if err := checkText("label name", l.Name, 1, 100); err != nil {
			return nil, "", err
		}
		if !validColor.MatchString(l.Color) {
			return nil, "", invalid("label color must be a six-digit hex color")
		}
		doc = l
	case "item":
		var item Item
		if json.Unmarshal(input, &item) != nil {
			return nil, "", invalid("invalid item")
		}
		item.Base = base
		normalizeItem(&item)
		var prior Item
		if len(previous) > 0 {
			if err := json.Unmarshal(previous, &prior); err != nil {
				return nil, "", err
			}
		}
		if err := preserveAgentReviews(ctx, &prior, &item); err != nil {
			return nil, "", err
		}
		project = item.ProjectID
		if err := validateItem(&item, s.adapters); err != nil {
			return nil, "", err
		}
		if len(previous) > 0 {
			var old Item
			_ = json.Unmarshal(previous, &old)
			if old.Adapter != item.Adapter {
				return nil, "", invalid("an existing item's adapter cannot change")
			}
			if (item.Adapter == "addr-scene" || item.Adapter == "addr-report") && !sameJSON(old.Source, item.Source) {
				return nil, "", invalid("imported source references are immutable; use a new item for a different source snapshot")
			}
			if item.Adapter == "addr-scene" {
				if err := validateAddrSceneTransition(&old, &item); err != nil {
					return nil, "", err
				}
			}
			if item.Adapter == "image" {
				for _, key := range []string{"url", "width", "height"} {
					if fmt.Sprint(old.Source[key]) != fmt.Sprint(item.Source[key]) {
						return nil, "", invalid("image source.%s is immutable; create a new item for a different image", key)
					}
				}
			}
			oldContent, _ := json.Marshal(struct {
				Annotations []Annotation
				Crop        *Crop
			}{old.Annotations, old.Crop})
			newContent, _ := json.Marshal(struct {
				Annotations []Annotation
				Crop        *Crop
			}{item.Annotations, item.Crop})
			if old.Status == "approved" && item.Status == "approved" && string(oldContent) != string(newContent) {
				item.Status = "in_review"
			}
		}
		checkedLabels := make(map[string]struct{})
		for _, a := range item.Annotations {
			if a.LabelID == "" {
				continue
			}
			if _, checked := checkedLabels[a.LabelID]; checked {
				continue
			}
			label, err := s.getDocument(ctx, tx, "label", a.LabelID)
			if errors.Is(err, ErrNotFound) {
				return nil, "", invalid("annotation label %q does not exist", a.LabelID)
			}
			if err != nil {
				return nil, "", err
			}
			var l Label
			_ = json.Unmarshal(label, &l)
			if l.ProjectID != project {
				return nil, "", invalid("annotation label belongs to another project")
			}
			checkedLabels[a.LabelID] = struct{}{}
		}
		doc = item
	default:
		return nil, "", invalid("unknown document kind")
	}
	if kind != "project" {
		if err := checkID(project); err != nil {
			return nil, "", err
		}
		if _, err := s.getDocument(ctx, tx, "project", project); errors.Is(err, ErrNotFound) {
			return nil, "", invalid("project does not exist")
		} else if err != nil {
			return nil, "", err
		}
		if len(previous) > 0 {
			var old struct {
				ProjectID string `json:"projectId"`
			}
			_ = json.Unmarshal(previous, &old)
			if old.ProjectID != project {
				return nil, "", invalid("document cannot be moved between projects")
			}
		}
	}
	data, err := json.Marshal(doc)
	if len(data) > MaxDocumentBytes {
		return nil, "", invalid("document exceeds 2 MiB")
	}
	return data, project, err
}

func (s *SQLStore) appendChange(ctx context.Context, tx *sql.Tx, cursor int64, change Change) error {
	change.Cursor = strconv.FormatInt(cursor, 10)
	data, err := json.Marshal(change)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, s.query("INSERT INTO annotation_changes (cursor,project_id,body) VALUES ($1,$2,$3)"), cursor, change.ProjectID, string(data))
	return err
}

func (s *SQLStore) Delete(ctx context.Context, kind, id string, expected int64, actor string) error {
	if kind != "label" && kind != "item" {
		return invalid("this document kind cannot be deleted")
	}
	if expected <= 0 || expected >= MaxSafeInteger {
		return invalid("deletion requires a positive expectedRevision")
	}
	tx, cursor, err := s.beginWrite(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	data, err := s.getDocument(ctx, tx, kind, id)
	if err != nil {
		return err
	}
	var doc struct {
		Base
		ProjectID string `json:"projectId"`
	}
	if err = json.Unmarshal(data, &doc); err != nil {
		return err
	}
	if doc.Revision != expected {
		return ErrConflict
	}
	if kind == "label" {
		rows, err := tx.QueryContext(ctx, s.query("SELECT body FROM annotation_documents WHERE kind='item' AND project_id=$1"), doc.ProjectID)
		if err != nil {
			return err
		}
		for rows.Next() {
			var raw []byte
			if err = rows.Scan(&raw); err != nil {
				rows.Close()
				return err
			}
			var item Item
			if err = json.Unmarshal(raw, &item); err != nil {
				rows.Close()
				return err
			}
			for _, a := range item.Annotations {
				if a.LabelID == id {
					rows.Close()
					return invalid("label is in use; relabel or remove its annotations first")
				}
			}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
	}
	if _, err = tx.ExecContext(ctx, s.query("DELETE FROM annotation_documents WHERE kind=$1 AND id=$2"), kind, id); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, s.query("INSERT INTO annotation_tombstones (kind,id,revision) VALUES ($1,$2,$3)"), kind, id, expected+1); err != nil {
		return err
	}
	if err = s.appendChange(ctx, tx, cursor, Change{ProjectID: doc.ProjectID, Kind: kind, ID: id, Operation: "delete", Revision: expected + 1, Actor: actor, Timestamp: time.Now().UTC().Format(time.RFC3339Nano), Document: data}); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *SQLStore) Changes(ctx context.Context, project, cursor string, limit int) (ChangePage, error) {
	if cursor == "" {
		cursor = "0"
	}
	n, err := strconv.ParseInt(cursor, 10, 64)
	if err != nil || n < 0 {
		return ChangePage{}, invalid("cursor must be a nonnegative decimal string")
	}
	limit = boundedLimit(limit)
	page := ChangePage{Changes: []Change{}, NextCursor: cursor}
	total := 0
	args := []any{n}
	where := "cursor>$1"
	if project != "" {
		args = append(args, project)
		where += " AND project_id=$2"
	}
	args = append(args, limit+1)
	rows, err := s.db.QueryContext(ctx, s.query("SELECT body FROM annotation_changes WHERE ")+where+fmt.Sprintf(" ORDER BY cursor LIMIT $%d", len(args)), args...)
	if err != nil {
		return page, err
	}
	defer rows.Close()
	for rows.Next() {
		var data []byte
		if err = rows.Scan(&data); err != nil {
			return page, err
		}
		if len(page.Changes) == limit || total+len(data) > 8<<20 {
			page.HasMore = true
			break
		}
		var change Change
		if err = json.Unmarshal(data, &change); err != nil {
			return page, err
		}
		page.Changes = append(page.Changes, change)
		page.NextCursor = change.Cursor
		total += len(data)
	}
	return page, rows.Err()
}

// Export reads the cursor and documents from one database snapshot. A consumer
// can resume ListChanges after this cursor without losing concurrent edits.
func (s *SQLStore) Export(ctx context.Context, project string) (Export, error) {
	options := &sql.TxOptions{ReadOnly: true}
	if s.driver == "postgres" {
		options.Isolation = sql.LevelRepeatableRead
	}
	tx, err := s.db.BeginTx(ctx, options)
	if err != nil {
		return Export{}, err
	}
	defer tx.Rollback()
	result := Export{SchemaVersion: 1, Items: []Item{}, Labels: []Label{}}
	var cursor int64
	if err = tx.QueryRowContext(ctx, s.query("SELECT cursor FROM annotation_clock WHERE id=1")).Scan(&cursor); err != nil {
		return result, err
	}
	result.Cursor = strconv.FormatInt(cursor, 10)
	data, err := s.getDocument(ctx, tx, "project", project)
	if err != nil {
		return result, err
	}
	if err = json.Unmarshal(data, &result.Project); err != nil {
		return result, err
	}
	rows, err := tx.QueryContext(ctx, s.query("SELECT kind,body FROM annotation_documents WHERE project_id=$1 AND kind IN ('item','label') ORDER BY kind,id"), project)
	if err != nil {
		return result, err
	}
	total := 0
	for rows.Next() {
		var kind string
		var data []byte
		if err = rows.Scan(&kind, &data); err != nil {
			rows.Close()
			return result, err
		}
		total += len(data)
		if total > 32<<20 {
			rows.Close()
			return result, invalid("export exceeds 32 MiB; use paginated ListChanges from cursor 0 to synchronize this project")
		}
		if kind == "item" {
			var item Item
			err = json.Unmarshal(data, &item)
			result.Items = append(result.Items, item)
		} else {
			var label Label
			err = json.Unmarshal(data, &label)
			result.Labels = append(result.Labels, label)
		}
		if err != nil {
			rows.Close()
			return result, err
		}
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return result, err
	}
	return result, tx.Commit()
}
