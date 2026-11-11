package annotation

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"strings"

	"github.com/lib/pq"
)

type serviceStageError struct {
	stage string
	err   error
}

func (e *serviceStageError) Error() string { return e.err.Error() }
func (e *serviceStageError) Unwrap() error { return e.err }

func serviceStage(stage string, err error) error {
	if err == nil {
		return nil
	}
	return &serviceStageError{stage: stage, err: err}
}

var quotedDiagnosticValue = regexp.MustCompile(`"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'`)
var diagnosticURL = regexp.MustCompile(`(?i)\b(?:postgres(?:ql)?|https?)://\S+`)

// PostgreSQL primary messages are useful diagnostics, but can quote input
// values or identifiers. Never include Detail, Hint, query text, or parameters.
func safePostgresMessage(message string) string {
	message = diagnosticURL.ReplaceAllString(message, "[redacted-url]")
	message = quotedDiagnosticValue.ReplaceAllString(message, "[redacted]")
	message = strings.Join(strings.Fields(message), " ")
	if len(message) > 512 {
		message = message[:512] + "…"
	}
	return message
}

func logInternalServiceError(method string, err error) {
	if err == nil || errors.Is(err, ErrInvalid) || errors.Is(err, ErrConflict) || errors.Is(err, ErrNotFound) || errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return
	}
	stage := "service"
	var staged *serviceStageError
	if errors.As(err, &staged) {
		stage = staged.stage
	}
	underlying := err
	for errors.Unwrap(underlying) != nil {
		underlying = errors.Unwrap(underlying)
	}
	fields := []any{"method", method, "stage", stage, "error_type", fmt.Sprintf("%T", underlying)}
	var postgres *pq.Error
	if errors.As(err, &postgres) {
		fields = append(fields, "pg_code", string(postgres.Code), "pg_severity", postgres.Severity, "pg_message", safePostgresMessage(postgres.Message))
	}
	slog.Error("annotation_internal", fields...)
}
