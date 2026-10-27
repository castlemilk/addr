//go:build darwin || linux || freebsd || openbsd || netbsd || dragonfly

package annotationsync

import (
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

func lockDirectory(directory string) (func(), error) {
	file, err := os.OpenFile(filepath.Join(directory, ".sync.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		file.Close()
		return nil, fmt.Errorf("mirror is already being synced or cannot be locked: %w", err)
	}
	// Leave the inode in place: deleting a lock file can give a second process
	// a different inode and allow two writers. The OS releases this lock on exit.
	return func() { _ = file.Close() }, nil
}
