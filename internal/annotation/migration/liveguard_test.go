package migration

import (
	"strings"
	"testing"
)

func TestGuardLiveWriteFailsClosedForEveryNonLoopbackSpelling(t *testing.T) {
	t.Setenv(AllowLiveEnv, "")
	live := []string{
		"https://fieldwork-annotations.ben-ebsworth.workers.dev",
		"https://fieldwork-annotations.ben-ebsworth.workers.dev:443",
		"https://FIELDWORK-ANNOTATIONS.ben-ebsworth.workers.dev",
		"https://fieldwork-annotations.ben-ebsworth.workers.dev/",
		"https://fieldwork-annotations.ben-ebsworth.workers.dev:8443",
		"https://localhost.example.com",
		"https://127.0.0.1.nip.io",
		"not a url",
	}
	for _, origin := range live {
		if err := GuardLiveWrite(origin, "annotate push", false); err == nil || !strings.Contains(err.Error(), "--allow-live") || !strings.Contains(err.Error(), AllowLiveEnv) {
			t.Fatalf("%s was not refused: %v", origin, err)
		}
		if err := GuardLiveWrite(origin, "annotate push", true); err != nil {
			t.Fatalf("--allow-live did not pass %s: %v", origin, err)
		}
	}
	for _, origin := range []string{"http://127.0.0.1:18131", "http://localhost:8080", "http://[::1]:18131", "https://127.0.0.1", "http://127.1.2.3:9"} {
		if err := GuardLiveWrite(origin, "annotate push", false); err != nil {
			t.Fatalf("loopback %s refused: %v", origin, err)
		}
	}
	t.Setenv(AllowLiveEnv, "1")
	if err := GuardLiveWrite(live[0], "annotate push", false); err != nil {
		t.Fatalf("%s=1 did not pass: %v", AllowLiveEnv, err)
	}
	t.Setenv(AllowLiveEnv, "true")
	if err := GuardLiveWrite(live[0], "annotate push", false); err == nil {
		t.Fatal("only the exact value 1 enables live writes")
	}
}
