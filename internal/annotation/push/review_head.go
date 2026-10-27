package push

import (
	"encoding/json"
	"errors"
)

// SplitReviewHistory separates review.json into a compact head (every key
// except history, values byte-preserved) and the raw history array. The head is
// re-encoded, so its hash differs from the on-disk review.json; the hash is
// recorded on the pushed item and its bytes are kept as push evidence.
func SplitReviewHistory(raw []byte) (head, history []byte, err error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil || fields == nil {
		return nil, nil, errors.New("review.json is not a JSON object")
	}
	if entry, ok := fields["history"]; ok {
		history = entry
		delete(fields, "history")
	}
	head, err = json.Marshal(fields)
	if err != nil {
		return nil, nil, err
	}
	return head, history, nil
}
