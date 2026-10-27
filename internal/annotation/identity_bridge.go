package annotation

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"
)

const IdentityHeader = "X-Fieldwork-Identity"

// Only the service's edge verifier can issue these short-lived, request-bound
// assertions. This key is separate from the old single-workspace credential.
type BridgeIdentity struct {
	UID           string `json:"uid"`
	Email         string `json:"email"`
	EmailVerified bool   `json:"emailVerified"`
	Provider      string `json:"provider"`
	Method        string `json:"method"`
	URI           string `json:"uri"`
	BodySHA256    string `json:"bodySha256"`
	IssuedAt      int64  `json:"iat"`
	ExpiresAt     int64  `json:"exp"`
}

func verifyBridge(r *http.Request, key string, now time.Time) (BridgeIdentity, error) {
	var identity BridgeIdentity
	value := r.Header.Get(IdentityHeader)
	if len(key) < 32 || len(value) > 4096 {
		return identity, errors.New("identity assertion is not configured or invalid")
	}
	parts := strings.Split(value, ".")
	if len(parts) != 2 {
		return identity, errors.New("invalid identity assertion")
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return identity, errors.New("invalid identity signature")
	}
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write([]byte(parts[0]))
	if !hmac.Equal(signature, mac.Sum(nil)) {
		return identity, errors.New("invalid identity signature")
	}
	encoded, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil || json.Unmarshal(encoded, &identity) != nil {
		return identity, errors.New("invalid identity claims")
	}
	unix := now.Unix()
	if identity.UID == "" || len(identity.UID) > 128 || identity.IssuedAt > unix+5 || identity.IssuedAt < unix-90 || identity.ExpiresAt < unix || identity.ExpiresAt > identity.IssuedAt+90 || identity.ExpiresAt <= identity.IssuedAt || identity.Method != r.Method || identity.URI != r.URL.RequestURI() {
		return identity, errors.New("expired or mismatched identity assertion")
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, MaxRequestBytes+1))
	if err != nil || len(body) > MaxRequestBytes {
		return identity, errors.New("invalid assertion body")
	}
	r.Body = io.NopCloser(bytes.NewReader(body))
	digest := sha256.Sum256(body)
	if identity.BodySHA256 != hex.EncodeToString(digest[:]) {
		return identity, errors.New("identity body mismatch")
	}
	return identity, nil
}
