package main

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func testConfig() config {
	c := config{RunID: "20261114-100000-aabbccddeeff", Kernel: "tester/addr-rooftam-20261114-100000-aabbccddeeff", Commit: strings.Repeat("a", 40), Accelerator: "TpuV5E8", Datasets: []string{}, RealSteps: 100, BatchSize: 1, Seed: 17, MaxSeconds: 7200}
	c.Hash = configHash(c)
	return c
}
func setupRun(t *testing.T) (string, state) {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "run")
	s, err := prepare(dir, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	return dir, s
}
func writeTestZip(t *testing.T, file string, files map[string][]byte, withReceipt bool) {
	t.Helper()
	if withReceipt {
		var r receipt
		if b := files["receipt.json"]; b != nil {
			if err := json.Unmarshal(b, &r); err != nil {
				t.Fatal(err)
			}
		}
		r.Artifacts = map[string]string{}
		for n, b := range files {
			if n != "receipt.json" {
				r.Artifacts[n] = digest(b)
			}
		}
		b, _ := json.Marshal(r)
		files["receipt.json"] = b
	}
	if err := os.MkdirAll(filepath.Dir(file), 0700); err != nil {
		t.Fatal(err)
	}
	f, err := os.Create(file)
	if err != nil {
		t.Fatal(err)
	}
	z := zip.NewWriter(f)
	for n, b := range files {
		w, err := z.Create(n)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = w.Write(b); err != nil {
			t.Fatal(err)
		}
	}
	if err = z.Close(); err != nil {
		t.Fatal(err)
	}
	if err = f.Close(); err != nil {
		t.Fatal(err)
	}
}
func validEvidence(c config) map[string][]byte {
	docs := map[string][]byte{}
	put := func(n string, v any) { b, _ := json.Marshal(v); docs[n] = b }
	xla := map[string]any{"aten_counters": map[string]int{}, "requires_fallback_review": false}
	env := map[string]any{"device": "xla:0", "xla_device_type": "TPU", "addr_revision": c.Commit}
	put("01-preflight/preflight.json", map[string]any{"status": "passed", "environment": env, "xla": xla})
	r := receipt{Schema: 1, RunID: c.RunID, Commit: c.Commit, ConfigHash: c.Hash, Kernel: c.Kernel, Status: "passed"}
	names := []string{"bootstrap", "tests", "01-preflight", "02-validation", "03-tiny-control", "04-edgetam-before", "05-edgetam-smoke", "06-edgetam-after", "07-smoke-comparison"}
	if c.Manifest != "" {
		names = append(names, "10-real-validation", "11-real-before", "12-real-train", "13-real-after", "15-real-comparison")
	}
	if c.SAM3 != "" {
		names = append(names, "14-sam3")
	}
	for _, name := range names {
		r.Stages = append(r.Stages, struct {
			Name string `json:"name"`
			Code int    `json:"exit_code"`
		}{name, 0})
	}
	for _, g := range []struct {
		train, before, after, model string
		synthetic                   bool
		steps                       int
	}{{"03-tiny-control", "", "", "tiny", true, 3}, {"05-edgetam-smoke", "04-edgetam-before", "06-edgetam-after", "edgetam", true, 3}, {"12-real-train", "11-real-before", "13-real-after", "edgetam", false, c.RealSteps}} {
		if !g.synthetic && c.Manifest == "" {
			continue
		}
		adapter := []byte("test-double weights " + g.train)
		docs[g.train+"/adapter.pt"] = adapter
		e := map[string]any{"status": "passed", "synthetic": g.synthetic, "weights_changed": true, "steps": g.steps, "final_loss": 0.8, "adapter_sha256": digest(adapter), "dataset_sha256": strings.Repeat("d", 64), "environment": env, "xla": xla, "configuration": map[string]string{"model": g.model, "device": "tpu"}}
		put(g.train+"/run.json", e)
		for _, stage := range []string{g.before, g.after} {
			if stage == "" {
				continue
			}
			ev := map[string]any{"status": "passed", "synthetic": g.synthetic, "split": "val", "summary": map[string]any{"n": 2, "iou": 0.8}, "dataset_sha256": strings.Repeat("d", 64), "adapter_sha256": digest(adapter), "environment": env, "xla": xla, "configuration": map[string]string{"model": g.model, "device": "tpu"}}
			put(stage+"/evaluation.json", ev)
		}
	}
	put("07-smoke-comparison/comparison.json", map[string]any{"synthetic": true})
	if c.Manifest != "" {
		put("15-real-comparison/comparison.json", map[string]any{"synthetic": false})
	}
	if c.SAM3 != "" {
		put("14-sam3/evaluation.json", map[string]string{"status": "passed"})
	}
	put("receipt.json", r)
	return docs
}
func alterJSON(t *testing.T, docs map[string][]byte, name string, change func(map[string]any)) {
	t.Helper()
	var v map[string]any
	if err := json.Unmarshal(docs[name], &v); err != nil {
		t.Fatal(err)
	}
	change(v)
	docs[name], _ = json.Marshal(v)
}
func TestPreparePrivatePinnedAndImmutable(t *testing.T) {
	dir, s := setupRun(t)
	got, err := loadState(dir)
	if err != nil || got.Config.Hash != s.Config.Hash {
		t.Fatalf("state: %v", err)
	}
	var m map[string]any
	b, _ := os.ReadFile(filepath.Join(dir, "upload/kernel-metadata.json"))
	json.Unmarshal(b, &m)
	if m["is_private"] != true || m["enable_tpu"] != true || m["enable_gpu"] != false {
		t.Fatal(m)
	}
	if m["id"] != s.Config.Kernel || m["title"] != "Addr RoofTAM "+s.Config.RunID {
		t.Fatal(m)
	}
	if err = verifyUpload(dir, &s); err != nil {
		t.Fatal(err)
	}
	if _, err = prepare(dir, s.Config); err == nil {
		t.Fatal("overwrote existing run")
	}
	os.WriteFile(filepath.Join(dir, "upload/kernel-metadata.json"), []byte(`{"is_private":false}`), 0600)
	if verifyUpload(dir, &s) == nil {
		t.Fatal("accepted modified upload")
	}
}
func TestOptions(t *testing.T) {
	tests := []struct {
		name   string
		change func(*config)
		owner  string
		allow  bool
	}{
		{"missing owner", func(c *config) {}, "", false},
		{"owner injection", func(c *config) {}, "bad;echo", false},
		{"GPU", func(c *config) { c.Accelerator = "NvidiaTeslaT4" }, "tester", false},
		{"unbounded", func(c *config) { c.MaxSeconds = 0 }, "tester", false},
		{"negative steps", func(c *config) { c.RealSteps = -1 }, "tester", false},
		{"negative batch", func(c *config) { c.BatchSize = 0 }, "tester", false},
		{"bad seed", func(c *config) { c.Seed = -1 }, "tester", false},
		{"dataset path", func(c *config) { c.Datasets = []string{"../data"} }, "tester", false},
		{"missing data consent", func(c *config) {
			c.Manifest = "/kaggle/input/roofs/manifest.jsonl"
			c.Datasets = []string{"owner/roofs"}
		}, "tester", false},
		{"missing data attachment", func(c *config) { c.Manifest = "/kaggle/input/roofs/manifest.jsonl" }, "tester", true},
		{"traversal", func(c *config) { c.Manifest = "/kaggle/input/../../secret"; c.Datasets = []string{"owner/roofs"} }, "tester", true},
		{"arbitrary local file", func(c *config) { c.Manifest = "/home/user/file" }, "tester", true},
		{"baseline without data", func(c *config) { c.SAM3 = "/kaggle/input/roofs/predictions.json" }, "tester", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			c := testConfig()
			tc.change(&c)
			if validateOptions(tc.owner, c, tc.allow) == nil {
				t.Fatal("accepted invalid options")
			}
		})
	}
	c := testConfig()
	if err := validateOptions("tester", c, false); err != nil {
		t.Fatal(err)
	}
	c.Manifest = "/kaggle/input/roofs/manifest.jsonl"
	c.Datasets = []string{"tester/roofs"}
	if err := validateOptions("tester", c, true); err != nil {
		t.Fatal(err)
	}
}
func TestParseStatus(t *testing.T) {
	for _, s := range []string{"queued", "running", "complete", "error", "cancelled", "KernelWorkerStatus.COMPLETE"} {
		got, err := parseStatus(`tester/roof has status "`+s+`"`, "tester/roof")
		if err != nil || got == "" {
			t.Fatalf("%s: %v", s, err)
		}
	}
	for _, s := range []string{`other/roof has status "complete"`, `tester/roof has status "something-new"`, `Failure message: "complete"`, `not complete`} {
		if _, err := parseStatus(s, "tester/roof"); err == nil {
			t.Fatal("guessed success", s)
		}
	}
}
func TestSubmitOnceAndUnknownResponse(t *testing.T) {
	for _, mode := range []string{"success", "transport-error", "printed-error", "wrong-kernel", "invalid-dataset"} {
		t.Run(mode, func(t *testing.T) {
			dir, s := setupRun(t)
			calls := 0
			a := app{out: io.Discard, errOut: io.Discard, execFn: func(ctx context.Context, name string, args []string, interactive bool) (string, error) {
				calls++
				if args[1] != "push" {
					t.Fatal(args)
				}
				switch mode {
				case "transport-error":
					return "", errors.New("network lost")
				case "printed-error":
					return "Kernel push error: rejected", nil
				case "wrong-kernel":
					return "Kernel version 1 successfully pushed. https://www.kaggle.com/code/somebody/other", nil
				case "invalid-dataset":
					return "The following are not valid dataset sources\nKernel version 1 successfully pushed. https://www.kaggle.com/code/" + s.Config.Kernel, nil
				}
				return "Kernel version 1 successfully pushed. https://www.kaggle.com/code/" + s.Config.Kernel, nil
			}}
			err := a.submit(context.Background(), "kaggle", dir, &s)
			if (err == nil) != (mode == "success") {
				t.Fatalf("unexpected %v", err)
			}
			if err = a.submit(context.Background(), "kaggle", dir, &s); err == nil {
				t.Fatal("duplicate push allowed")
			}
			if calls != 1 {
				t.Fatal(calls)
			}
			restored, err := loadState(dir)
			if err != nil {
				t.Fatal(err)
			}
			if mode != "success" && restored.Phase != "submission-unknown" {
				t.Fatal(restored.Phase)
			}
		})
	}
}
func TestLockAndAlteredState(t *testing.T) {
	dir, s := setupRun(t)
	unlock, err := lock(dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = lock(dir); err == nil {
		t.Fatal("double lock")
	}
	unlock()
	s.Config.Commit = strings.Repeat("b", 40)
	saveState(dir, &s)
	if _, err = loadState(dir); err == nil {
		t.Fatal("accepted altered config")
	}
}
func TestCancellationDoesNotResubmit(t *testing.T) {
	dir, s := setupRun(t)
	s.Phase = "submitted"
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	calls := 0
	a := app{out: io.Discard, errOut: io.Discard, execFn: func(ctx context.Context, name string, args []string, interactive bool) (string, error) {
		calls++
		if args[1] != "status" {
			t.Fatal("unexpected mutation", args)
		}
		return "", ctx.Err()
	}}
	err := a.finish(ctx, "kaggle", dir, &s, time.Second, time.Second)
	if err == nil || !strings.Contains(err.Error(), "NOT cancelled") {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatal(calls)
	}
}
func TestUnknownSubmissionResumeOnlyReads(t *testing.T) {
	dir, s := setupRun(t)
	s.Phase = "submission-unknown"
	calls := 0
	a := app{out: io.Discard, errOut: io.Discard, execFn: func(ctx context.Context, name string, args []string, interactive bool) (string, error) {
		calls++
		if args[1] != "status" {
			t.Fatal("resubmitted", args)
		}
		return s.Config.Kernel + ` has status "complete"`, nil
	}}
	if err := a.wait(context.Background(), "kaggle", dir, &s, time.Second, time.Second); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatal(calls)
	}
}
func TestVerification(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*testing.T, map[string][]byte)
		want   string
	}{
		{"valid", func(t *testing.T, d map[string][]byte) {}, "verified"},
		{"wrong run", func(t *testing.T, d map[string][]byte) {
			alterJSON(t, d, "receipt.json", func(v map[string]any) { v["run_id"] = "wrong" })
		}, "failed"},
		{"failed orchestration", func(t *testing.T, d map[string][]byte) {
			alterJSON(t, d, "receipt.json", func(v map[string]any) { v["status"] = "failed" })
		}, "failed"},
		{"missing XLA", func(t *testing.T, d map[string][]byte) {
			alterJSON(t, d, "05-edgetam-smoke/run.json", func(v map[string]any) { delete(v, "xla") })
		}, "failed"},
		{"missing report", func(t *testing.T, d map[string][]byte) { delete(d, "05-edgetam-smoke/run.json") }, "failed"},
		{"no weight update", func(t *testing.T, d map[string][]byte) {
			alterJSON(t, d, "05-edgetam-smoke/run.json", func(v map[string]any) { v["weights_changed"] = false })
		}, "failed"},
		{"CPU masquerade", func(t *testing.T, d map[string][]byte) {
			alterJSON(t, d, "05-edgetam-smoke/run.json", func(v map[string]any) { v["environment"].(map[string]any)["device"] = "cpu" })
		}, "failed"},
		{"wrong revision", func(t *testing.T, d map[string][]byte) {
			alterJSON(t, d, "01-preflight/preflight.json", func(v map[string]any) { v["environment"].(map[string]any)["addr_revision"] = "other" })
		}, "failed"},
		{"stale adapter", func(t *testing.T, d map[string][]byte) {
			alterJSON(t, d, "06-edgetam-after/evaluation.json", func(v map[string]any) { v["adapter_sha256"] = "other" })
		}, "failed"},
		{"wrong split", func(t *testing.T, d map[string][]byte) {
			alterJSON(t, d, "06-edgetam-after/evaluation.json", func(v map[string]any) { v["split"] = "test" })
		}, "failed"},
		{"wrong dataset", func(t *testing.T, d map[string][]byte) {
			alterJSON(t, d, "06-edgetam-after/evaluation.json", func(v map[string]any) { v["dataset_sha256"] = "other" })
		}, "failed"},
		{"fallback counters", func(t *testing.T, d map[string][]byte) {
			alterJSON(t, d, "05-edgetam-smoke/run.json", func(v map[string]any) { v["xla"] = map[string]any{"aten_counters": map[string]int{"aten::op": 2}} })
		}, "review"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dir, s := setupRun(t)
			docs := validEvidence(s.Config)
			tc.mutate(t, docs)
			writeTestZip(t, filepath.Join(dir, "downloads", "rooftam-"+s.Config.RunID+".zip"), docs, true)
			a := app{out: io.Discard, errOut: io.Discard}
			err := a.verify(dir, &s)
			if (err == nil) != (tc.want == "verified") {
				t.Fatal(err)
			}
			var v verdict
			b, _ := os.ReadFile(filepath.Join(dir, "verification.json"))
			json.Unmarshal(b, &v)
			if v.Status != tc.want {
				t.Fatalf("want %s got %+v", tc.want, v)
			}
			if tc.want == "review" {
				var review *reviewError
				if !errors.As(err, &review) {
					t.Fatal("review exit type", err)
				}
			}
		})
	}
}
func TestArchiveRejectsTraversalAndHashMismatch(t *testing.T) {
	for _, name := range []string{"../escape", "/absolute", "a\\b", "a/../b", "C:drive", "receipt.json"} {
		t.Run(name, func(t *testing.T) {
			file := filepath.Join(t.TempDir(), "test.zip")
			docs := map[string][]byte{name: []byte(`{"artifacts":{}}`), "normal.txt": []byte("x")}
			writeTestZip(t, file, docs, false)
			if _, _, err := readArchive(file); err == nil {
				t.Fatal("accepted bad archive")
			}
		})
	}
	c := testConfig()
	docs := validEvidence(c)
	file := filepath.Join(t.TempDir(), "test.zip")
	writeTestZip(t, file, docs, true)
	docs["05-edgetam-smoke/adapter.pt"] = []byte("tampered")
	writeTestZip(t, file, docs, false)
	if _, _, err := readArchive(file); err == nil || !strings.Contains(err.Error(), "hash mismatch") {
		t.Fatal(err)
	}
}
func TestRealDataAndSAM3Evidence(t *testing.T) {
	c := testConfig()
	c.Manifest = "/kaggle/input/roofs/manifest.jsonl"
	c.Datasets = []string{"tester/roofs"}
	c.SAM3 = "/kaggle/input/baseline/predictions.json"
	c.Hash = configHash(c)
	file := filepath.Join(t.TempDir(), "test.zip")
	writeTestZip(t, file, validEvidence(c), true)
	docs, hashes, err := readArchive(file)
	if err != nil {
		t.Fatal(err)
	}
	v, err := verifyEvidence(docs, hashes, c)
	if err != nil || v.SyntheticOnly {
		t.Fatal(v, err)
	}
}
func TestCompleteLifecycleWithFakeKaggle(t *testing.T) {
	// Contract integration only: no Kaggle service or TPU is called here.
	dir, s := setupRun(t)
	var calls []string
	var log bytes.Buffer
	a := app{out: &log, errOut: &log}
	a.execFn = func(ctx context.Context, name string, args []string, interactive bool) (string, error) {
		calls = append(calls, strings.Join(args, " "))
		switch args[1] {
		case "push":
			return "Kernel version 1 successfully pushed. https://www.kaggle.com/code/" + s.Config.Kernel, nil
		case "status":
			return s.Config.Kernel + ` has status "KernelWorkerStatus.COMPLETE"`, nil
		case "output":
			if !strings.Contains(strings.Join(args, " "), "--file-pattern") {
				t.Fatal("unfiltered download")
			}
			writeTestZip(t, filepath.Join(dir, "downloads", "rooftam-"+s.Config.RunID+".zip"), validEvidence(s.Config), true)
			return "downloaded", nil
		}
		return "", fmt.Errorf("unexpected %v", args)
	}
	if err := a.submit(context.Background(), "kaggle", dir, &s); err != nil {
		t.Fatal(err)
	}
	if err := a.finish(context.Background(), "kaggle", dir, &s, time.Second, time.Second); err != nil {
		t.Fatal(err)
	}
	if s.Phase != "verified" || len(calls) != 3 {
		t.Fatal(s.Phase, calls)
	}
}
func TestNotebookPythonSyntaxAndRunnerFailurePackaging(t *testing.T) {
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("Python unavailable")
	}
	dir, _ := setupRun(t)
	// Execute actual embedded orchestration with only process/filesystem stubs.
	script := `import ast, json, pathlib, tempfile, unittest.mock as m, sys, zipfile
nb=json.loads(pathlib.Path(sys.argv[1]).read_text())
for cell in nb['cells']:
 if cell['cell_type']=='code': ast.parse(cell['source'])
ns={};exec(nb['cells'][1]['source'],ns)
source=nb['cells'][2]['source']
root=pathlib.Path(tempfile.mkdtemp());out=root/'outputs';out.mkdir();work=root/'work';work.mkdir()
real_path=pathlib.Path
def fake_path(p): return out if str(p)=='/kaggle/working' else real_path(p)
with m.patch('tempfile.mkdtemp',return_value=str(work)), m.patch('pathlib.Path',side_effect=fake_path), m.patch('subprocess.Popen',side_effect=OSError('injected network failure')):
 try: exec(source,ns)
 except OSError: pass
 else: raise AssertionError('expected failure')
archive=next(out.glob('*.zip'))
with zipfile.ZipFile(archive) as z:
 receipt=json.loads(z.read('receipt.json'))
 assert receipt['status']=='failed'
 assert receipt['run_id']==ns['CONFIG']['run_id']
 assert 'orchestrator-failure.txt' in z.namelist()
print('Notebook syntax and actual runner failure packaging passed')
`
	cmd := exec.Command(python, "-c", script, filepath.Join(dir, "upload/rooftam.ipynb"))
	b, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("%v\n%s", err, b)
	}
}
func TestRedaction(t *testing.T) {
	t.Setenv("KAGGLE_API_TOKEN", "secret-test-token")
	got := redact("token=secret-test-token")
	if strings.Contains(got, "secret-test-token") || !strings.Contains(got, "REDACTED") {
		t.Fatal(got)
	}
}
func TestHelpAndInvalidCLI(t *testing.T) {
	var b bytes.Buffer
	a := app{out: &b, errOut: &b}
	if err := a.run(context.Background(), []string{"help"}); err != nil || !strings.Contains(b.String(), "resume") {
		t.Fatal(err)
	}
	if err := a.run(context.Background(), []string{"unknown"}); err == nil {
		t.Fatal("unknown accepted")
	}
	if err := a.run(context.Background(), []string{"prepare", "--owner", "tester", "--poll", "0s"}); err == nil {
		t.Fatal("zero poll accepted")
	}
}

