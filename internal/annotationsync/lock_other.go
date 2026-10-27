//go:build !darwin && !linux && !freebsd && !openbsd && !netbsd && !dragonfly

package annotationsync

import "errors"

func lockDirectory(string) (func(), error) {
	return nil, errors.New("local sync currently requires a system with flock support (macOS, Linux, or BSD)")
}
