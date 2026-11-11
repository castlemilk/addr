package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
)

const (
	maxMCPRequestBytes = 8 << 20
	// Export tools include both text and structured JSON. A 32 MiB snapshot
	// needs extra room for the second representation and JSON escaping.
	maxMCPResponseBytes = 128 << 20
)

// mcp bridges the standard newline-delimited stdio transport to the server's
// Streamable HTTP endpoint. Standard output contains protocol messages only.
func (c *client) mcp(ctx context.Context, input io.Reader, output, diagnostics io.Writer) error {
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 4096), maxMCPRequestBytes)
	session := ""
	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}
		var message struct {
			ID     json.RawMessage `json:"id"`
			Method string          `json:"method"`
		}
		// A JSON-RPC batch is an array of messages; it is checked message by message below.
		var batch []json.RawMessage
		if err := json.Unmarshal(line, &message); err != nil && (line[0] != '[' || json.Unmarshal(line, &batch) != nil) {
			if err := writeMCPError(output, json.RawMessage("null"), -32700, "Invalid JSON"); err != nil {
				return err
			}
			continue
		}
		if err := c.mcpMessageRefusal(ctx, line); err != nil {
			fmt.Fprintln(diagnostics, "annotate mcp:", err)
			if err := writeMCPRefusal(output, line, err.Error()); err != nil {
				return err
			}
			continue
		}
		resp, err := c.post(ctx, "/mcp", line, session)
		if err != nil {
			fmt.Fprintln(diagnostics, "annotate mcp:", err)
			if err := writeMCPFailure(output, line, batch != nil); err != nil {
				return err
			}
			continue
		}
		if value := resp.Header.Get("Mcp-Session-Id"); value != "" {
			session = value
		}
		if resp.StatusCode == http.StatusAccepted || resp.StatusCode == http.StatusNoContent {
			resp.Body.Close()
			continue
		}
		if strings.HasPrefix(resp.Header.Get("Content-Type"), "text/event-stream") {
			err = copySSEMessages(resp.Body, output)
		} else {
			var data []byte
			data, err = io.ReadAll(io.LimitReader(resp.Body, maxMCPResponseBytes+1))
			if err == nil && len(data) > maxMCPResponseBytes {
				err = fmt.Errorf("MCP response exceeds 128 MiB")
			}
			if err == nil && len(bytes.TrimSpace(data)) > 0 {
				err = writeMCPMessage(output, data)
			}
		}
		resp.Body.Close()
		if err != nil {
			return err
		}
	}
	return scanner.Err()
}

func writeMCPError(output io.Writer, id json.RawMessage, code int, message string) error {
	return json.NewEncoder(output).Encode(mcpError(id, code, message))
}

func mcpError(id json.RawMessage, code int, message string) map[string]any {
	return map[string]any{"jsonrpc": "2.0", "id": id, "error": map[string]any{"code": code, "message": message}}
}

// writeMCPFailure answers a line the bridge could not forward: one error per
// request id (an array for a batch), nothing for notifications.
func writeMCPFailure(output io.Writer, line []byte, batch bool) error {
	ids := mcpRequestIDs(line)
	if len(ids) == 0 {
		return nil
	}
	if !batch {
		return writeMCPError(output, ids[0], -32000, "Fieldwork request failed; see stderr")
	}
	responses := make([]map[string]any, 0, len(ids))
	for _, id := range ids {
		responses = append(responses, mcpError(id, -32000, "Fieldwork request failed; see stderr"))
	}
	return json.NewEncoder(output).Encode(responses)
}

func writeMCPMessage(output io.Writer, data []byte) error {
	var compact bytes.Buffer
	if err := json.Compact(&compact, data); err != nil {
		return fmt.Errorf("invalid MCP server response")
	}
	compact.WriteByte('\n')
	_, err := compact.WriteTo(output)
	return err
}

func copySSEMessages(input io.Reader, output io.Writer) error {
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 4096), maxMCPResponseBytes)
	var data []string
	responseBytes := 0
	flush := func() error {
		if len(data) == 0 {
			return nil
		}
		message := strings.Join(data, "\n")
		data = nil
		if message == "[DONE]" {
			return nil
		}
		return writeMCPMessage(output, []byte(message))
	}
	for scanner.Scan() {
		line := scanner.Text()
		responseBytes += len(line) + 1
		if responseBytes > maxMCPResponseBytes {
			return fmt.Errorf("MCP response exceeds 128 MiB")
		}
		if line == "" {
			if err := flush(); err != nil {
				return err
			}
		} else if strings.HasPrefix(line, "data:") {
			data = append(data, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
		}
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	return flush()
}
