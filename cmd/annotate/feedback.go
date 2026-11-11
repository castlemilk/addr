package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	feedbackpacket "github.com/benebsworth/addr/internal/annotation/feedback"
	"github.com/benebsworth/addr/internal/annotation/migration"
	"github.com/benebsworth/addr/internal/annotation/push"
	"github.com/benebsworth/addr/internal/annotationsync"
)

// loadPushReceipts returns the push receipt index under dir, or nil when the
// directory does not exist or holds no item/scene receipts (a dry-run plan
// alone is not a receipt). nil keeps the schema-1 packet; an index yields the
// schema-2 packet in which every item our push wrote is compared against the
// receipt, not the frozen bundle. Corrupt receipts are an error, never ignored.
func loadPushReceipts(dir string) (*push.ReceiptIndex, error) {
	if dir == "" {
		return nil, nil
	}
	if _, err := os.Stat(dir); errors.Is(err, os.ErrNotExist) {
		return nil, nil
	} else if err != nil {
		return nil, err
	}
	receipts, err := push.LoadReceipts(dir)
	if err != nil {
		return nil, err
	}
	if items, scenes := receipts.Len(); items == 0 && scenes == 0 {
		return nil, nil
	}
	return receipts, nil
}

func (c *client) feedback(ctx context.Context, o options, output io.Writer) error {
	if o.project == "" || o.directory == "" || o.snapshot == "" || o.file == "" || o.file == "-" {
		return errors.New("feedback requires --project, --dir, --snapshot, and --file output path")
	}
	if o.cursor != "" {
		return errors.New("feedback resumes its mirror checkpoint; do not pass --cursor")
	}
	packetPath, err := filepath.Abs(o.file)
	if err != nil {
		return err
	}
	mirrorPath, err := filepath.Abs(filepath.Join(o.directory, annotationsync.FileName))
	if err != nil {
		return err
	}
	snapshotPath, err := filepath.Abs(o.snapshot)
	if err != nil {
		return err
	}
	relative, err := filepath.Rel(snapshotPath, packetPath)
	if err != nil {
		return err
	}
	if packetPath == mirrorPath || relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return errors.New("feedback output must not replace the mirror or any frozen snapshot file")
	}
	bundle, err := migration.OpenBundle(o.snapshot)
	if err != nil {
		return err
	}
	summary, err := annotationsync.Sync(ctx, c.fetchChanges, c.baseURL, o.project, o.directory, o.limit)
	if err != nil {
		return err
	}
	mirror, err := annotationsync.Load(o.directory, c.baseURL, o.project)
	if err != nil {
		return err
	}
	receiptsDir := o.pushReceipts
	if receiptsDir == "" {
		receiptsDir = filepath.Join(o.directory, "push")
	}
	receipts, err := loadPushReceipts(receiptsDir)
	if err != nil {
		return fmt.Errorf("push receipts: %w", err)
	}
	packet, err := feedbackpacket.Build(ctx, mirror, bundle, o.id, receipts)
	if err != nil {
		return err
	}
	if err := feedbackpacket.Write(ctx, packetPath, packet); err != nil {
		return err
	}
	var pushReceipts any
	if receipts != nil {
		items, scenes := receipts.Len()
		pushReceipts = map[string]any{"dir": receiptsDir, "items": items, "scenes": scenes}
	}
	return json.NewEncoder(output).Encode(map[string]any{
		"schemaVersion": 1, "packetSchemaVersion": packet.SchemaVersion, "packetFile": packetPath, "digest": packet.Digest,
		"origin": packet.Origin, "projectId": packet.ProjectID, "cursor": packet.Cursor,
		"snapshotId": packet.SnapshotID, "entries": len(packet.Entries),
		"quarantined": len(packet.Quarantined), "selection": packet.Selection, "sync": summary,
		"pushReceipts": pushReceipts,
	})
}

