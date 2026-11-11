package crm

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"connectrpc.com/connect"
)

// Scopes gate CRM operations. Roles bundle scopes; API keys may further
// restrict a principal to a subset of its role scopes.
const (
	ScopeRead         = "crm.read"
	ScopeAddressWrite = "crm.address.write"
	ScopeRoofWrite    = "crm.roof.write"
	ScopeTreeWrite    = "crm.tree.write"
	ScopeObjectWrite  = "crm.object.write"
	ScopePoiWrite     = "crm.poi.write"
	ScopeUsersRead    = "crm.users.read"
	ScopeAdmin        = "crm.admin"
)

// RoleScopes maps built-in roles to their scopes.
var RoleScopes = map[string][]string{
	"viewer": {ScopeRead},
	"editor": {
		ScopeRead,
		ScopeAddressWrite,
		ScopeRoofWrite,
		ScopeTreeWrite,
		ScopeObjectWrite,
		ScopePoiWrite,
	},
	"admin": {
		ScopeRead,
		ScopeAddressWrite,
		ScopeRoofWrite,
		ScopeTreeWrite,
		ScopeObjectWrite,
		ScopePoiWrite,
		ScopeUsersRead,
		ScopeAdmin,
	},
}

func knownScope(scope string) bool {
	for _, scopes := range RoleScopes {
		for _, candidate := range scopes {
			if candidate == scope {
				return true
			}
		}
	}
	return false
}

// ResolveScopes unions the scopes of the given roles and, when keyScopes is
// non-nil, intersects with the key restriction.
func ResolveScopes(roles, keyScopes []string) []string {
	union := map[string]bool{}
	for _, role := range roles {
		for _, scope := range RoleScopes[role] {
			union[scope] = true
		}
	}
	if keyScopes != nil {
		allowed := map[string]bool{}
		for _, scope := range keyScopes {
			allowed[scope] = true
		}
		for scope := range union {
			if !allowed[scope] {
				delete(union, scope)
			}
		}
	}
	scopes := make([]string, 0, len(union))
	for scope := range union {
		scopes = append(scopes, scope)
	}
	sortStrings(scopes)
	return scopes
}

func sortStrings(values []string) {
	for i := 1; i < len(values); i++ {
		for j := i; j > 0 && values[j] < values[j-1]; j-- {
			values[j], values[j-1] = values[j-1], values[j]
		}
	}
}

// Principal is an authenticated CRM caller.
type Principal struct {
	UserID   int64    `json:"user_id"`
	Name     string   `json:"name"`
	Roles    []string `json:"roles"`
	Scopes   []string `json:"scopes"`
	KeyLabel string   `json:"key_label,omitempty"`
}

// HasScope reports whether the principal holds the required scope.
func (p Principal) HasScope(scope string) bool {
	for _, candidate := range p.Scopes {
		if candidate == scope {
			return true
		}
	}
	return false
}

type principalKey struct{}

// PrincipalFrom returns the authenticated principal from the context.
func PrincipalFrom(ctx context.Context) (Principal, bool) {
	principal, ok := ctx.Value(principalKey{}).(Principal)
	return principal, ok
}

// WithPrincipal attaches a principal to a context (used by tests).
func WithPrincipal(ctx context.Context, principal Principal) context.Context {
	return context.WithValue(ctx, principalKey{}, principal)
}

// Authenticate resolves a bearer token to a principal.
func (s *Store) Authenticate(ctx context.Context, token string) (Principal, error) {
	token = strings.TrimSpace(token)
	if token == "" {
		return Principal{}, errors.New("missing bearer token")
	}
	hash := hashToken(token)
	var (
		principal  Principal
		rolesJSON  string
		scopesJSON *string
		keyLabel   string
		active     int
	)
	err := s.db.QueryRowContext(ctx,
		`SELECT u.id, u.name, u.roles, u.active, k.label, k.scopes
		 FROM crm_api_keys k JOIN crm_users u ON u.id = k.user_id
		 WHERE k.key_hash = ? AND k.revoked_at IS NULL`, hash,
	).Scan(&principal.UserID, &principal.Name, &rolesJSON, &active, &keyLabel, &scopesJSON)
	if err != nil {
		return Principal{}, errors.New("invalid or revoked API key")
	}
	if active != 1 {
		return Principal{}, errors.New("user is disabled")
	}
	_ = json.Unmarshal([]byte(rolesJSON), &principal.Roles)
	var keyScopes []string
	if scopesJSON != nil {
		keyScopes = []string{}
		_ = json.Unmarshal([]byte(*scopesJSON), &keyScopes)
	}
	principal.Scopes = ResolveScopes(principal.Roles, keyScopes)
	principal.KeyLabel = keyLabel
	_, _ = s.db.ExecContext(ctx, `UPDATE crm_api_keys SET last_used_at = ? WHERE key_hash = ?`, now(), hash)
	return principal, nil
}

