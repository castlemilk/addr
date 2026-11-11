// Package crm implements the per-address CRM: metadata, boundary overrides,
// POI records, and the RBAC layer (roles, scopes, API keys) that guards them.
package crm

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

// Store is the read-write CRM store. It shares the SQLite file with the
// read-only geo store; WAL mode allows concurrent readers with one writer.
type Store struct {
	db *sql.DB
}

// ErrNotFound is returned when a record does not exist.
var ErrNotFound = errors.New("crm record not found")

func now() string { return time.Now().UTC().Format(time.RFC3339) }

// NewStore opens (creating if needed) the CRM database and applies migrations.
func NewStore(dbPath string) (*Store, error) {
	dsn := fmt.Sprintf(
		"file:%s?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)&_pragma=synchronous(NORMAL)",
		dbPath,
	)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("opening crm sqlite: %w", err)
	}
	db.SetMaxOpenConns(1)
	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("pinging crm sqlite: %w", err)
	}
	store := &Store{db: db}
	if err := store.migrate(); err != nil {
		_ = db.Close()
		return nil, err
	}
	return store, nil
}

func (s *Store) Close() error { return s.db.Close() }

// DB exposes the underlying handle for migrations and tests.
func (s *Store) DB() *sql.DB { return s.db }

func (s *Store) migrate() error {
	const schema = `
CREATE TABLE IF NOT EXISTS crm_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  roles TEXT NOT NULL DEFAULT '[]',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_api_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES crm_users(id),
  key_hash TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  scopes TEXT,
  created_at TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS crm_address_meta (
  address_id INTEGER PRIMARY KEY,
  status TEXT,
  owner_name TEXT,
  owner_email TEXT,
  owner_phone TEXT,
  notes TEXT,
  tags TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_features (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  address_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  geometry_json TEXT NOT NULL,
  label TEXT,
  properties TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS crm_features_address_idx ON crm_features(address_id);
CREATE TABLE IF NOT EXISTS crm_pois (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  category TEXT,
  address_text TEXT,
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  phone TEXT,
  website TEXT,
  address_id INTEGER,
  notes TEXT,
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS crm_pois_address_idx ON crm_pois(address_id);
`
	_, err := s.db.Exec(schema)
	if err != nil {
		return fmt.Errorf("applying crm schema: %w", err)
	}
	return nil
}

// ---------------------------------------------------------------- users/keys

// User is a CRM principal with role-based scopes.
type User struct {
	ID        int64    `json:"id"`
	Name      string   `json:"name"`
	Roles     []string `json:"roles"`
	Active    bool     `json:"active"`
	CreatedAt string   `json:"created_at"`
	UpdatedAt string   `json:"updated_at"`
}

func (s *Store) CreateUser(ctx context.Context, name string, roles []string) (User, error) {
	if strings.TrimSpace(name) == "" {
		return User{}, errors.New("user name is required")
	}
	for _, role := range roles {
		if _, ok := RoleScopes[role]; !ok {
			return User{}, fmt.Errorf("unknown role %q", role)
		}
	}
	rolesJSON, _ := json.Marshal(roles)
	res, err := s.db.ExecContext(ctx,
		`INSERT INTO crm_users (name, roles, active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)`,
		name, string(rolesJSON), now(), now(),
	)
	if err != nil {
		return User{}, fmt.Errorf("creating user: %w", err)
	}
	id, _ := res.LastInsertId()
	return s.getUser(ctx, id)
}

func (s *Store) getUser(ctx context.Context, id int64) (User, error) {
	var (
		user      User
		rolesJSON string
		active    int
	)
	err := s.db.QueryRowContext(ctx,
		`SELECT id, name, roles, active, created_at, updated_at FROM crm_users WHERE id = ?`, id,
	).Scan(&user.ID, &user.Name, &rolesJSON, &active, &user.CreatedAt, &user.UpdatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return User{}, ErrNotFound
	}
	if err != nil {
		return User{}, err
	}
	_ = json.Unmarshal([]byte(rolesJSON), &user.Roles)
	user.Active = active == 1
	return user, nil
}

