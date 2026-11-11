package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	feedbackpacket "github.com/benebsworth/addr/internal/annotation/feedback"
	"github.com/benebsworth/addr/internal/annotation/migration"
	"github.com/benebsworth/addr/internal/annotation/push"
)

func localPacket(t *testing.T) string {
	t.Helper()
	p := &feedbackpacket.Packet{SchemaVersion: 1, Origin: "https://fieldwork.example", ProjectID: "mooroolbark", SnapshotID: strings.Repeat("a", 64), ManifestSHA256: strings.Repeat("b", 64), Cursor: "538", Entries: []feedbackpacket.Entry{}, Quarantined: []feedbackpacket.Quarantine{}}
	p.Digest, _ = p.ComputeDigest()
	path := filepath.Join(t.TempDir(), "feedback.json")
	if err := feedbackpacket.Write(context.Background(), path, p); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLocalFeedbackUsesSeparateCredentialsAndExactMode(t *testing.T) {
	t.Setenv("ANNOTATION_TOKEN", "remote-credential-must-never-be-used")
	t.Setenv("ANNOTATION_URL", ":invalid-remote-url")
	packet := localPacket(t)
	localToken := filepath.Join(t.TempDir(), "local-token")
	if err := os.WriteFile(localToken, []byte("separate-local-token\n"), 0600); err != nil {
		t.Fatal(err)
	}
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/api/scenes/feedback" || r.Method != "POST" {
			t.Errorf("unexpected local request %s %s", r.Method, r.URL.Path)
		}
		wantAuth := ""
		if calls == 2 {
			wantAuth = "Bearer separate-local-token"
		}
		if r.Header.Get("Authorization") != wantAuth {
			t.Error("remote credential leaked or local credential missing")
		}
		var req struct {
			Bundle       feedbackpacket.Packet `json:"bundle"`
			Mode, PlanID string
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Error(err)
		}
		if req.Bundle.Cursor != "538" {
			t.Error("packet was not forwarded")
		}
		if calls == 1 && (req.Mode != "preview" || req.PlanID != "") || calls == 2 && (req.Mode != "apply" || req.PlanID != "reviewed-plan") {
			t.Error("preview/apply mode or plan ID lost")
		}
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, `{"planId":"reviewed-plan","counts":{"ready":0}}`)
	}))
	defer server.Close()
	for _, args := range [][]string{
		{"feedback-preview", "--file", packet, "--local-url", server.URL, "--token-file", "/does-not-exist-remote-token"},
		{"feedback-apply", "--file", packet, "--local-url", server.URL, "--local-token-file", localToken, "--plan-id", "reviewed-plan"},
	} {
		var output bytes.Buffer
		if err := run(context.Background(), args, strings.NewReader(""), &output, io.Discard); err != nil {
			t.Fatal(err)
		}
		if !json.Valid(output.Bytes()) || !bytes.Contains(output.Bytes(), []byte(`"planId":"reviewed-plan"`)) {
			t.Fatal("local response did not remain JSON")
		}
	}
	if calls != 2 {
		t.Fatalf("calls=%d", calls)
	}
}

func TestLocalFeedbackRejectsRedirectsCorruptionAndMissingPlan(t *testing.T) {
	packet := localPacket(t)
	var followed atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { followed.Add(1); w.WriteHeader(200) }))
	defer target.Close()
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
	}))
	defer redirect.Close()
	var output bytes.Buffer
	err := run(context.Background(), []string{"feedback-preview", "--file", packet, "--local-url", redirect.URL}, strings.NewReader(""), &output, io.Discard)
	if err == nil || !strings.Contains(err.Error(), "307") || followed.Load() != 0 {
		t.Fatal("followed local API redirect")
	}
	if err := run(context.Background(), []string{"feedback-apply", "--file", packet, "--local-url", target.URL}, strings.NewReader(""), &output, io.Discard); err == nil || !strings.Contains(err.Error(), "--plan-id") {
		t.Fatal("applied without a reviewed plan")
	}
	raw, _ := os.ReadFile(packet)
	raw = bytes.Replace(raw, []byte(`"cursor": "538"`), []byte(`"cursor": "539"`), 1)
	if err := os.WriteFile(packet, raw, 0600); err != nil {
		t.Fatal(err)
	}
	if err := run(context.Background(), []string{"feedback-preview", "--file", packet, "--local-url", target.URL}, strings.NewReader(""), &output, io.Discard); err == nil || !strings.Contains(err.Error(), "digest") {
		t.Fatal("submitted tampered packet")
	}
	if followed.Load() != 0 {
		t.Fatal("invalid packet or missing plan made a network request")
	}
}

