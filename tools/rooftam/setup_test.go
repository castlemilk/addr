package main

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func isolatedSetupHome(t *testing.T) string {
	t.Helper()
	root := filepath.Join(t.TempDir(), "config with spaces")
	t.Setenv("HOME", root)
	t.Setenv("XDG_CONFIG_HOME", root)
	t.Setenv("APPDATA", root)
	home, err := managedHome()
	if err != nil {
		t.Fatal(err)
	}
	return home
}

func TestSetupPinsRuntimeAndChecksExecutableBeforeSuccess(t *testing.T) {
	home := isolatedSetupHome(t)
	type invocation struct {
		name string
		args []string
	}
	var calls []invocation
	var output bytes.Buffer
	a := app{out: &output, errOut: io.Discard, execFn: func(_ context.Context, name string, args []string, interactive bool) (string, error) {
		if interactive {
			t.Fatal("setup must not start sign-in")
		}
		if strings.Contains(output.String(), "startup-checked") {
			t.Fatal("reported success before the last startup check")
		}
		calls = append(calls, invocation{name, append([]string(nil), args...)})
		return "", nil
	}}
	if err := a.run(context.Background(), []string{"setup", "--python", "/tmp/python with spaces"}); err != nil {
		t.Fatal(err)
	}
	py, k := venvBin(home, "python"), venvBin(home, "kaggle")
	want := []invocation{
		{"/tmp/python with spaces", []string{"-m", "venv", home}},
		{py, []string{"-m", "pip", "install", "kaggle==" + kaggleVersion, "six==1.17.0"}},
		{py, []string{"-m", "pip", "check"}},
		{k, []string{"--version"}},
		{k, []string{"auth", "login", "--help"}},
	}
	if !reflect.DeepEqual(calls, want) {
		t.Fatalf("commands = %#v; want %#v", calls, want)
	}
	if !strings.Contains(output.String(), "Installed and startup-checked") {
		t.Fatal(output.String())
	}
}

func TestSetupStopsAtEachFailureWithoutReportingSuccess(t *testing.T) {
	stages := []string{"create Python environment", "install Kaggle runtime", "check declared dependencies", "start Kaggle client", "check OAuth command startup"}
	for failed, stage := range stages {
		t.Run(stage, func(t *testing.T) {
			isolatedSetupHome(t)
			var output bytes.Buffer
			calls := 0
			a := app{out: &output, errOut: io.Discard, execFn: func(_ context.Context, _ string, _ []string, interactive bool) (string, error) {
				if interactive {
					t.Fatal("setup attempted sign-in")
				}
				calls++
				if calls == failed+1 {
					return "ModuleNotFoundError: No module named 'six'", errors.New("test failure")
				}
				return "", nil
			}}
			err := a.run(context.Background(), []string{"setup"})
			if err == nil || !strings.Contains(err.Error(), stage) || !strings.Contains(err.Error(), "No module named 'six'") {
				t.Fatalf("wrong failure: %v", err)
			}
			if calls != failed+1 || strings.Contains(output.String(), "Installed") || strings.Contains(output.String(), "Next:") {
				t.Fatalf("calls=%d, premature success=%q", calls, output.String())
			}
		})
	}
}

func TestSetupReinstallsMissingDependencyInExistingEnvironment(t *testing.T) {
	home := isolatedSetupHome(t)
	if err := os.MkdirAll(home, 0700); err != nil {
		t.Fatal(err)
	}
	sentinel := filepath.Join(home, "preserve-existing-file")
	if err := os.WriteFile(sentinel, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	installs := 0
	a := app{out: io.Discard, errOut: io.Discard, execFn: func(_ context.Context, _ string, args []string, _ bool) (string, error) {
		if len(args) > 2 && args[0] == "-m" && args[1] == "pip" && args[2] == "install" {
			if !strings.Contains(strings.Join(args, " "), "six==1.17.0") {
				t.Fatal("repair omitted six")
			}
			installs++
		}
		return "", nil
	}}
	for i := 0; i < 2; i++ {
		if err := a.run(context.Background(), []string{"setup"}); err != nil {
			t.Fatal(err)
		}
	}
	b, err := os.ReadFile(sentinel)
	if err != nil || string(b) != "keep" || installs != 2 {
		t.Fatalf("setup did not preserve and repair existing environment: installs=%d content=%s error=%v", installs, b, err)
	}
}

func TestDoctorSeparatesStartupFailureFromAccountFailure(t *testing.T) {
	for _, failing := range []string{"--version", "kernels push --help", "kernels list --mine --page-size 1"} {
		t.Run(failing, func(t *testing.T) {
			a := app{out: io.Discard, errOut: io.Discard, execFn: func(_ context.Context, _ string, args []string, _ bool) (string, error) {
				if strings.Join(args, " ") == failing {
					return "diagnostic details", errors.New("test failure")
				}
				return "--accelerator --timeout --file-pattern", nil
			}}
			err := a.doctor(context.Background(), "fake-kaggle")
			want := "client startup failed"
			if strings.Contains(failing, "list --mine") {
				want = "account preflight failed"
			}
			if err == nil || !strings.Contains(err.Error(), want) {
				t.Fatalf("expected %q: %v", want, err)
			}
		})
	}
}