// GetUserByName looks up a user by name.
func (s *Store) GetUserByName(ctx context.Context, name string) (User, error) {
	var id int64
	err := s.db.QueryRowContext(ctx, `SELECT id FROM crm_users WHERE name = ?`, name).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return User{}, ErrNotFound
	}
	if err != nil {
		return User{}, err
	}
	return s.getUser(ctx, id)
}

// ListUsers returns all users.
func (s *Store) ListUsers(ctx context.Context) ([]User, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT id, name, roles, active, created_at, updated_at FROM crm_users ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var users []User
	for rows.Next() {
		var (
			user      User
			rolesJSON string
			active    int
		)
		if err := rows.Scan(&user.ID, &user.Name, &rolesJSON, &active, &user.CreatedAt, &user.UpdatedAt); err != nil {
			return nil, err
		}
		_ = json.Unmarshal([]byte(rolesJSON), &user.Roles)
		user.Active = active == 1
		users = append(users, user)
	}
	return users, rows.Err()
}

// APIKey is a display record for a stored key (the secret is never persisted).
type APIKey struct {
	ID         int64    `json:"id"`
	UserID     int64    `json:"user_id"`
	UserName   string   `json:"user_name"`
	Prefix     string   `json:"prefix"`
	Label      string   `json:"label"`
	Scopes     []string `json:"scopes,omitempty"`
	CreatedAt  string   `json:"created_at"`
	LastUsedAt *string  `json:"last_used_at,omitempty"`
	RevokedAt  *string  `json:"revoked_at,omitempty"`
}

// CreateAPIKey mints a new key for a user and returns the plaintext exactly
// once. scopes optionally restricts the key to a subset of the user's scopes.
func (s *Store) CreateAPIKey(ctx context.Context, userName, label string, scopes []string) (string, APIKey, error) {
	user, err := s.GetUserByName(ctx, userName)
	if err != nil {
		return "", APIKey{}, err
	}
	for _, scope := range scopes {
		if !knownScope(scope) {
			return "", APIKey{}, fmt.Errorf("unknown scope %q", scope)
		}
	}
	secret, prefix, hash, err := generateKey()
	if err != nil {
		return "", APIKey{}, err
	}
	var scopesJSON *string
	if len(scopes) > 0 {
		encoded, _ := json.Marshal(scopes)
		value := string(encoded)
		scopesJSON = &value
	}
	res, err := s.db.ExecContext(ctx,
		`INSERT INTO crm_api_keys (user_id, key_hash, prefix, label, scopes, created_at)
		 VALUES (?, ?, ?, ?, ?, ?)`,
		user.ID, hash, prefix, label, scopesJSON, now(),
	)
	if err != nil {
		return "", APIKey{}, fmt.Errorf("creating api key: %w", err)
	}
	id, _ := res.LastInsertId()
	key := APIKey{ID: id, UserID: user.ID, UserName: user.Name, Prefix: prefix, Label: label, Scopes: scopes}
	key.CreatedAt = now()
	return secret, key, nil
}

// ListAPIKeys returns stored key records for a user (or all users when empty).
func (s *Store) ListAPIKeys(ctx context.Context, userName string) ([]APIKey, error) {
	query := `SELECT k.id, k.user_id, u.name, k.prefix, k.label, k.scopes, k.created_at,
	                 k.last_used_at, k.revoked_at
	          FROM crm_api_keys k JOIN crm_users u ON u.id = k.user_id`
	var args []any
	if userName != "" {
		query += ` WHERE u.name = ?`
		args = append(args, userName)
	}
	query += ` ORDER BY k.id`
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var keys []APIKey
	for rows.Next() {
		var (
			key        APIKey
			scopesJSON *string
		)
		if err := rows.Scan(&key.ID, &key.UserID, &key.UserName, &key.Prefix, &key.Label,
			&scopesJSON, &key.CreatedAt, &key.LastUsedAt, &key.RevokedAt); err != nil {
			return nil, err
		}
		if scopesJSON != nil {
			_ = json.Unmarshal([]byte(*scopesJSON), &key.Scopes)
		}
		keys = append(keys, key)
	}
	return keys, rows.Err()
}

// RevokeAPIKey marks a key as revoked.
func (s *Store) RevokeAPIKey(ctx context.Context, id int64) error {
	res, err := s.db.ExecContext(ctx,
		`UPDATE crm_api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL`, now(), id)
	if err != nil {
		return err
	}
	affected, _ := res.RowsAffected()
	if affected == 0 {
		return ErrNotFound
	}
	return nil
}