// Local preview/apply deliberately does not initialize the remote API client or
// read ANNOTATION_TOKEN. Local credentials are separate and redirects are denied.
func localFeedback(ctx context.Context, command string, o options, input io.Reader, output io.Writer) error {
	if o.file == "" || o.file == "-" {
		return errors.New("feedback preview/apply requires --file packet path")
	}
	if command == "feedback-apply" && strings.TrimSpace(o.planID) == "" {
		return errors.New("feedback-apply requires --plan-id from preview")
	}
	endpoint, err := url.Parse(o.localURL)
	if err != nil || endpoint.Hostname() == "" || endpoint.User != nil || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		return errors.New("--local-url must be an API URL without credentials, query, or fragment")
	}
	if endpoint.Scheme != "https" {
		ip := net.ParseIP(endpoint.Hostname())
		if endpoint.Scheme != "http" || !(endpoint.Hostname() == "localhost" || ip != nil && ip.IsLoopback()) {
			return errors.New("local API requires HTTPS except on loopback")
		}
	}
	data, err := readInput(o.file, input)
	if err != nil {
		return err
	}
	packet, err := feedbackpacket.Read(data)
	if err != nil {
		return err
	}
	if o.id != "" {
		packet, err = packet.Select(o.id)
		if err != nil {
			return err
		}
		data, err = json.Marshal(packet)
		if err != nil {
			return err
		}
	}
	request := map[string]any{"bundle": json.RawMessage(data), "mode": strings.TrimPrefix(command, "feedback-")}
	if command == "feedback-apply" {
		request["planId"] = o.planID
	}
	body, err := json.Marshal(request)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(o.localURL, "/")+"/api/scenes/feedback", bytes.NewReader(body))
	if err != nil {
		return errors.New("cannot create local feedback request")
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	localToken := ""
	if o.localTokenFile != "" {
		token, err := os.ReadFile(o.localTokenFile)
		if err != nil {
			return fmt.Errorf("read local token file: %w", err)
		}
		localToken = strings.TrimSpace(string(token))
		req.Header.Set("Authorization", "Bearer "+localToken)
	}
	client := &http.Client{Timeout: 120 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("local feedback request failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return localFeedbackError(resp, localToken, output)
	}
	result, err := io.ReadAll(io.LimitReader(resp.Body, maxInputBytes+1))
	if err != nil {
		return err
	}
	if len(result) > maxInputBytes {
		return errors.New("local feedback response exceeds 64 MiB")
	}
	if !json.Valid(result) {
		return errors.New("local feedback API returned invalid JSON")
	}
	if _, err := output.Write(result); err != nil {
		return err
	}
	_, err = fmt.Fprintln(output)
	return err
}

// Recovery metadata can follow a large plan in the response. Parse the whole
// bounded body, but expose only these small fields; an unknown failure is never
// represented as a safe pre-write rejection.
func localFeedbackError(resp *http.Response, localToken string, output io.Writer) error {
	var envelope struct {
		Status               int      `json:"status"`
		Error                string   `json:"error,omitempty"`
		Complete             *bool    `json:"complete,omitempty"`
		CanReplan            *bool    `json:"canReplan,omitempty"`
		RetryPlanID          string   `json:"retryPlanId,omitempty"`
		AppliedCount         *int     `json:"appliedCount,omitempty"`
		AppliedScenes        *int     `json:"appliedScenes,omitempty"`
		AlreadyAppliedScenes *int     `json:"alreadyAppliedScenes,omitempty"`
		ReceiptOnly          *int     `json:"receiptOnly,omitempty"`
		ReviewURLs           []string `json:"reviewUrls,omitempty"`
	}
	envelope.Status = resp.StatusCode
	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, maxInputBytes+1))
	var detail struct {
		Error       string            `json:"error"`
		Complete    *bool             `json:"complete"`
		CanReplan   *bool             `json:"canReplan"`
		RetryPlanID string            `json:"retryPlanId"`
		Applied     []json.RawMessage `json:"applied"`
	}
	decoded := readErr == nil && len(raw) <= maxInputBytes && json.Unmarshal(raw, &detail) == nil
	validRetryID := detail.RetryPlanID == "" || len(detail.RetryPlanID) <= 128 && !strings.ContainsAny(detail.RetryPlanID, "\r\n\t ")
	if decoded && validRetryID {
		envelope.Error = detail.Error
		if localToken != "" {
			envelope.Error = strings.ReplaceAll(envelope.Error, localToken, "[redacted]")
		}
		if len(envelope.Error) > 512 {
			envelope.Error = envelope.Error[:512]
		}
		envelope.Complete = detail.Complete
		envelope.CanReplan = detail.CanReplan
		if resp.StatusCode != http.StatusConflict || detail.Complete == nil || *detail.Complete || detail.Applied == nil || detail.RetryPlanID == "" {
			envelope.CanReplan = nil
		}
		if detail.Applied != nil {
			count := len(detail.Applied)
			envelope.AppliedCount = &count
			applied, already, receiptOnly := 0, 0, 0
			valid := true
			links, seen := []string{}, map[string]bool{}
			for _, rawScene := range detail.Applied {
				var scene struct {
					AlreadyApplied *bool  `json:"alreadyApplied"`
					ReceiptOnly    *bool  `json:"receiptOnly"`
					ReviewURL      string `json:"reviewUrl"`
				}
				if json.Unmarshal(rawScene, &scene) != nil || scene.AlreadyApplied == nil || scene.ReceiptOnly == nil {
					valid = false
					break
				}
				if *scene.AlreadyApplied {
					already++
				} else if *scene.ReceiptOnly {
					receiptOnly++
				} else {
					applied++
				}
				link := scene.ReviewURL
				if len(links) < 32 && len(link) <= 512 && strings.HasPrefix(link, "/scenes?") && !strings.ContainsAny(link, "\r\n\t ") && (localToken == "" || !strings.Contains(link, localToken)) && !seen[link] {
					links, seen[link] = append(links, link), true
				}
			}
			if valid {
				envelope.AppliedScenes, envelope.AlreadyAppliedScenes, envelope.ReceiptOnly = &applied, &already, &receiptOnly
				envelope.ReviewURLs = links
			} else {
				// Malformed prefix outcomes cannot authorize abandoning a plan.
				envelope.CanReplan = nil
			}
		}
		if id := detail.RetryPlanID; id != "" {
			if localToken != "" {
				id = strings.ReplaceAll(id, localToken, "[redacted]")
			}
			envelope.RetryPlanID = id
		}
	}
	if err := json.NewEncoder(output).Encode(envelope); err != nil {
		return err
	}
	message := fmt.Sprintf("local feedback API returned HTTP %d", resp.StatusCode)
	if envelope.Error != "" {
		message += ": " + strings.Join(strings.Fields(envelope.Error), " ")
	}
	if envelope.AppliedCount != nil {
		message += fmt.Sprintf("; applied: %d", *envelope.AppliedCount)
	}
	if envelope.RetryPlanID != "" {
		message += "; retryPlanId: " + envelope.RetryPlanID
	}
	return errors.New(message)
}
