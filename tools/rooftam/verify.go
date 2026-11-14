package main

import (
	"archive/zip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"path"
	"path/filepath"
	"sort"
	"strings"
)

type reviewError struct{ reason string }

func (e *reviewError) Error() string { return e.reason }

type receipt struct {
	Schema     int               `json:"schema_version"`
	RunID      string            `json:"run_id"`
	Commit     string            `json:"addr_commit"`
	ConfigHash string            `json:"config_sha256"`
	Kernel     string            `json:"kernel"`
	Status     string            `json:"status"`
	Artifacts  map[string]string `json:"artifacts"`
	Stages     []struct {
		Name string `json:"name"`
		Code int    `json:"exit_code"`
	} `json:"stages"`
}
type evidence struct {
	Status         string  `json:"status"`
	Synthetic      *bool   `json:"synthetic"`
	WeightsChanged bool    `json:"weights_changed"`
	Steps          int     `json:"steps"`
	FinalLoss      float64 `json:"final_loss"`
	Adapter        string  `json:"adapter_sha256"`
	Dataset        string  `json:"dataset_sha256"`
	Split          string  `json:"split"`
	Environment    struct {
		Device   string `json:"device"`
		Type     string `json:"xla_device_type"`
		Revision string `json:"addr_revision"`
	} `json:"environment"`
	Configuration struct {
		Model  string `json:"model"`
		Device string `json:"device"`
	} `json:"configuration"`
	Summary struct {
		N   int     `json:"n"`
		IoU float64 `json:"iou"`
	} `json:"summary"`
	XLA *struct {
		Review   bool             `json:"requires_fallback_review"`
		Counters map[string]int64 `json:"aten_counters"`
	} `json:"xla"`
}
type verdict struct {
	Status        string   `json:"status"`
	RunID         string   `json:"run_id"`
	Commit        string   `json:"addr_commit"`
	SyntheticOnly bool     `json:"synthetic_only"`
	Warnings      []string `json:"warnings"`
	Error         string   `json:"error,omitempty"`
}