// ---------------------------------------------------------------- address meta

// AddressMeta is the editable per-address record.
type AddressMeta struct {
	AddressID   int64    `json:"address_id"`
	FullAddress string   `json:"full_address,omitempty"`
	Status      *string  `json:"status,omitempty"`
	OwnerName   *string  `json:"owner_name,omitempty"`
	OwnerEmail  *string  `json:"owner_email,omitempty"`
	OwnerPhone  *string  `json:"owner_phone,omitempty"`
	Notes       *string  `json:"notes,omitempty"`
	Tags        []string `json:"tags"`
	UpdatedAt   *string  `json:"updated_at,omitempty"`
	UpdatedBy   *string  `json:"updated_by,omitempty"`
}

func nullString(value string) *string {
	if value == "" {
		return nil
	}
	return &value
}

// GetAddressMeta returns the stored meta (zero value when none exists yet).
func (s *Store) GetAddressMeta(ctx context.Context, addressID int64) (AddressMeta, error) {
	meta := AddressMeta{AddressID: addressID}
	var tagsJSON string
	err := s.db.QueryRowContext(ctx,
		`SELECT status, owner_name, owner_email, owner_phone, notes, tags, updated_at, updated_by
		 FROM crm_address_meta WHERE address_id = ?`, addressID,
	).Scan(&meta.Status, &meta.OwnerName, &meta.OwnerEmail, &meta.OwnerPhone, &meta.Notes,
		&tagsJSON, &meta.UpdatedAt, &meta.UpdatedBy)
	if errors.Is(err, sql.ErrNoRows) {
		_ = s.db.QueryRowContext(ctx, `SELECT full_address FROM addresses WHERE id = ?`, addressID).
			Scan(&meta.FullAddress)
		return meta, nil
	}
	if err != nil {
		return AddressMeta{}, err
	}
	_ = json.Unmarshal([]byte(tagsJSON), &meta.Tags)
	_ = s.db.QueryRowContext(ctx, `SELECT full_address FROM addresses WHERE id = ?`, addressID).
		Scan(&meta.FullAddress)
	return meta, nil
}

// UpdateAddressMeta upserts editable metadata for an address.
func (s *Store) UpdateAddressMeta(ctx context.Context, meta AddressMeta, actor string) (AddressMeta, error) {
	var exists int
	if err := s.db.QueryRowContext(ctx, `SELECT 1 FROM addresses WHERE id = ?`, meta.AddressID).Scan(&exists); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return AddressMeta{}, ErrNotFound
		}
		return AddressMeta{}, err
	}
	tagsJSON, _ := json.Marshal(meta.Tags)
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO crm_address_meta (address_id, status, owner_name, owner_email, owner_phone, notes, tags, updated_at, updated_by)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
		 ON CONFLICT(address_id) DO UPDATE SET
		   status = excluded.status,
		   owner_name = excluded.owner_name,
		   owner_email = excluded.owner_email,
		   owner_phone = excluded.owner_phone,
		   notes = excluded.notes,
		   tags = excluded.tags,
		   updated_at = excluded.updated_at,
		   updated_by = excluded.updated_by`,
		meta.AddressID, meta.Status, meta.OwnerName, meta.OwnerEmail, meta.OwnerPhone,
		meta.Notes, string(tagsJSON), now(), actor,
	)
	if err != nil {
		return AddressMeta{}, fmt.Errorf("saving address meta: %w", err)
	}
	return s.GetAddressMeta(ctx, meta.AddressID)
}

// SearchAddresses returns address matches for the CRM editor.
func (s *Store) SearchAddresses(ctx context.Context, query string, limit int) ([]AddressMeta, error) {
	if limit <= 0 || limit > 50 {
		limit = 15
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT a.id, a.full_address, m.status, m.tags
		 FROM addresses a LEFT JOIN crm_address_meta m ON m.address_id = a.id
		 WHERE a.full_address LIKE ? ORDER BY a.full_address LIMIT ?`,
		"%"+strings.TrimSpace(query)+"%", limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var metas []AddressMeta
	for rows.Next() {
		var (
			meta     AddressMeta
			tagsJSON *string
		)
		if err := rows.Scan(&meta.AddressID, &meta.FullAddress, &meta.Status, &tagsJSON); err != nil {
			return nil, err
		}
		if tagsJSON != nil {
			_ = json.Unmarshal([]byte(*tagsJSON), &meta.Tags)
		}
		metas = append(metas, meta)
	}
	return metas, rows.Err()
}