func TestLocalFeedbackSelectionUsesSamePacketForPreviewAndApply(t *testing.T) {
	path := localPacket(t)
	raw, _ := os.ReadFile(path)
	packet, err := feedbackpacket.Read(raw)
	if err != nil {
		t.Fatal(err)
	}
	source := `{"coordinate":0.0016998300169983002,"counter":9007199254740993}`
	evidence := feedbackpacket.Evidence{SHA256: migration.Digest([]byte(source)), JSON: source}
	packet.Entries = []feedbackpacket.Entry{
		{ItemID: "scene-a", SceneID: "a", BaseItem: evidence, Item: evidence, Model: evidence, Review: &evidence},
		{ItemID: "scene-b", SceneID: "b", BaseItem: evidence, Item: evidence, Model: evidence},
	}
	packet.Quarantined = []feedbackpacket.Quarantine{{ItemID: "scene-c", Reason: "changed_immutable_source"}}
	packet.Digest, _ = packet.ComputeDigest()
	if err := feedbackpacket.Write(context.Background(), path, packet); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(path)
	var calls int
	var selectedDigest string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		var req struct {
			Bundle json.RawMessage `json:"bundle"`
			Mode   string          `json:"mode"`
			PlanID string          `json:"planId"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Error(err)
		}
		selected, err := feedbackpacket.Read(req.Bundle)
		if err != nil {
			t.Error(err)
			w.WriteHeader(400)
			return
		}
		if selected.Selection != "scene-a" || len(selected.Entries) != 1 || selected.Entries[0].Model.JSON != source || len(selected.Quarantined) != 0 || selected.ManifestSHA256 != packet.ManifestSHA256 || selected.Digest == packet.Digest {
			t.Error("selection lost evidence or kept unrelated scenes")
		}
		if calls == 1 {
			selectedDigest = selected.Digest
		} else if selected.Digest != selectedDigest || req.Mode != "apply" || req.PlanID != "selected-plan" {
			t.Error("apply did not preserve the preview selection")
		}
		io.WriteString(w, `{"planId":"selected-plan"}`)
	}))
	defer server.Close()
	for _, command := range []string{"feedback-preview", "feedback-apply"} {
		args := []string{command, "--file", path, "--id", "scene-a", "--local-url", server.URL, "--token-file", "/remote-token-must-not-be-read"}
		if command == "feedback-apply" {
			args = append(args, "--plan-id", "selected-plan")
		}
		if err := run(context.Background(), args, strings.NewReader(""), io.Discard, io.Discard); err != nil {
			t.Fatal(err)
		}
	}
	if err := run(context.Background(), []string{"feedback-preview", "--file", path, "--id", "missing", "--local-url", server.URL}, strings.NewReader(""), io.Discard, io.Discard); err == nil || calls != 2 {
		t.Fatal("missing selection made a request or succeeded")
	}
	after, _ := os.ReadFile(path)
	if !bytes.Equal(before, after) {
		t.Fatal("local selection rewrote the packet file")
	}
}

func TestLocalFeedbackReportsBoundedActionableError(t *testing.T) {
	packet := localPacket(t)
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("local-private-value"), 0600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		json.NewEncoder(w).Encode(map[string]any{"error": "Plan is stale; preview again. local-private-value " + strings.Repeat("details ", 200), "body": "must not be dumped", "applied": []string{"first", "second"}, "retryPlanId": "replacement-plan"})
	}))
	defer server.Close()
	var output bytes.Buffer
	err := run(context.Background(), []string{"feedback-apply", "--file", packet, "--local-url", server.URL, "--local-token-file", tokenFile, "--plan-id", "old-plan"}, strings.NewReader(""), &output, io.Discard)
	if err == nil || !strings.Contains(err.Error(), "Plan is stale") || !strings.Contains(err.Error(), "applied: 2") || !strings.Contains(err.Error(), "retryPlanId: replacement-plan") || strings.Contains(err.Error(), "local-private-value") || strings.Contains(err.Error(), "must not be dumped") || len(err.Error()) > 740 {
		t.Fatalf("unsafe/unhelpful local error: %v", err)
	}
	var result struct {
		Status       int    `json:"status"`
		Error        string `json:"error"`
		RetryPlanID  string `json:"retryPlanId"`
		AppliedCount int    `json:"appliedCount"`
	}
	if err := json.Unmarshal(output.Bytes(), &result); err != nil || result.Status != 409 || result.RetryPlanID != "replacement-plan" || result.AppliedCount != 2 || len(result.Error) > 512 || bytes.Contains(output.Bytes(), []byte("local-private-value")) || bytes.Contains(output.Bytes(), []byte("must not be dumped")) || output.Len() > 740 {
		t.Fatalf("unsafe/incomplete structured error: %s (%v)", output.String(), err)
	}
}

func TestLocalFeedbackErrorParsesRecoveryFieldsAfterLargePlan(t *testing.T) {
	const stale = "The local review, receipt or packet changed since preview. Preview again; no feedback was applied."
	for _, malformed := range []bool{false, true} {
		raw := `{"scenes":"` + strings.Repeat("x", 16384) + `","error":"` + stale + `"}`
		if malformed {
			raw += `invalid`
		}
		var output bytes.Buffer
		response := &http.Response{StatusCode: http.StatusConflict, Body: io.NopCloser(strings.NewReader(raw))}
		if err := localFeedbackError(response, "", &output); err == nil {
			t.Fatal("HTTP conflict returned success")
		}
		var result map[string]any
		if err := json.Unmarshal(output.Bytes(), &result); err != nil {
			t.Fatal(err)
		}
		if malformed {
			if len(result) != 1 || result["status"] != float64(409) {
				t.Fatal("malformed response was represented as a safe stale-plan rejection")
			}
		} else if result["error"] != stale || result["status"] != float64(409) || len(result) != 2 {
			t.Fatal("large plan hid recovery fields or leaked unrelated response data")
		}
	}
	var output bytes.Buffer
	response := &http.Response{StatusCode: http.StatusServiceUnavailable, Body: io.NopCloser(strings.NewReader(`{"scenes":"` + strings.Repeat("x", 16384) + `","error":"Review save interrupted","complete":false,"applied":[{"itemId":"scene-a"}],"retryPlanId":"original-plan"}`))}
	if err := localFeedbackError(response, "", &output); err == nil {
		t.Fatal("partial apply returned success")
	}
	var partial map[string]any
	if err := json.Unmarshal(output.Bytes(), &partial); err != nil || partial["status"] != float64(503) || partial["complete"] != false || partial["retryPlanId"] != "original-plan" || partial["appliedCount"] != float64(1) || len(partial) != 5 {
		t.Fatal("large partial plan hid retry metadata")
	}
	output.Reset()
	response = &http.Response{StatusCode: http.StatusConflict, Body: io.NopCloser(strings.NewReader(`{"error":"` + stale + `","retryPlanId":"invalid retry ID"}`))}
	_ = localFeedbackError(response, "", &output)
	if output.String() != "{\"status\":409}\n" {
		t.Fatal("invalid retry metadata became a safe stale-plan rejection")
	}
}

func TestLocalFeedbackErrorPreservesExplicitReplanAndPrefixCounts(t *testing.T) {
	body := map[string]any{
		"error": "This scene changed after preview.", "complete": false, "canReplan": true, "retryPlanId": "original-plan",
		"applied": []map[string]any{
			{"alreadyApplied": false, "receiptOnly": false, "reviewUrl": "/scenes?scene=a&status=all"},
			{"alreadyApplied": true, "receiptOnly": false, "reviewUrl": "/scenes?scene=b&status=all"},
			{"alreadyApplied": false, "receiptOnly": true, "reviewUrl": "https://untrusted.example/private-value"},
			{"alreadyApplied": true, "receiptOnly": true, "reviewUrl": "/scenes?scene=private-value"},
		},
	}
	raw, _ := json.Marshal(body)
	var output bytes.Buffer
	response := &http.Response{StatusCode: http.StatusConflict, Body: io.NopCloser(bytes.NewReader(raw))}
	if err := localFeedbackError(response, "private-value", &output); err == nil {
		t.Fatal("replan response returned success")
	}
	var result map[string]any
	if err := json.Unmarshal(output.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result["canReplan"] != true || result["appliedCount"] != float64(4) || result["appliedScenes"] != float64(1) || result["alreadyAppliedScenes"] != float64(2) || result["receiptOnly"] != float64(1) {
		t.Fatalf("prefix recovery fields missing: %s", output.String())
	}
	links, ok := result["reviewUrls"].([]any)
	if !ok || len(links) != 2 || strings.Contains(output.String(), "untrusted.example") || strings.Contains(output.String(), "private-value") {
		t.Fatal("unsafe review links were exposed")
	}
	for _, partial := range []any{"invalid-prefix", map[string]any{"receiptOnly": false}} {
		body["applied"] = []any{partial}
		raw, _ = json.Marshal(body)
		output.Reset()
		response.Body = io.NopCloser(bytes.NewReader(raw))
		_ = localFeedbackError(response, "", &output)
		if bytes.Contains(output.Bytes(), []byte(`"canReplan"`)) || bytes.Contains(output.Bytes(), []byte(`"appliedScenes"`)) {
			t.Fatal("malformed prefix was allowed to authorize replanning")
		}
	}
}

func TestFeedbackLoadsPushReceiptsOnlyWhenPresent(t *testing.T) {
	dir := t.TempDir()
	if receipts, err := loadPushReceipts(filepath.Join(dir, "absent")); err != nil || receipts != nil {
		t.Fatalf("absent directory: %v %v", receipts, err)
	}
	if receipts, err := loadPushReceipts(""); err != nil || receipts != nil {
		t.Fatalf("empty path: %v %v", receipts, err)
	}
	// A dry-run plan directory holds no receipts: schema-1 behaviour is kept.
	planned := filepath.Join(dir, "planned")
	if err := os.MkdirAll(filepath.Join(planned, "runs", "20260929T000000Z-00000001"), 0700); err != nil {
		t.Fatal(err)
	}
	if receipts, err := loadPushReceipts(planned); err != nil || receipts != nil {
		t.Fatalf("plan-only directory: %v %v", receipts, err)
	}
	pushed := filepath.Join(dir, "pushed")
	index, err := push.LoadReceipts(pushed)
	if err != nil {
		t.Fatal(err)
	}
	doc := json.RawMessage(`{"id":"addr-scene-s-m000000000000-r1","revision":1}`)
	if err := index.RecordCreated(push.ItemReceipt{ItemID: "addr-scene-s-m000000000000-r1", SceneID: "s", RunID: "20260929T000000Z-00000001", PushedAt: "t", Revision: 1, Document: doc, Baseline: doc}); err != nil {
		t.Fatal(err)
	}
	receipts, err := loadPushReceipts(pushed)
	if err != nil || receipts == nil {
		t.Fatalf("pushed directory: %v %v", receipts, err)
	}
	if items, scenes := receipts.Len(); items != 1 || scenes != 1 {
		t.Fatalf("index %d/%d", items, scenes)
	}
	// Corrupt receipts are an error, not a silent fallback to schema 1.
	if err := os.WriteFile(filepath.Join(pushed, "items", "addr-scene-s-m000000000000-r1.json"), []byte(`{"schemaVersion":1,"itemId":"other"}`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := loadPushReceipts(pushed); err == nil {
		t.Fatal("corrupt receipt ignored")
	}
}

func TestLocalFeedbackForwardsSchema2Packet(t *testing.T) {
	source := `{"coordinate":0.0016998300169983002}`
	evidence := feedbackpacket.Evidence{SHA256: migration.Digest([]byte(source)), JSON: source}
	p := &feedbackpacket.Packet{SchemaVersion: 2, Origin: "https://fieldwork.example", ProjectID: "mooroolbark", SnapshotID: strings.Repeat("a", 64), ManifestSHA256: strings.Repeat("b", 64), Cursor: "538", Quarantined: []feedbackpacket.Quarantine{},
		Entries: []feedbackpacket.Entry{{ItemID: "addr-scene-a-m000000000000-r1", SceneID: "a", BaseItem: evidence, Item: evidence, Model: evidence, Binding: &feedbackpacket.Binding{Kind: "push", RunID: "20260929T000000Z-00000001", ReceiptSHA256: strings.Repeat("c", 64), ReceiptRevision: 1}}}}
	p.Digest, _ = p.ComputeDigest()
	path := filepath.Join(t.TempDir(), "feedback.json")
	if err := feedbackpacket.Write(context.Background(), path, p); err != nil {
		t.Fatal(err)
	}
	var forwarded *feedbackpacket.Packet
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Bundle json.RawMessage `json:"bundle"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Error(err)
		}
		forwarded, _ = feedbackpacket.Read(req.Bundle)
		io.WriteString(w, `{"planId":"p"}`)
	}))
	defer server.Close()
	if err := run(context.Background(), []string{"feedback-preview", "--file", path, "--local-url", server.URL}, strings.NewReader(""), io.Discard, io.Discard); err != nil {
		t.Fatal(err)
	}
	if forwarded == nil || forwarded.SchemaVersion != 2 || forwarded.Entries[0].Binding == nil || forwarded.Entries[0].Binding.Kind != "push" || forwarded.Digest != p.Digest {
		t.Fatalf("schema-2 packet not forwarded intact: %+v", forwarded)
	}
}
