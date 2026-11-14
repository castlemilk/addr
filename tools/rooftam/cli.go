package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"embed"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"
)

//go:embed runner.py
var templates embed.FS

const kaggleVersion = "2.2.4"
const maxCapture = 2 << 20

var shaPattern = regexp.MustCompile(`^[0-9a-f]{40}$`)
var ownerPattern = regexp.MustCompile(`^[a-zA-Z0-9_-]{1,64}$`)
var datasetPattern = regexp.MustCompile(`^[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+$`)
var runPattern = regexp.MustCompile(`^[0-9]{8}-[0-9]{6}-[0-9a-f]{12}$`)

type config struct {
	RunID       string   `json:"run_id"`
	Kernel      string   `json:"kernel"`
	Commit      string   `json:"commit"`
	Accelerator string   `json:"accelerator"`
	Datasets    []string `json:"datasets"`
	Manifest    string   `json:"manifest"`
	SAM3        string   `json:"sam3_predictions"`
	RealSteps   int      `json:"real_steps"`
	BatchSize   int      `json:"batch_size"`
	Seed        int      `json:"seed"`
	MaxSeconds  int      `json:"max_seconds"`
	Hash        string   `json:"config_sha256"`
}
type state struct {
	Schema      int    `json:"schema_version"`
	Config      config `json:"config"`
	Phase       string `json:"phase"`
	NotebookSHA string `json:"notebook_sha256"`
	MetadataSHA string `json:"metadata_sha256"`
	Version     string `json:"kaggle_version,omitempty"`
}
type app struct {
	out, errOut io.Writer
	execFn      func(context.Context, string, []string, bool) (string, error)
	resolveFn   func(context.Context, string) (string, error)
}
type stringsFlag []string

func (s *stringsFlag) String() string     { return strings.Join(*s, ",") }
func (s *stringsFlag) Set(v string) error { *s = append(*s, v); return nil }

const usage = `RoofTAM: private Kaggle TPU experiment controller

  rooftam setup                              install the pinned official Kaggle client
  rooftam auth                               run Kaggle's interactive OAuth login
  rooftam doctor                             verify client capabilities and account access
  rooftam run --owner USER [options]          prepare, submit, wait, download, verify
  rooftam prepare --owner USER [options]      generate a private upload without submitting
  rooftam submit --run DIR                    submit a prepared run exactly once
  rooftam resume --run DIR                    wait/download/verify; NEVER resubmit
  rooftam status|logs|wait|download|verify --run DIR

Run options: --output DIR --ref main --accelerator TpuV5E8 --timeout 3h
  --max-runtime 2h --poll 30s --dataset OWNER/SLUG (repeatable)
  --manifest /kaggle/input/.../manifest.jsonl --allow-real-data
  --sam3-predictions /kaggle/input/.../predictions.json --real-steps 100
  --batch-size 1 --seed 17
Common: --kaggle /path/to/kaggle (or ROOFTAM_KAGGLE_BIN).
Setup: --python python3. Auth: --no-launch-browser.

No credentials are stored in run files. New runs use unique private notebook IDs.
Ctrl-C/local timeout stops waiting, NOT the remote Kaggle run. Use resume afterward.
Exit 0: command succeeded; exit 1: failed/unverified; exit 3: XLA fallback review required.
`

