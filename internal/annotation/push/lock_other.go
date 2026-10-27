//go:build !darwin && !linux && !freebsd && !openbsd && !netbsd && !dragonfly

package push

import "errors"

func Lock(string) (func(), error) {
	return nil, errors.New("push currently requires a system with flock support (macOS, Linux, or BSD)")
}