// Inspect downloaded content without extracting or executing it.
func readArchive(file string) (map[string][]byte, map[string]string, error) {
	z, err := zip.OpenReader(file)
	if err != nil {
		return nil, nil, err
	}
	defer z.Close()
	if len(z.File) < 2 || len(z.File) > 10000 {
		return nil, nil, errors.New("invalid archive entry count")
	}
	docs := map[string][]byte{}
	hashes := map[string]string{}
	seen := map[string]bool{}
	var total uint64
	for _, f := range z.File {
		n := f.Name
		if n == "" || strings.ContainsAny(n, "\\\x00") || strings.HasPrefix(n, "/") || path.Clean(n) != n || n == ".." || strings.HasPrefix(n, "../") || strings.Contains(n, ":") || !f.Mode().IsRegular() {
			return nil, nil, fmt.Errorf("unsafe archive entry %q", n)
		}
		if seen[n] {
			return nil, nil, fmt.Errorf("duplicate archive entry %q", n)
		}
		seen[n] = true
		if f.UncompressedSize64 > 2<<30 || total+f.UncompressedSize64 > 4<<30 {
			return nil, nil, errors.New("archive exceeds 2 GiB/file or 4 GiB/run limit")
		}
		total += f.UncompressedSize64
		r, err := f.Open()
		if err != nil {
			return nil, nil, err
		}
		h := sha256.New()
		var size int64
		if strings.HasSuffix(n, ".json") {
			if f.UncompressedSize64 > 8<<20 {
				r.Close()
				return nil, nil, fmt.Errorf("JSON report too large: %s", n)
			}
			b, readErr := io.ReadAll(io.LimitReader(r, int64(f.UncompressedSize64)+1))
			if readErr != nil {
				r.Close()
				return nil, nil, readErr
			}
			size = int64(len(b))
			h.Write(b)
			docs[n] = b
		} else {
			size, err = io.Copy(h, io.LimitReader(r, int64(f.UncompressedSize64)+1))
		}
		closeErr := r.Close()
		if err != nil {
			return nil, nil, err
		}
		if closeErr != nil {
			return nil, nil, closeErr
		}
		if uint64(size) != f.UncompressedSize64 {
			return nil, nil, errors.New("archive entry size mismatch")
		}
		hashes[n] = hex.EncodeToString(h.Sum(nil))
	}
	var r receipt
	if err = json.Unmarshal(docs["receipt.json"], &r); err != nil {
		return nil, nil, fmt.Errorf("missing/invalid receipt: %w", err)
	}
	if len(r.Artifacts) != len(hashes)-1 {
		return nil, nil, errors.New("artifact manifest does not cover archive")
	}
	for n, h := range hashes {
		if n != "receipt.json" && r.Artifacts[n] != h {
			return nil, nil, fmt.Errorf("artifact hash mismatch: %s", n)
		}
	}
	return docs, hashes, nil
}
func verifyEvidence(docs map[string][]byte, hashes map[string]string, c config) (verdict, error) {
	v := verdict{Status: "failed", RunID: c.RunID, Commit: c.Commit, SyntheticOnly: c.Manifest == "", Warnings: []string{}}
	var r receipt
	if err := json.Unmarshal(docs["receipt.json"], &r); err != nil {
		return v, err
	}
	if r.Schema != 1 || r.RunID != c.RunID || r.Commit != c.Commit || r.ConfigHash != c.Hash || r.Kernel != c.Kernel {
		return v, errors.New("receipt belongs to a different run/commit/config")
	}
	if r.Status != "passed" {
		return v, errors.New("remote orchestrator failed; inspect ZIP logs and orchestrator-failure.txt")
	}
	stageNames := map[string]bool{}
	for _, s := range r.Stages {
		if s.Code != 0 {
			return v, fmt.Errorf("stage failed: %s", s.Name)
		}
		stageNames[s.Name] = true
	}
	required := []string{"bootstrap", "tests", "01-preflight", "02-validation", "03-tiny-control", "04-edgetam-before", "05-edgetam-smoke", "06-edgetam-after", "07-smoke-comparison"}
	if c.Manifest != "" {
		required = append(required, "10-real-validation", "11-real-before", "12-real-train", "13-real-after", "15-real-comparison")
	}
	if c.SAM3 != "" {
		required = append(required, "14-sam3")
	}
	for _, n := range required {
		if !stageNames[n] {
			return v, fmt.Errorf("missing completed stage: %s", n)
		}
	}
	load := func(name string) (evidence, error) {
		var e evidence
		b, ok := docs[name]
		if !ok {
			return e, fmt.Errorf("missing report: %s", name)
		}
		err := json.Unmarshal(b, &e)
		if err == nil && e.Status != "passed" {
			err = fmt.Errorf("report not passed: %s", name)
		}
		return e, err
	}
	checkTPU := func(name string, e evidence) error {
		if e.Environment.Type != "TPU" || !strings.HasPrefix(e.Environment.Device, "xla:") || e.Environment.Revision != c.Commit {
			return fmt.Errorf("%s does not prove TPU execution at the pinned commit", name)
		}
		if e.XLA == nil {
			return fmt.Errorf("%s is missing XLA diagnostics", name)
		}
		keys := []string{}
		for n, value := range e.XLA.Counters {
			if value > 0 {
				keys = append(keys, n)
			}
		}
		sort.Strings(keys)
		if e.XLA.Review || len(keys) > 0 {
			v.Warnings = append(v.Warnings, name+": inspect XLA fallback counters: "+strings.Join(keys, ", "))
		}
		return nil
	}
	pre, err := load("01-preflight/preflight.json")
	if err != nil {
		return v, err
	}
	if err = checkTPU("preflight", pre); err != nil {
		return v, err
	}
	type group struct {
		before, train, after, model string
		synthetic                   bool
		steps                       int
	}
	groups := []group{{"", "03-tiny-control", "", "tiny", true, 3}, {"04-edgetam-before", "05-edgetam-smoke", "06-edgetam-after", "edgetam", true, 3}}
	if c.Manifest != "" {
		groups = append(groups, group{"11-real-before", "12-real-train", "13-real-after", "edgetam", false, c.RealSteps})
	}
	for _, g := range groups {
		e, err := load(g.train + "/run.json")
		if err != nil {
			return v, err
		}
		if err = checkTPU(g.train, e); err != nil {
			return v, err
		}
		if e.Configuration.Model != g.model || e.Configuration.Device != "tpu" || e.Synthetic == nil || *e.Synthetic != g.synthetic || !e.WeightsChanged || e.Steps != g.steps || len(e.Dataset) != 64 || e.Adapter == "" || e.Adapter != hashes[g.train+"/adapter.pt"] || math.IsNaN(e.FinalLoss) || math.IsInf(e.FinalLoss, 0) {
			return v, fmt.Errorf("invalid model/gradient/checkpoint evidence: %s", g.train)
		}
		for _, stage := range []string{g.before, g.after} {
			if stage == "" {
				continue
			}
			ev, err := load(stage + "/evaluation.json")
			if err != nil {
				return v, err
			}
			if err = checkTPU(stage, ev); err != nil {
				return v, err
			}
			if ev.Configuration.Model != g.model || ev.Configuration.Device != "tpu" || ev.Synthetic == nil || *ev.Synthetic != g.synthetic || ev.Split != "val" || ev.Summary.N < 1 || ev.Dataset != e.Dataset || ev.Summary.IoU < 0 || ev.Summary.IoU > 1 {
				return v, fmt.Errorf("invalid/mismatched evaluation: %s", stage)
			}
			if stage == g.after && ev.Adapter != e.Adapter {
				return v, errors.New("evaluation did not use the trained adapter")
			}
		}
	}
	if _, ok := docs["07-smoke-comparison/comparison.json"]; !ok {
		return v, errors.New("missing smoke comparison")
	}
	if c.Manifest != "" {
		if _, ok := docs["15-real-comparison/comparison.json"]; !ok {
			return v, errors.New("missing real-data comparison")
		}
	}
	if c.SAM3 != "" {
		if _, err = load("14-sam3/evaluation.json"); err != nil {
			return v, err
		}
	}
	v.Status = "verified"
	if len(v.Warnings) > 0 {
		v.Status = "review"
		return v, &reviewError{"TPU execution/weight updates verified, but XLA fallback review is required; see verification.json (not a clean TPU performance result)"}
	}
	return v, nil
}
func (a *app) verify(dir string, s *state) error {
	p, err := archivePath(dir, s)
	v := verdict{Status: "failed", RunID: s.Config.RunID, Commit: s.Config.Commit, SyntheticOnly: s.Config.Manifest == "", Warnings: []string{}}
	if err == nil {
		var docs map[string][]byte
		var hashes map[string]string
		docs, hashes, err = readArchive(p)
		if err == nil {
			v, err = verifyEvidence(docs, hashes, s.Config)
		}
	}
	if err != nil {
		v.Error = err.Error()
	}
	writeErr := writeJSON(filepath.Join(dir, "verification.json"), v)
	if v.Status == "verified" || v.Status == "review" {
		s.Phase = v.Status
		writeErr = errors.Join(writeErr, saveState(dir, s))
	}
	fmt.Fprintf(a.out, "Verification: %s\nResults: %s\n", v.Status, p)
	if v.SyntheticOnly {
		fmt.Fprintln(a.out, "Synthetic compatibility evidence only; no real-roof accuracy or performance claim.")
	}
	return errors.Join(err, writeErr)
}