func (a *app) run(ctx context.Context, args []string) error {
	if a.out == nil {
		a.out = io.Discard
	}
	if a.errOut == nil {
		a.errOut = io.Discard
	}
	if len(args) == 0 || args[0] == "help" || args[0] == "--help" {
		fmt.Fprint(a.out, usage)
		return nil
	}
	action := args[0]
	known := map[string]bool{"setup": true, "auth": true, "doctor": true, "run": true, "prepare": true, "submit": true, "resume": true, "status": true, "logs": true, "wait": true, "download": true, "verify": true}
	if !known[action] {
		return fmt.Errorf("unknown command %q; use rooftam help", action)
	}
	f := flag.NewFlagSet(action, flag.ContinueOnError)
	f.SetOutput(a.errOut)
	kaggle := f.String("kaggle", "", "official Kaggle executable override")
	python := f.String("python", "python3", "Python 3.11+ executable for setup")
	noBrowser := f.Bool("no-launch-browser", false, "print OAuth authorization URL")
	owner := f.String("owner", "", "actual Kaggle username")
	output := f.String("output", "", "new local run directory")
	runDir := f.String("run", "", "existing run directory")
	ref := f.String("ref", "main", "addr ref; resolved to immutable commit before submission")
	acc := f.String("accelerator", "TpuV5E8", "requested TPU accelerator (availability is account-specific)")
	waitLimit := f.Duration("timeout", 3*time.Hour, "local wait limit; does not cancel remote compute")
	maxRuntime := f.Duration("max-runtime", 2*time.Hour, "remote compute time cap")
	poll := f.Duration("poll", 30*time.Second, "status poll interval")
	manifest := f.String("manifest", "", "attached Kaggle input manifest")
	sam3 := f.String("sam3-predictions", "", "matching attached SAM3 prediction export")
	allowReal := f.Bool("allow-real-data", false, "explicitly authorize attached reviewed data for this run")
	steps := f.Int("real-steps", 100, "real-data training steps")
	batch := f.Int("batch-size", 1, "fixed batch size")
	seed := f.Int("seed", 17, "experiment seed")
	var datasets stringsFlag
	f.Var(&datasets, "dataset", "existing Kaggle dataset OWNER/SLUG; repeatable")
	if err := f.Parse(args[1:]); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if f.NArg() != 0 {
		return fmt.Errorf("unexpected positional arguments: %v", f.Args())
	}
	if *waitLimit <= 0 || *poll < time.Second {
		return errors.New("timeout must be positive; poll must be at least 1s")
	}
	if action == "setup" {
		return a.setup(ctx, *python)
	}
	if action == "auth" {
		k, err := kagglePath(*kaggle)
		if err != nil {
			return err
		}
		xs := []string{"auth", "login"}
		if *noBrowser {
			xs = append(xs, "--no-launch-browser")
		}
		_, err = a.command(ctx, k, xs, true)
		return err
	}
	if action == "doctor" {
		k, err := kagglePath(*kaggle)
		if err != nil {
			return err
		}
		return a.doctor(ctx, k)
	}
	if action == "prepare" || action == "run" {
		c := config{Accelerator: *acc, Datasets: append([]string{}, datasets...), Manifest: *manifest, SAM3: *sam3, RealSteps: *steps, BatchSize: *batch, Seed: *seed, MaxSeconds: int(maxRuntime.Seconds())}
		if err := validateOptions(*owner, c, *allowReal); err != nil {
			return err
		}
		id, err := newRunID()
		if err != nil {
			return err
		}
		c.RunID = id
		c.Kernel = *owner + "/addr-rooftam-" + id
		c.Commit, err = a.resolve(ctx, *ref)
		if err != nil {
			return err
		}
		c.Hash = configHash(c)
		dir := *output
		if dir == "" {
			dir = filepath.Join("rooftam-runs", id)
		}
		s, err := prepare(dir, c)
		if err != nil {
			return err
		}
		abs, _ := filepath.Abs(dir)
		fmt.Fprintf(a.out, "Prepared %s\nRun directory: %s\nCommit: %s\n", c.Kernel, abs, c.Commit)
		if action == "prepare" {
			return nil
		}
		unlock, err := lock(dir)
		if err != nil {
			return err
		}
		defer unlock()
		k, err := kagglePath(*kaggle)
		if err != nil {
			return err
		}
		if err = a.doctor(ctx, k); err != nil {
			return err
		}
		if err = a.submit(ctx, k, dir, &s); err != nil {
			return err
		}
		return a.finish(ctx, k, dir, &s, *waitLimit, *poll)
	}
	if *runDir == "" {
		return errors.New("--run DIR is required")
	}
	unlock, err := lock(*runDir)
	if err != nil {
		return err
	}
	defer unlock()
	s, err := loadState(*runDir)
	if err != nil {
		return err
	}
	if action == "verify" {
		return a.verify(*runDir, &s)
	}
	k, err := kagglePath(*kaggle)
	if err != nil {
		return err
	}
	switch action {
	case "submit":
		return a.submit(ctx, k, *runDir, &s)
	case "resume":
		return a.finish(ctx, k, *runDir, &s, *waitLimit, *poll)
	case "status":
		status, err := a.status(ctx, k, s.Config.Kernel)
		if err == nil {
			fmt.Fprintln(a.out, status)
		}
		return err
	case "logs":
		text, err := a.command(ctx, k, []string{"kernels", "logs", s.Config.Kernel}, false)
		fmt.Fprint(a.out, text)
		return err
	case "wait":
		return a.wait(ctx, k, *runDir, &s, *waitLimit, *poll)
	case "download":
		return a.download(ctx, k, *runDir, &s)
	}
	return nil
}

