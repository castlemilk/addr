package main

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

var pushSuccess = regexp.MustCompile(`(?i)Kernel version (?:([0-9]+) )?successfully pushed`)

func (a *app) submit(ctx context.Context, k, dir string, s *state) error {
	if s.Phase != "prepared" {
		return fmt.Errorf("run is %s; refusing duplicate submission; use resume --run %s", s.Phase, dir)
	}
	if err := verifyUpload(dir, s); err != nil {
		return err
	}
	s.Phase = "submitting"
	if err := saveState(dir, s); err != nil {
		return err
	}
	// Never retry a write: the server may accept it before the connection fails.
	text, callErr := a.command(ctx, k, []string{"kernels", "push", "-p", filepath.Join(dir, "upload"), "--accelerator", s.Config.Accelerator, "--timeout", strconv.Itoa(s.Config.MaxSeconds)}, false)
	logErr := atomicWrite(filepath.Join(dir, "submission.log"), []byte(redact(text)))
	match := pushSuccess.FindStringSubmatch(text)
	lower := strings.ToLower(text)
	accepted := callErr == nil && len(match) > 0 && strings.Contains(text, "https://www.kaggle.com/code/"+s.Config.Kernel) && !strings.Contains(lower, "push error") && !strings.Contains(lower, "not valid") && !strings.Contains(lower, "invalid")
	if !accepted {
		s.Phase = "submission-unknown"
		saveErr := saveState(dir, s)
		return errors.Join(fmt.Errorf("submission not confirmed; DO NOT retry push. Inspect submission.log and use resume --run %s to reconcile the unique notebook", dir), callErr, logErr, saveErr)
	}
	s.Phase = "submitted"
	s.Version = match[1]
	saveErr := saveState(dir, s)
	fmt.Fprintf(a.out, "Submitted private notebook: https://www.kaggle.com/code/%s\n", s.Config.Kernel)
	return errors.Join(logErr, saveErr)
}

func parseStatus(text, kernel string) (string, error) {
	re := regexp.MustCompile(`(?m)^` + regexp.QuoteMeta(kernel) + ` has status "([A-Za-z0-9_.]+)"\s*$`)
	m := re.FindStringSubmatch(text)
	if len(m) != 2 {
		return "", errors.New("unrecognized Kaggle status response; refusing to guess success")
	}
	parts := strings.Split(strings.ToLower(m[1]), ".")
	status := parts[len(parts)-1]
	switch status {
	case "queued", "running", "pending", "initializing", "complete", "error", "failed", "cancelled", "canceled":
		return status, nil
	}
	return "", fmt.Errorf("unrecognized Kaggle status %q", m[1])
}
func (a *app) status(ctx context.Context, k, kernel string) (string, error) {
	text, err := a.command(ctx, k, []string{"kernels", "status", kernel}, false)
	if err != nil {
		return "", fmt.Errorf("status unavailable: %w\n%s", err, text)
	}
	return parseStatus(text, kernel)
}
func (a *app) wait(ctx context.Context, k, dir string, s *state, limit, poll time.Duration) error {
	if s.Phase == "prepared" {
		return errors.New("run has not been submitted; use submit, not resume")
	}
	ctx, cancel := context.WithTimeout(ctx, limit)
	defer cancel()
	failures := 0
	for {
		status, err := a.status(ctx, k, s.Config.Kernel)
		if ctx.Err() != nil {
			return fmt.Errorf("waiting stopped (%v); remote run is NOT cancelled. Resume with: rooftam resume --run %s", ctx.Err(), dir)
		}
		if err != nil {
			failures++
			if failures >= 3 {
				return fmt.Errorf("status failed three times; remote run may continue; use resume: %w", err)
			}
			fmt.Fprintln(a.errOut, "Status unavailable; retrying read only.")
		} else {
			failures = 0
			s.Phase = status
			if err = saveState(dir, s); err != nil {
				return err
			}
			fmt.Fprintf(a.out, "%s: %s\n", s.Config.Kernel, status)
			switch status {
			case "complete":
				return nil
			case "error", "failed", "cancelled", "canceled":
				return fmt.Errorf("Kaggle run ended with %s; inspect downloaded logs/receipt", status)
			}
		}
		timer := time.NewTimer(poll)
		select {
		case <-ctx.Done():
			timer.Stop()
			return fmt.Errorf("waiting stopped; remote run is NOT cancelled. Resume with: rooftam resume --run %s", dir)
		case <-timer.C:
		}
	}
}
func (a *app) download(ctx context.Context, k, dir string, s *state) error {
	if s.Phase == "prepared" {
		return errors.New("cannot download an unsubmitted run")
	}
	dest := filepath.Join(dir, "downloads")
	if err := os.Mkdir(dest, 0700); err != nil && !os.IsExist(err) {
		return err
	}
	info, err := os.Lstat(dest)
	if err != nil {
		return err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return errors.New("downloads path must be a real directory")
	}
	pattern := `(^|/)rooftam-` + regexp.QuoteMeta(s.Config.RunID) + `\.zip$`
	text, callErr := a.command(ctx, k, []string{"kernels", "output", s.Config.Kernel, "-p", dest, "--force", "--file-pattern", pattern}, false)
	logErr := atomicWrite(filepath.Join(dir, "download.log"), []byte(redact(text)))
	if callErr != nil {
		return errors.Join(fmt.Errorf("artifact download failed: %w", callErr), logErr)
	}
	if _, err = archivePath(dir, s); err != nil {
		return errors.Join(err, logErr)
	}
	s.Phase = "downloaded"
	return errors.Join(saveState(dir, s), logErr)
}
func archivePath(dir string, s *state) (string, error) {
	var found []string
	want := "rooftam-" + s.Config.RunID + ".zip"
	err := filepath.WalkDir(filepath.Join(dir, "downloads"), func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.Type()&os.ModeSymlink != 0 {
			return errors.New("symlink found in downloaded output")
		}
		if !d.IsDir() && d.Name() == want {
			found = append(found, p)
		}
		return nil
	})
	if err != nil {
		return "", err
	}
	if len(found) != 1 {
		return "", fmt.Errorf("expected one %s archive, found %d; remote failure/termination may have prevented packaging; inspect download.log", want, len(found))
	}
	return found[0], nil
}
func (a *app) finish(ctx context.Context, k, dir string, s *state, limit, poll time.Duration) error {
	if s.Phase == "verified" || s.Phase == "review" || s.Phase == "downloaded" {
		return a.verify(dir, s)
	}
	remoteErr := a.wait(ctx, k, dir, s, limit, poll)
	if ctx.Err() != nil {
		return remoteErr
	}
	// Do not download during an unresolved timeout or transient outage.
	switch s.Phase {
	case "complete", "error", "failed", "cancelled", "canceled":
	default:
		return remoteErr
	}
	downloadErr := a.download(ctx, k, dir, s)
	if remoteErr != nil {
		return errors.Join(remoteErr, downloadErr)
	}
	if downloadErr != nil {
		return downloadErr
	}
	return a.verify(dir, s)
}