// ---------------------------------------------------------------- features

// SpatialFeature is a boundary override (roof, tree, or other object).
type SpatialFeature struct {
	ID           int64             `json:"id"`
	AddressID    int64             `json:"address_id"`
	Kind         string            `json:"kind"` // roof | tree | object
	GeometryJSON string            `json:"geometry_json"`
	Label        *string           `json:"label,omitempty"`
	Properties   map[string]string `json:"properties"`
	UpdatedAt    string            `json:"updated_at"`
	UpdatedBy    string            `json:"updated_by"`
}

// ValidFeatureKind reports whether kind is editable through the CRM.
func ValidFeatureKind(kind string) bool {
	return kind == "roof" || kind == "tree" || kind == "object"
}

// UpsertFeature inserts (id == 0) or replaces a boundary override.
func (s *Store) UpsertFeature(ctx context.Context, feature SpatialFeature, actor string) (SpatialFeature, error) {
	if !ValidFeatureKind(feature.Kind) {
		return SpatialFeature{}, fmt.Errorf("invalid feature kind %q", feature.Kind)
	}
	if strings.TrimSpace(feature.GeometryJSON) == "" {
		return SpatialFeature{}, errors.New("geometry_json is required")
	}
	if feature.Properties == nil {
		feature.Properties = map[string]string{}
	}
	propsJSON, _ := json.Marshal(feature.Properties)
	if feature.ID > 0 {
		res, err := s.db.ExecContext(ctx,
			`UPDATE crm_features SET address_id = ?, kind = ?, geometry_json = ?, label = ?,
			 properties = ?, updated_at = ?, updated_by = ? WHERE id = ?`,
			feature.AddressID, feature.Kind, feature.GeometryJSON, feature.Label,
			string(propsJSON), now(), actor, feature.ID,
		)
		if err != nil {
			return SpatialFeature{}, fmt.Errorf("updating feature: %w", err)
		}
		if affected, _ := res.RowsAffected(); affected == 0 {
			return SpatialFeature{}, ErrNotFound
		}
		return s.GetFeature(ctx, feature.ID)
	}
	res, err := s.db.ExecContext(ctx,
		`INSERT INTO crm_features (address_id, kind, geometry_json, label, properties, updated_at, updated_by)
		 VALUES (?, ?, ?, ?, ?, ?, ?)`,
		feature.AddressID, feature.Kind, feature.GeometryJSON, feature.Label,
		string(propsJSON), now(), actor,
	)
	if err != nil {
		return SpatialFeature{}, fmt.Errorf("inserting feature: %w", err)
	}
	id, _ := res.LastInsertId()
	return s.GetFeature(ctx, id)
}

// GetFeature returns one feature by id.
func (s *Store) GetFeature(ctx context.Context, id int64) (SpatialFeature, error) {
	var (
		feature  SpatialFeature
		propsRaw string
	)
	err := s.db.QueryRowContext(ctx,
		`SELECT id, address_id, kind, geometry_json, label, properties, updated_at, updated_by
		 FROM crm_features WHERE id = ?`, id,
	).Scan(&feature.ID, &feature.AddressID, &feature.Kind, &feature.GeometryJSON,
		&feature.Label, &propsRaw, &feature.UpdatedAt, &feature.UpdatedBy)
	if errors.Is(err, sql.ErrNoRows) {
		return SpatialFeature{}, ErrNotFound
	}
	if err != nil {
		return SpatialFeature{}, err
	}
	_ = json.Unmarshal([]byte(propsRaw), &feature.Properties)
	return feature, nil
}