func validateOptions(owner string, c config, allowReal bool) error {
	if !ownerPattern.MatchString(owner) {
		return errors.New("--owner must be your actual Kaggle username")
	}
	if !regexp.MustCompile(`^Tpu[A-Za-z0-9]+$`).MatchString(c.Accelerator) {
		return errors.New("this bench requires an explicit TPU accelerator")
	}
	if c.MaxSeconds < 60 || c.MaxSeconds > 24*60*60 {
		return errors.New("max-runtime must be between 1m and 24h")
	}
	if c.RealSteps < 1 || c.BatchSize < 1 || c.Seed < 0 || c.Seed > 2147483647 {
		return errors.New("steps/batch must be positive; seed must be 0..2147483647")
	}
	for _, d := range c.Datasets {
		if !datasetPattern.MatchString(d) {
			return fmt.Errorf("invalid dataset reference %q; use OWNER/SLUG", d)
		}
	}
	for _, p := range []string{c.Manifest, c.SAM3} {
		if p != "" && (!strings.HasPrefix(p, "/kaggle/input/") || path.Clean(p) != p || strings.ContainsAny(p, "\\\x00\r\n")) {
			return errors.New("data paths must be clean absolute paths under /kaggle/input/")
		}
	}
	if c.Manifest != "" && (!allowReal || len(c.Datasets) == 0) {
		return errors.New("real data requires --allow-real-data and at least one --dataset; reviewed rights/labels are validated remotely")
	}
	if c.Manifest == "" && (c.SAM3 != "" || len(c.Datasets) > 0) {
		return errors.New("dataset attachments and SAM3 predictions require --manifest")
	}
	return nil
}
func newRunID() (string, error) {
	var b [6]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return time.Now().UTC().Format("20060102-150405") + "-" + hex.EncodeToString(b[:]), nil
}
func digest(b []byte) string     { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func configHash(c config) string { c.Hash = ""; b, _ := json.Marshal(c); return digest(b) }
func writeJSON(p string, v any) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	return atomicWrite(p, append(b, '\n'))
}
func atomicWrite(p string, b []byte) error {
	f, err := os.CreateTemp(filepath.Dir(p), ".rooftam-*")
	if err != nil {
		return err
	}
	name := f.Name()
	defer os.Remove(name)
	if err = f.Chmod(0600); err == nil {
		_, err = f.Write(b)
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	return os.Rename(name, p)
}
func saveState(dir string, s *state) error { return writeJSON(filepath.Join(dir, "state.json"), s) }
func loadState(dir string) (state, error) {
	var s state
	b, err := os.ReadFile(filepath.Join(dir, "state.json"))
	if err != nil {
		return s, err
	}
	if err = json.Unmarshal(b, &s); err != nil {
		return s, err
	}
	if s.Schema != 1 || !shaPattern.MatchString(s.Config.Commit) || !runPattern.MatchString(s.Config.RunID) || s.Config.Hash != configHash(s.Config) {
		return s, errors.New("invalid or altered state/config")
	}
	parts := strings.Split(s.Config.Kernel, "/")
	if len(parts) != 2 || !ownerPattern.MatchString(parts[0]) || parts[1] != "addr-rooftam-"+s.Config.RunID {
		return s, errors.New("invalid run kernel")
	}
	return s, nil
}
func lock(dir string) (func(), error) {
	info, err := os.Lstat(dir)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, errors.New("run path must be a real directory")
	}
	p := filepath.Join(dir, ".lock")
	f, err := os.OpenFile(p, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return nil, fmt.Errorf("run is locked; remove %s only after confirming no other process is using it: %w", p, err)
	}
	fmt.Fprintln(f, os.Getpid())
	f.Close()
	return func() { _ = os.Remove(p) }, nil
}
func (a *app) resolve(ctx context.Context, ref string) (string, error) {
	if a.resolveFn != nil {
		return a.resolveFn(ctx, ref)
	}
	if ref == "" || strings.ContainsAny(ref, "\x00\r\n") {
		return "", errors.New("invalid addr ref")
	}
	req, err := http.NewRequestWithContext(ctx, "GET", "https://api.github.com/repos/castlemilk/addr/commits/"+url.PathEscape(ref), nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "addr-rooftam-cli")
	client := &http.Client{Timeout: 30 * time.Second}
	res, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("resolve public addr commit: %w", err)
	}
	defer res.Body.Close()
	if res.StatusCode != 200 {
		return "", fmt.Errorf("GitHub commit lookup returned HTTP %d (no credentials are sent)", res.StatusCode)
	}
	var obj struct {
		SHA string `json:"sha"`
	}
	if err = json.NewDecoder(io.LimitReader(res.Body, 8<<20)).Decode(&obj); err != nil {
		return "", err
	}
	if !shaPattern.MatchString(obj.SHA) {
		return "", errors.New("GitHub returned invalid commit")
	}
	return obj.SHA, nil
}
func prepare(dir string, c config) (state, error) {
	s := state{Schema: 1, Config: c, Phase: "prepared"}
	if err := os.MkdirAll(filepath.Dir(dir), 0700); err != nil {
		return s, err
	}
	if err := os.Mkdir(dir, 0700); err != nil {
		return s, fmt.Errorf("use a NEW output directory: %w", err)
	}
	if err := os.Mkdir(filepath.Join(dir, "upload"), 0700); err != nil {
		return s, err
	}
	cfg, _ := json.Marshal(c)
	runner, err := templates.ReadFile("runner.py")
	if err != nil {
		return s, err
	}
	cell := func(kind, source string) map[string]any {
		v := map[string]any{"cell_type": kind, "metadata": map[string]any{}, "source": source}
		if kind == "code" {
			v["execution_count"] = nil
			v["outputs"] = []any{}
		}
		return v
	}
	book := map[string]any{"nbformat": 4, "nbformat_minor": 4, "metadata": map[string]any{"kernelspec": map[string]string{"display_name": "Python 3", "language": "python", "name": "python3"}}, "cells": []any{
		cell("markdown", "# Addr RoofTAM\nPrivate TPU experiment. Generated by the Go CLI. Synthetic results are not roof accuracy. Do not reuse this run ID."),
		cell("code", "import base64, json\nCONFIG = json.loads(base64.b64decode("+strconv.Quote(base64.StdEncoding.EncodeToString(cfg))+"))\n"), cell("code", string(runner))}}
	nb, err := json.MarshalIndent(book, "", "  ")
	if err != nil {
		return s, err
	}
	nb = append(nb, '\n')
	meta := map[string]any{"id": c.Kernel, "title": "Addr RoofTAM " + c.RunID, "code_file": "rooftam.ipynb", "language": "python", "kernel_type": "notebook", "is_private": true, "enable_gpu": false, "enable_tpu": true, "enable_internet": true, "machine_shape": c.Accelerator, "dataset_sources": c.Datasets, "competition_sources": []string{}, "kernel_sources": []string{}}
	mb, err := json.MarshalIndent(meta, "", "  ")
	if err != nil {
		return s, err
	}
	mb = append(mb, '\n')
	for name, b := range map[string][]byte{"rooftam.ipynb": nb, "kernel-metadata.json": mb} {
		if err = os.WriteFile(filepath.Join(dir, "upload", name), b, 0600); err != nil {
			return s, err
		}
	}
	s.NotebookSHA = digest(nb)
	s.MetadataSHA = digest(mb)
	return s, saveState(dir, &s)
}
func verifyUpload(dir string, s *state) error {
	for name, want := range map[string]string{"rooftam.ipynb": s.NotebookSHA, "kernel-metadata.json": s.MetadataSHA} {
		b, err := os.ReadFile(filepath.Join(dir, "upload", name))
		if err != nil {
			return err
		}
		if digest(b) != want {
			return fmt.Errorf("%s changed after preparation; create a new run", name)
		}
	}
	return nil
}
func managedHome() (string, error) {
	p, err := os.UserConfigDir()
	return filepath.Join(p, "addr-rooftam", "kaggle-"+kaggleVersion), err
}
func venvBin(home, name string) string {
	if runtime.GOOS == "windows" {
		return filepath.Join(home, "Scripts", name+".exe")
	}
	return filepath.Join(home, "bin", name)
}
func kagglePath(override string) (string, error) {
	if override == "" {
		override = os.Getenv("ROOFTAM_KAGGLE_BIN")
	}
	if override != "" {
		return exec.LookPath(override)
	}
	home, err := managedHome()
	if err == nil {
		p := venvBin(home, "kaggle")
		if _, err = os.Stat(p); err == nil {
			return p, nil
		}
	}
	p, err := exec.LookPath("kaggle")
	if err != nil {
		return "", errors.New("Kaggle client not found; run rooftam setup, then rooftam auth")
	}
	return p, nil
}
func (a *app) setup(ctx context.Context, python string) error {
	home, err := managedHome()
	if err != nil {
		return err
	}
	if err = os.MkdirAll(home, 0700); err != nil {
		return err
	}
	for _, cmd := range []struct {
		name string
		args []string
	}{{python, []string{"-m", "venv", home}}, {venvBin(home, "python"), []string{"-m", "pip", "install", "kaggle==" + kaggleVersion}}} {
		text, err := a.command(ctx, cmd.name, cmd.args, false)
		if err != nil {
			return fmt.Errorf("setup requires Python 3.11+, venv and Internet: %w\n%s", err, text)
		}
	}
	fmt.Fprintf(a.out, "Installed official Kaggle %s in %s\nNext: rooftam auth\n", kaggleVersion, home)
	return nil
}
func (a *app) doctor(ctx context.Context, k string) error {
	for _, args := range [][]string{{"--version"}, {"kernels", "push", "--help"}, {"kernels", "output", "--help"}, {"kernels", "list", "--mine", "--page-size", "1"}} {
		text, err := a.command(ctx, k, args, false)
		if err != nil {
			return fmt.Errorf("Kaggle preflight failed; run rooftam auth or check network/account access: %w\n%s", err, text)
		}
		if len(args) > 1 && args[1] == "push" && (!strings.Contains(text, "--accelerator") || !strings.Contains(text, "--timeout")) {
			return errors.New("Kaggle client lacks required submission flags; run rooftam setup")
		}
		if len(args) > 1 && args[1] == "output" && !strings.Contains(text, "--file-pattern") {
			return errors.New("Kaggle client lacks filtered output download; run rooftam setup")
		}
	}
	fmt.Fprintln(a.out, "Kaggle client and authenticated account read passed. TPU allocation is verified only inside the run.")
	return nil
}

