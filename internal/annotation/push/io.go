package push

import "io"

func copyTo(dst io.Writer, src io.Reader) (int64, error) { return io.Copy(dst, src) }
