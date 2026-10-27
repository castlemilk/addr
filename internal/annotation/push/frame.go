package push

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// Frame is the image identity a model snapshot claims.
type Frame struct {
	ImageSHA256 string
	Width       float64
	Height      float64
}

var pngSignature = []byte{0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'}

// CheckFrame verifies the scene PNG named by the model hashes to image_sha256
// and that its IHDR dimensions equal image_width/height. The server does not
// check dimensions, so this client gate is the only one.
func CheckFrame(root, sceneID string, model Frame) error {
	if !digestPattern.MatchString(model.ImageSHA256) {
		return fmt.Errorf("image_sha256 is not a SHA-256 digest")
	}
	path := filepath.Join(root, "data", "scenes", sceneID, model.ImageSHA256+".png")
	file, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("scene image missing: %w", err)
	}
	defer file.Close()
	header := make([]byte, 24)
	if _, err := io.ReadFull(file, header); err != nil {
		return fmt.Errorf("scene image is not a PNG: %w", err)
	}
	if !bytes.Equal(header[:8], pngSignature) || string(header[12:16]) != "IHDR" {
		return fmt.Errorf("scene image is not a PNG")
	}
	width := float64(binary.BigEndian.Uint32(header[16:20]))
	height := float64(binary.BigEndian.Uint32(header[20:24]))
	if width != model.Width || height != model.Height {
		return fmt.Errorf("PNG is %.0fx%.0f but the model frame is %.0fx%.0f", width, height, model.Width, model.Height)
	}
	hash := sha256.New()
	hash.Write(header)
	if _, err := io.Copy(hash, file); err != nil {
		return err
	}
	if hex.EncodeToString(hash.Sum(nil)) != model.ImageSHA256 {
		return fmt.Errorf("PNG bytes do not hash to image_sha256 %s", model.ImageSHA256)
	}
	return nil
}