// ListFeatures returns boundary overrides for an address.
func (s *Store) ListFeatures(ctx context.Context, addressID int64) ([]SpatialFeature, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT id, address_id, kind, geometry_json, label, properties, updated_at, updated_by
		 FROM crm_features WHERE address_id = ? ORDER BY id`, addressID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var features []SpatialFeature
	for rows.Next() {
		var (
			feature  SpatialFeature
			propsRaw string
		)
		if err := rows.Scan(&feature.ID, &feature.AddressID, &feature.Kind, &feature.GeometryJSON,
			&feature.Label, &propsRaw, &feature.UpdatedAt, &feature.UpdatedBy); err != nil {
			return nil, err
		}
		_ = json.Unmarshal([]byte(propsRaw), &feature.Properties)
		features = append(features, feature)
	}
	return features, rows.Err()
}

// DeleteFeature removes a boundary override.
func (s *Store) DeleteFeature(ctx context.Context, id int64) error {
	res, err := s.db.ExecContext(ctx, `DELETE FROM crm_features WHERE id = ?`, id)
	if err != nil {
		return err
	}
	if affected, _ := res.RowsAffected(); affected == 0 {
		return ErrNotFound
	}
	return nil
}

// ---------------------------------------------------------------- pois

// Poi is an editable point of interest.
type Poi struct {
	ID          int64    `json:"id"`
	Name        string   `json:"name"`
	Category    *string  `json:"category,omitempty"`
	AddressText *string  `json:"address_text,omitempty"`
	Latitude    float64  `json:"latitude"`
	Longitude   float64  `json:"longitude"`
	Phone       *string  `json:"phone,omitempty"`
	Website     *string  `json:"website,omitempty"`
	AddressID   *int64   `json:"address_id,omitempty"`
	Notes       *string  `json:"notes,omitempty"`
	UpdatedAt   string   `json:"updated_at"`
	UpdatedBy   string   `json:"updated_by"`
	DistanceM   *float64 `json:"distance_meters,omitempty"`
}

func (s *Store) scanPois(rows *sql.Rows) ([]Poi, error) {
	defer rows.Close()
	var pois []Poi
	for rows.Next() {
		var poi Poi
		if err := rows.Scan(&poi.ID, &poi.Name, &poi.Category, &poi.AddressText,
			&poi.Latitude, &poi.Longitude, &poi.Phone, &poi.Website,
			&poi.AddressID, &poi.Notes, &poi.UpdatedAt, &poi.UpdatedBy); err != nil {
			return nil, err
		}
		pois = append(pois, poi)
	}
	return pois, rows.Err()
}

const poiColumns = `id, name, category, address_text, lat, lon, phone, website,
	address_id, notes, updated_at, updated_by`

// CreatePoi inserts a POI.
func (s *Store) CreatePoi(ctx context.Context, poi Poi, actor string) (Poi, error) {
	if strings.TrimSpace(poi.Name) == "" {
		return Poi{}, errors.New("poi name is required")
	}
	res, err := s.db.ExecContext(ctx,
		`INSERT INTO crm_pois (name, category, address_text, lat, lon, phone, website, address_id, notes, updated_at, updated_by)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		poi.Name, poi.Category, poi.AddressText, poi.Latitude, poi.Longitude,
		poi.Phone, poi.Website, poi.AddressID, poi.Notes, now(), actor,
	)
	if err != nil {
		return Poi{}, fmt.Errorf("creating poi: %w", err)
	}
	id, _ := res.LastInsertId()
	return s.GetPoi(ctx, id)
}

// GetPoi returns one POI by id.
func (s *Store) GetPoi(ctx context.Context, id int64) (Poi, error) {
	var poi Poi
	err := s.db.QueryRowContext(ctx,
		`SELECT `+poiColumns+` FROM crm_pois WHERE id = ?`, id,
	).Scan(&poi.ID, &poi.Name, &poi.Category, &poi.AddressText, &poi.Latitude, &poi.Longitude,
		&poi.Phone, &poi.Website, &poi.AddressID, &poi.Notes, &poi.UpdatedAt, &poi.UpdatedBy)
	if errors.Is(err, sql.ErrNoRows) {
		return Poi{}, ErrNotFound
	}
	if err != nil {
		return Poi{}, err
	}
	return poi, nil
}