// requiredScope maps a Connect procedure to the scope it requires. Anything
// not listed is rejected, so new RPCs must opt in explicitly.
var requiredScope = map[string]string{
	"/crm.v1.CrmService/WhoAmI":            ScopeRead,
	"/crm.v1.CrmService/GetAddressCrm":     ScopeRead,
	"/crm.v1.CrmService/SearchAddresses":   ScopeRead,
	"/crm.v1.CrmService/ListPois":          ScopeRead,
	"/crm.v1.CrmService/UpdateAddressMeta": ScopeAddressWrite,
	"/crm.v1.CrmService/CreatePoi":         ScopePoiWrite,
	"/crm.v1.CrmService/UpdatePoi":         ScopePoiWrite,
	"/crm.v1.CrmService/DeletePoi":         ScopePoiWrite,
	// UpsertFeature/DeleteFeature additionally check the kind-specific scope
	// inside the handler.
	"/crm.v1.CrmService/UpsertFeature": ScopeRead,
	"/crm.v1.CrmService/DeleteFeature": ScopeRead,
}

// FeatureScope returns the scope required to write the given feature kind.
func FeatureScope(kind string) (string, error) {
	switch kind {
	case "roof":
		return ScopeRoofWrite, nil
	case "tree":
		return ScopeTreeWrite, nil
	case "object":
		return ScopeObjectWrite, nil
	default:
		return "", fmt.Errorf("unknown feature kind %q", kind)
	}
}

// LocalPrincipal is the principal injected for unauthenticated requests when
// the server runs with local no-auth mode (-crm-noauth / CRM_NOAUTH=1). It
// carries full admin scopes and is audited as "local-noauth".
func LocalPrincipal() Principal {
	return Principal{
		UserID:   0,
		Name:     "local",
		Roles:    []string{"admin"},
		Scopes:   ResolveScopes([]string{"admin"}, nil),
		KeyLabel: "local-noauth",
	}
}

// NewAuthInterceptor authenticates bearer credentials and enforces the
// procedure scope table. Requests without valid scopes fail closed.
//
// Passing allowNoAuth=true enables local-dev mode: requests with no bearer
// token are served as the local admin principal instead of being rejected.
// Requests that *do* present a token are still authenticated normally. This
// must only be enabled for loopback development (task start:server); prod
// deployments leave it off so anonymous requests keep failing closed.
func NewAuthInterceptor(store *Store, allowNoAuth ...bool) connect.UnaryInterceptorFunc {
	noAuth := len(allowNoAuth) > 0 && allowNoAuth[0]
	return func(next connect.UnaryFunc) connect.UnaryFunc {
		return func(ctx context.Context, req connect.AnyRequest) (connect.AnyResponse, error) {
			required, known := requiredScope[req.Spec().Procedure]
			if !known {
				return nil, connect.NewError(connect.CodePermissionDenied,
					fmt.Errorf("procedure %s is not available", req.Spec().Procedure))
			}
			token := bearerToken(req.Header().Get("Authorization"))
			if token == "" && noAuth {
				return next(WithPrincipal(ctx, LocalPrincipal()), req)
			}
			principal, err := store.Authenticate(ctx, token)
			if err != nil {
				return nil, connect.NewError(connect.CodeUnauthenticated, err)
			}
			if !principal.HasScope(required) {
				return nil, connect.NewError(connect.CodePermissionDenied,
					fmt.Errorf("missing required scope %q", required))
			}
			return next(WithPrincipal(ctx, principal), req)
		}
	}
}

func bearerToken(header string) string {
	const prefix = "Bearer "
	if len(header) > len(prefix) && strings.EqualFold(header[:len(prefix)], prefix) {
		return strings.TrimSpace(header[len(prefix):])
	}
	return ""
}

func generateKey() (secret, prefix, hash string, err error) {
	raw := make([]byte, 24)
	if _, err := rand.Read(raw); err != nil {
		return "", "", "", fmt.Errorf("generating key material: %w", err)
	}
	secret = "addr_" + base64.RawURLEncoding.EncodeToString(raw)
	prefix = secret[:14]
	hash = hashToken(secret)
	return secret, prefix, hash, nil
}

func hashToken(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

// ConstantTimeEqual compares two token hashes without leaking timing.
func ConstantTimeEqual(a, b string) bool {
	return subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1
}
