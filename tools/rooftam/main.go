// RoofTAM controls the experiment lifecycle; Python remains the model runtime.
package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if err := (&app{out: os.Stdout, errOut: os.Stderr}).run(ctx, os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "rooftam:", err)
		var review *reviewError
		if errors.As(err, &review) {
			os.Exit(3)
		}
		os.Exit(1)
	}
}
