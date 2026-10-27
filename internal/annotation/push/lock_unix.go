//go:build darwin || linux || freebsd || openbsd || netbsd || dragonfly

package push

import (
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

// Lock takes push.lock in the receipts directory; the OS releases it on exit.
func Lock(dir string) (func(), error) {
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(filepath.Join(dir, "push.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		file.Close()
		return nil, fmt.Errorf("another push is using this receipts directory: %w", err)
	}
	return func() { _ = file.Close() }, nil
}
