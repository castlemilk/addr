package migration

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"unicode"

	"golang.org/x/text/cases"
	"golang.org/x/text/unicode/norm"
)

// ReviewProjectSuffix marks a Fieldwork review project (for example
// mooroolbark-review). Human decisions use the browser UI. Agents append separate
// assessments through SaveAgentReview with a server-stamped agent actor; ordinary
// client writes to the human review fields remain refused.
const ReviewProjectSuffix = "-review"

// NormalizeProjectID is the spelling every review guard compares, so no
// spelling of a review project slips past them: Unicode NFKC (fullwidth and
// other compatibility forms become ASCII), format characters (zero-width
// spaces, soft hyphens, a BOM) dropped, every dash (Unicode Pd and the minus
// sign) read as '-', case-folded, and whitespace or control characters
// trimmed from both ends. scripts/ml/fieldwork_review.py
// (normalize_project_id) and deploy/annotation/src/mcp-guard.ts
// (normalizeProjectId) apply the same steps.
func NormalizeProjectID(id string) string {
	id = strings.Map(func(r rune) rune {
		switch {
		case unicode.Is(unicode.Cf, r):
			return -1
		case unicode.Is(unicode.Pd, r) || r == '\u2212':
			return '-'
		}
		return r
	}, norm.NFKC.String(id))
	return strings.TrimFunc(cases.Fold().String(id), func(r rune) bool { return unicode.IsSpace(r) || unicode.IsControl(r) })
}

// IsReviewProject reports whether a project id names a review project, in
// any spelling (NormalizeProjectID): MOOROOLBARK-REVIEW, a trailing space and
// a fullwidth hyphen all count.
func IsReviewProject(id string) bool {
	return strings.HasSuffix(NormalizeProjectID(id), ReviewProjectSuffix)
}

// ReviewWriteRefusal is the error every client path returns for a write to a
// review project.
func ReviewWriteRefusal(method, project string) error {
	return fmt.Errorf("refusing %s on project %s: agents never edit a review project (id ending in %s); use SaveAgentReview for a separate agent assessment; human decisions are saved in the Fieldwork UI", method, project, ReviewProjectSuffix)
}

// ReadMethod reports whether an RPC method only reads.
func ReadMethod(method string) bool {
	switch method {
	case "GetInfo", "GetProject", "ListProjects", "GetItem", "ListItems", "SearchItems", "GetLabel", "ListLabels", "GetAnnotation", "ListAnnotations", "ListChanges", "ExportProject":
		return true
	}
	return false
}

// FieldStrings returns every string value a JSON object body holds at a key
// path, matching each key the way the server's encoding/json decoder does
// (strings.EqualFold: "ProjectID", "PROJECTID" and "projectId" are one field)
// and keeping every candidate, because the server reads whichever spelling it
// decodes last. FieldStrings(body, "item", "projectId") reads item.projectId.
func FieldStrings(body []byte, path ...string) []string {
	values := []json.RawMessage{body}
	for _, key := range path {
		var next []json.RawMessage
		for _, value := range values {
			var object map[string]json.RawMessage
			if json.Unmarshal(value, &object) != nil {
				continue
			}
			names := make([]string, 0, len(object))
			for name := range object {
				if strings.EqualFold(name, key) {
					names = append(names, name)
				}
			}
			sort.Strings(names)
			for _, name := range names {
				next = append(next, object[name])
			}
		}
		values = next
	}
	var out []string
	for _, value := range values {
		var text string
		if json.Unmarshal(value, &text) == nil && text != "" {
			out = append(out, text)
		}
	}
	return out
}

// DeclaredProjects returns the project ids a request body names: projectId,
// project.id, item.projectId and label.projectId, in any key spelling the
// server decodes (FieldStrings). Requests that name their target only by item
// or label id need a lookup (cmd/annotate does it).
func DeclaredProjects(body []byte) []string {
	var out []string
	out = append(out, FieldStrings(body, "projectId")...)
	out = append(out, FieldStrings(body, "project", "id")...)
	out = append(out, FieldStrings(body, "item", "projectId")...)
	out = append(out, FieldStrings(body, "label", "projectId")...)
	return out
}

// RefuseDeclaredReviewWrite refuses a write whose request names a review
// project.
func RefuseDeclaredReviewWrite(method string, body []byte) error {
	if ReadMethod(method) || method == "SaveAgentReview" {
		return nil
	}
	for _, project := range DeclaredProjects(body) {
		if IsReviewProject(project) {
			return ReviewWriteRefusal(method, project)
		}
	}
	return nil
}