// UpdatePoi edits an existing POI (nil fields are left unchanged).
func (s *Store) UpdatePoi(ctx context.Context, poi Poi, actor string) (Poi, error) {
	current, err := s.GetPoi(ctx, poi.ID)
	if err != nil {
		return Poi{}, err
	}
	if poi.Name == "" {
		poi.Name = current.Name
	}
	if poi.Category == nil {
		poi.Category = current.Category
	}
	if poi.AddressText == nil {
		poi.AddressText = current.AddressText
	}
	if poi.Latitude == 0 {
		poi.Latitude = current.Latitude
	}
	if poi.Longitude == 0 {
		poi.Longitude = current.Longitude
	}
	if poi.Phone == nil {
		poi.Phone = current.Phone
	}
	if poi.Website == nil {
		poi.Website = current.Website
	}
	if poi.AddressID == nil {
		poi.AddressID = current.AddressID
	}
	if poi.Notes == nil {
		poi.Notes = current.Notes
	}
	_, err = s.db.ExecContext(ctx,
		`UPDATE crm_pois SET name = ?, category = ?, address_text = ?, lat = ?, lon = ?,
		 phone = ?, website = ?, address_id = ?, notes = ?, updated_at = ?, updated_by = ?
		 WHERE id = ?`,
		poi.Name, poi.Category, poi.AddressText, poi.Latitude, poi.Longitude,
		poi.Phone, poi.Website, poi.AddressID, poi.Notes, now(), actor, poi.ID,
	)
	if err != nil {
		return Poi{}, fmt.Errorf("updating poi: %w", err)
	}
	return s.GetPoi(ctx, poi.ID)
}

// DeletePoi removes a POI.
func (s *Store) DeletePoi(ctx context.Context, id int64) error {
	res, err := s.db.ExecContext(ctx, `DELETE FROM crm_pois WHERE id = ?`, id)
	if err != nil {
		return err
	}
	if affected, _ := res.RowsAffected(); affected == 0 {
		return ErrNotFound
	}
	return nil
}

// PoiQuery filters POI listings.
type PoiQuery struct {
	AddressID *int64
	MinLat    *float64
	MinLon    *float64
	MaxLat    *float64
	MaxLon    *float64
	NearLat   *float64
	NearLon   *float64
	RadiusM   *float64
	Limit     int
}

// ListPois returns POIs matching the filter.
func (s *Store) ListPois(ctx context.Context, query PoiQuery) ([]Poi, error) {
	limit := query.Limit
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	sqlQuery := `SELECT ` + poiColumns + ` FROM crm_pois WHERE 1=1`
	var args []any
	if query.AddressID != nil {
		sqlQuery += ` AND address_id = ?`
		args = append(args, *query.AddressID)
	}
	if query.MinLat != nil && query.MaxLat != nil {
		sqlQuery += ` AND lat BETWEEN ? AND ?`
		args = append(args, *query.MinLat, *query.MaxLat)
	}
	if query.MinLon != nil && query.MaxLon != nil {
		sqlQuery += ` AND lon BETWEEN ? AND ?`
		args = append(args, *query.MinLon, *query.MaxLon)
	}
	sqlQuery += ` ORDER BY id LIMIT ?`
	args = append(args, limit)

	rows, err := s.db.QueryContext(ctx, sqlQuery, args...)
	if err != nil {
		return nil, err
	}
	pois, err := s.scanPois(rows)
	if err != nil {
		return nil, err
	}
	if query.NearLat != nil && query.NearLon != nil {
		radius := 500.0
		if query.RadiusM != nil && *query.RadiusM > 0 {
			radius = *query.RadiusM
		}
		filtered := pois[:0]
		for _, poi := range pois {
			distance := haversineM(*query.NearLat, *query.NearLon, poi.Latitude, poi.Longitude)
			if distance <= radius {
				value := distance
				poi.DistanceM = &value
				filtered = append(filtered, poi)
			}
		}
		pois = filtered
	}
	return pois, nil
}

func haversineM(lat1, lon1, lat2, lon2 float64) float64 {
	const earthRadius = 6371000.0
	toRad := func(deg float64) float64 { return deg * math.Pi / 180 }
	dLat := toRad(lat2 - lat1)
	dLon := toRad(lon2 - lon1)
	a := math.Sin(dLat/2)*math.Sin(dLat/2) +
		math.Cos(toRad(lat1))*math.Cos(toRad(lat2))*math.Sin(dLon/2)*math.Sin(dLon/2)
	return 2 * earthRadius * math.Asin(math.Sqrt(a))
}