func TestRunCommandEndToEnd(t *testing.T) {
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(t.TempDir(), "new-run")
	var log bytes.Buffer
	a := app{out: &log, errOut: &log, resolveFn: func(ctx context.Context, ref string) (string, error) {
		if ref != "main" {
			t.Fatal(ref)
		}
		return strings.Repeat("a", 40), nil
	}}
	a.execFn = func(ctx context.Context, name string, args []string, interactive bool) (string, error) {
		if args[0] == "--version" {
			return "Kaggle API 2.2.4", nil
		}
		if args[len(args)-1] == "--help" {
			return "--accelerator --timeout --file-pattern", nil
		}
		if args[1] == "list" {
			return "authenticated", nil
		}
		s, err := loadState(dir)
		if err != nil {
			t.Fatal(err)
		}
		switch args[1] {
		case "push":
			return "Kernel version 1 successfully pushed. https://www.kaggle.com/code/" + s.Config.Kernel, nil
		case "status":
			return s.Config.Kernel + ` has status "complete"`, nil
		case "output":
			writeTestZip(t, filepath.Join(dir, "downloads", "rooftam-"+s.Config.RunID+".zip"), validEvidence(s.Config), true)
			return "downloaded", nil
		}
		return "", fmt.Errorf("unexpected command: %v", args)
	}
	if err = a.run(context.Background(), []string{"run", "--owner", "tester", "--output", dir, "--kaggle", executable}); err != nil {
		t.Fatal(err, log.String())
	}
	s, err := loadState(dir)
	if err != nil || s.Phase != "verified" {
		t.Fatal(s, err)
	}
	if !strings.Contains(log.String(), "Synthetic compatibility") {
		t.Fatal(log.String())
	}
}
func TestSetupAndAuthUseOfficialClientWithoutSerializingCredentials(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_CONFIG_HOME", home)
	t.Setenv("APPDATA", home)
	t.Setenv("KAGGLE_API_TOKEN", "secret-test-token")
	var commands []string
	var interactiveCalls int
	a := app{out: io.Discard, errOut: io.Discard, execFn: func(ctx context.Context, name string, args []string, interactive bool) (string, error) {
		commands = append(commands, strings.Join(args, " "))
		if interactive {
			interactiveCalls++
		}
		return "", nil
	}}
	if err := a.run(context.Background(), []string{"setup", "--python", "python3"}); err != nil {
		t.Fatal(err)
	}
	if len(commands) != 5 || !strings.Contains(commands[1], "kaggle=="+kaggleVersion) || !strings.Contains(commands[1], "six==1.17.0") {
		t.Fatal(commands)
	}
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	if err = a.run(context.Background(), []string{"auth", "--no-launch-browser", "--kaggle", executable}); err != nil {
		t.Fatal(err)
	}
	if interactiveCalls != 1 || commands[5] != "auth login --no-launch-browser" {
		t.Fatal(commands)
	}
	for _, command := range commands {
		if strings.Contains(command, "secret-test-token") {
			t.Fatal("credential serialized")
		}
	}
}
