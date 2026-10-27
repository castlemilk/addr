package annotation

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"testing"

	"github.com/lib/pq"
)

func captureDiagnosticLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var output bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&output, nil)))
	t.Cleanup(func() { slog.SetDefault(previous) })
	return &output
}

func TestInternalServiceDiagnosticsExcludeSensitiveValues(t *testing.T) {
	output := captureDiagnosticLog(t)
	postgres := &pq.Error{
		Code: "22P02", Severity: "ERROR", Message: `invalid input syntax: "private-input" at postgresql://user:secret@host/db`,
		Detail: "private-detail", Hint: "private-hint", InternalQuery: "private-query", Where: "private-parameters",
	}
	logInternalServiceError("GetItem", serviceStage("store_get", fmt.Errorf("private-wrapper: %w", postgres)))
	var logged map[string]any
	if err := json.Unmarshal(output.Bytes(), &logged); err != nil {
		t.Fatal(err)
	}
	if logged["method"] != "GetItem" || logged["stage"] != "store_get" || logged["error_type"] != "*pq.Error" || logged["pg_code"] != "22P02" || logged["pg_severity"] != "ERROR" {
		t.Fatalf("missing diagnostic classification: %v", logged)
	}
	for _, private := range []string{"private-", "user:secret", "host/db", "postgresql://"} {
		if strings.Contains(output.String(), private) {
			t.Fatalf("diagnostic exposed %q", private)
		}
	}
	output.Reset()
	logInternalServiceError("GetItem", serviceStage("decode_item", errors.New("private arbitrary error with password=secret")))
	if !strings.Contains(output.String(), `"error_type":"*errors.errorString"`) || strings.Contains(output.String(), "private") || strings.Contains(output.String(), "secret") {
		t.Fatal("non-Postgres error text must never be logged")
	}
}

func TestExpectedServiceErrorsDoNotLog(t *testing.T) {
	output := captureDiagnosticLog(t)
	for _, err := range []error{nil, ErrInvalid, ErrConflict, ErrNotFound, context.Canceled, context.DeadlineExceeded} {
		logInternalServiceError("GetItem", serviceStage("store_get", err))
	}
	if output.Len() != 0 {
		t.Fatalf("expected errors produced diagnostic noise: %s", output)
	}
}

type diagnosticStore struct {
	Store
	document        json.RawMessage
	getErr, listErr error
}

func (s diagnosticStore) Get(context.Context, string, string) (json.RawMessage, error) {
	return s.document, s.getErr
}
func (s diagnosticStore) List(context.Context, string, ListOptions) ([]json.RawMessage, string, error) {
	return nil, "", s.listErr
}

func TestGetItemDiagnosticStages(t *testing.T) {
	for _, tc := range []struct {
		stage string
		store diagnosticStore
	}{
		{"store_get", diagnosticStore{getErr: errors.New("private-store-error")}},
		{"decode_item", diagnosticStore{document: json.RawMessage(`{`)}},
		{"list_labels", diagnosticStore{document: json.RawMessage(`{"id":"item","projectId":"project"}`), listErr: errors.New("private-label-error")}},
	} {
		t.Run(tc.stage, func(t *testing.T) {
			output := captureDiagnosticLog(t)
			service := &Service{Store: tc.store}
			_, err := service.Call(context.Background(), "GetItem", Object{"id": "private-item-parameter"})
			if err == nil || connectError(err).Error() != "internal: annotation storage request failed" {
				t.Fatalf("public error contract changed: %v", err)
			}
			var logged map[string]any
			if err := json.Unmarshal(output.Bytes(), &logged); err != nil {
				t.Fatal(err)
			}
			if logged["method"] != "GetItem" || logged["stage"] != tc.stage || strings.Contains(output.String(), "private") {
				t.Fatalf("incorrect or unsafe diagnostic: %v", logged)
			}
		})
	}
}
