package migration

import (
	"fmt"
	"net"
	"net/url"
	"os"
)

// AllowLiveEnv is the environment switch shared with the Python wrappers.
const AllowLiveEnv = "FIELDWORK_PUSH_ALLOW_LIVE"

// LoopbackOrigin reports whether baseURL names localhost or a loopback IP,
// i.e. a local --dev annotation-server rather than a live project.
func LoopbackOrigin(baseURL string) bool {
	u, err := url.Parse(baseURL)
	if err != nil || u.Hostname() == "" {
		return false
	}
	host := u.Hostname()
	ip := net.ParseIP(host)
	return host == "localhost" || ip != nil && ip.IsLoopback()
}

// GuardLiveWrite refuses a write command against any non-loopback origin unless
// the caller passed --allow-live or FIELDWORK_PUSH_ALLOW_LIVE=1. Reads and dry
// runs never call it. Every spelling of a live origin (case, port, trailing
// slash) is non-loopback, so none of them bypasses the check; an unparsable
// URL is refused too.
func GuardLiveWrite(baseURL, command string, allowLive bool) error {
	if LoopbackOrigin(baseURL) || allowLive || os.Getenv(AllowLiveEnv) == "1" {
		return nil
	}
	return fmt.Errorf("refusing %s against non-loopback Fieldwork origin %s: it would write to a live project; use a local --dev server, or pass --allow-live (or set %s=1) only for an explicitly authorised live write", command, baseURL, AllowLiveEnv)
}
