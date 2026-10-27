package annotation

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

// Run with ANNOTATION_TEST_DATABASE_URL pointing to a direct, disposable/test
// database. Each run uses and removes its own randomly named Postgres schema;
// it never deletes or modifies records in the application's public schema.
func TestPostgresIntegration(t *testing.T) {
	dsn := os.Getenv("ANNOTATION_TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("set ANNOTATION_TEST_DATABASE_URL to test live Postgres")
	}
	u, err := url.Parse(dsn)
	if err != nil {
		t.Fatal("invalid test database URL")
	}
	if strings.Contains(u.Hostname(), "-pooler") {
		t.Fatal("integration tests require a direct database URL")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	admin, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal("cannot configure test database")
	}
	defer admin.Close()
	schema := "annotation_test_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	if _, err = admin.ExecContext(ctx, "CREATE SCHEMA "+schema); err != nil {
		t.Fatal("cannot create isolated annotation test schema")
	}
	defer func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		if _, err := admin.ExecContext(cleanup, "DROP SCHEMA "+schema+" CASCADE"); err != nil {
			t.Error("could not remove isolated test schema")
		}
	}()
	query := u.Query()
	query.Del("search_path")
	query.Set("options", strings.TrimSpace(query.Get("options")+" -csearch_path="+schema))
	u.RawQuery = query.Encode()
	// Confirm startup options select the isolated schema before migrations
	// can execute any unqualified DDL against this connection configuration.
	isolation, err := sql.Open("postgres", u.String())
	if err != nil {
		t.Fatal("cannot configure isolated test database")
	}
	var activeSchema string
	err = isolation.QueryRowContext(ctx, "SELECT current_schema()").Scan(&activeSchema)
	_ = isolation.Close()
	if err != nil || activeSchema != schema {
		t.Fatal("test database did not select its isolated schema before migration")
	}
	s, err := OpenStore(ctx, "postgres", u.String(), true)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if err = s.db.QueryRowContext(ctx, "SELECT current_schema()").Scan(&activeSchema); err != nil || activeSchema != schema {
		t.Fatal("test database did not select its isolated schema")
	}
	var documentCount int
	if err = s.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM annotation_documents").Scan(&documentCount); err != nil || documentCount != 0 {
		t.Fatal("isolated test schema is not empty")
	}
	createProject(t, s, "project")
	item := imageItem("project")
	item.Title = "100% verified"
	saveTest(t, s, "item", "image", 0, item)
	var wg sync.WaitGroup
	results := make(chan error, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := s.Save(ctx, "item", "image", 1, "verification", raw(t, item))
			results <- err
		}()
	}
	wg.Wait()
	close(results)
	success, conflict := 0, 0
	for err := range results {
		if err == nil {
			success++
		} else if errors.Is(err, ErrConflict) {
			conflict++
		} else {
			t.Fatal(err)
		}
	}
	if success != 1 || conflict != 1 {
		t.Fatalf("Postgres compare-and-swap: success=%d conflict=%d", success, conflict)
	}
	list, _, err := s.List(ctx, "item", ListOptions{ProjectID: "project", Query: "%", Limit: 1})
	if err != nil || len(list) != 1 {
		t.Fatalf("Postgres literal query: %v (%d)", err, len(list))
	}
	if tagged, _, err := s.List(ctx, "item", ListOptions{ProjectID: "project", Tag: "EXAMPLE"}); err != nil || len(tagged) != 1 {
		t.Fatalf("Postgres tag filter: %v (%d)", err, len(tagged))
	}
	if none, _, err := s.List(ctx, "item", ListOptions{ProjectID: "project", Tag: "absent"}); err != nil || len(none) != 0 {
		t.Fatalf("Postgres absent tag: %v (%d)", err, len(none))
	}
	export, err := s.Export(ctx, "project")
	if err != nil {
		t.Fatal(err)
	}
	if export.Cursor != "3" || len(export.Items) != 1 || export.Items[0].Revision != 2 {
		t.Fatal("inconsistent Postgres snapshot")
	}
	// Holding the clock transaction must prevent another mutation from committing
	// a later cursor. Rolling it back must leave no event or cursor gap.
	tx, _, err := s.beginWrite(ctx)
	if err != nil {
		t.Fatal(err)
	}
	started := make(chan struct{})
	finished := make(chan error, 1)
	go func() {
		close(started)
		_, err := s.Save(ctx, "project", "project2", 0, "verification", raw(t, Project{Name: "Example verification"}))
		finished <- err
	}()
	<-started
	select {
	case err := <-finished:
		tx.Rollback()
		t.Fatalf("write bypassed clock lock: %v", err)
	case <-time.After(100 * time.Millisecond):
	}
	if err = tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if err = <-finished; err != nil {
		t.Fatal(err)
	}
	page, err := s.Changes(ctx, "", export.Cursor, 1)
	if err != nil || len(page.Changes) != 1 || page.Changes[0].Cursor != "4" {
		t.Fatalf("Postgres cursor lock: %+v %v", page, err)
	}
	if err = s.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = OpenStore(ctx, "postgres", u.String(), false)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	data, err := s.Get(ctx, "item", "image")
	if err != nil {
		t.Fatal(err)
	}
	var persisted Item
	_ = json.Unmarshal(data, &persisted)
	if persisted.Revision != 2 {
		t.Fatal("Postgres reopen lost document")
	}
	if err = s.Delete(ctx, "item", "image", 2, "verification"); err != nil {
		t.Fatal(err)
	}
	if _, err = s.Save(ctx, "item", "image", 0, "verification", raw(t, item)); !errors.Is(err, ErrConflict) {
		t.Fatalf("Postgres allowed tombstone ID reuse: %v", err)
	}
	t.Run("shared workspace HTTP isolation", func(t *testing.T) {
		t.Cleanup(func() {
			rows, err := s.db.QueryContext(context.Background(), "SELECT namespace FROM fieldwork_workspaces WHERE namespace<>''")
			if err != nil {
				t.Error(err)
				return
			}
			var names []string
			for rows.Next() {
				var name string
				if err = rows.Scan(&name); err != nil {
					t.Error(err)
					break
				}
				names = append(names, name)
			}
			rows.Close()
			for _, name := range names {
				if !namespacePattern.MatchString(name) {
					t.Fatal("invalid test schema")
				}
				if _, err = s.db.ExecContext(context.Background(), `DROP SCHEMA "`+name+`" CASCADE`); err != nil {
					t.Error(err)
				}
			}
		})
		testSharedWorkspaceHTTPIsolation(t, s)
	})

}