type tailBuffer struct{ b []byte }

func (t *tailBuffer) Write(p []byte) (int, error) {
	n := len(p)
	t.b = append(t.b, p...)
	if len(t.b) > maxCapture {
		t.b = t.b[len(t.b)-maxCapture:]
	}
	return n, nil
}
func redact(s string) string {
	for _, key := range []string{"KAGGLE_API_TOKEN", "KAGGLE_KEY", "GITHUB_TOKEN", "GH_TOKEN"} {
		if v := os.Getenv(key); len(v) >= 4 {
			s = strings.ReplaceAll(s, v, "[REDACTED]")
		}
	}
	return s
}
func (a *app) command(ctx context.Context, name string, args []string, interactive bool) (string, error) {
	if a.execFn != nil {
		return a.execFn(ctx, name, args, interactive)
	}
	limit := 2 * time.Minute
	if interactive {
		limit = 15 * time.Minute
	}
	if len(args) > 1 && (args[1] == "venv" || strings.Contains(strings.Join(args, " "), "pip install")) {
		limit = 15 * time.Minute
	}
	if len(args) > 1 && args[1] == "output" {
		limit = 15 * time.Minute
	}
	child, cancel := context.WithTimeout(ctx, limit)
	defer cancel()
	cmd := exec.CommandContext(child, name, args...)
	if interactive {
		cmd.Stdin = os.Stdin
		cmd.Stdout = a.out
		cmd.Stderr = a.errOut
		return "", cmd.Run()
	}
	var buf tailBuffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	err := cmd.Run()
	text := redact(string(bytes.ToValidUTF8(buf.b, []byte("?"))))
	if err != nil {
		return text, fmt.Errorf("%s %s: %w", filepath.Base(name), strings.Join(args, " "), err)
	}
	return text, nil
}
