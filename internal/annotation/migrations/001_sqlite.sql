CREATE TABLE IF NOT EXISTS annotation_migrations (version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS annotation_documents (
  kind TEXT NOT NULL CHECK (kind IN ('project','item','label')),
  id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK (revision > 0),
  body TEXT NOT NULL,
  PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS annotation_documents_project ON annotation_documents (kind, project_id, id);
CREATE TABLE IF NOT EXISTS annotation_tombstones (kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY (kind, id));
CREATE TABLE IF NOT EXISTS annotation_clock (id INTEGER PRIMARY KEY CHECK (id = 1), cursor INTEGER NOT NULL);
INSERT INTO annotation_clock (id, cursor) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;
CREATE TABLE IF NOT EXISTS annotation_changes (cursor INTEGER PRIMARY KEY, project_id TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS annotation_changes_project ON annotation_changes (project_id, cursor);
INSERT INTO annotation_migrations (version) VALUES (1) ON CONFLICT (version) DO NOTHING;
