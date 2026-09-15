package geo

import (
	"fmt"
	"math"
	"regexp"
	"strings"
	"unicode"
)

// Australian street abbreviation dictionary for bidirectional synonym expansion.
var streetAbbrevs = map[string][]string{
	"rd":        {"road", "rd"},
	"road":      {"road", "rd"},
	"st":        {"street", "st"},
	"street":    {"street", "st"},
	"ave":       {"avenue", "ave", "av"},
	"av":        {"avenue", "ave", "av"},
	"avenue":    {"avenue", "ave", "av"},
	"dr":        {"drive", "dr"},
	"drive":     {"drive", "dr"},
	"cl":        {"close", "cl"},
	"close":     {"close", "cl"},
	"cres":      {"crescent", "cres", "cr"},
	"cr":        {"crescent", "cres", "cr"},
	"crescent":  {"crescent", "cres", "cr"},
	"ct":        {"court", "ct"},
	"court":     {"court", "ct"},
	"hwy":       {"highway", "hwy"},
	"highway":   {"highway", "hwy"},
	"pde":       {"parade", "pde"},
	"parade":    {"parade", "pde"},
	"pl":        {"place", "pl"},
	"place":     {"place", "pl"},
	"tce":       {"terrace", "tce"},
	"terrace":   {"terrace", "tce"},
	"ln":        {"lane", "ln"},
	"lane":      {"lane", "ln"},
	"bvd":       {"boulevard", "bvd", "blvd"},
	"blvd":      {"boulevard", "bvd", "blvd"},
	"boulevard": {"boulevard", "bvd", "blvd"},
	"esp":       {"esplanade", "esp"},
	"esplanade": {"esplanade", "esp"},
	"cct":       {"circuit", "cct"},
	"circuit":   {"circuit", "cct"},
	"way":       {"way"},
	"sq":        {"square", "sq"},
	"square":    {"square", "sq"},
}

var houseNumRegex = regexp.MustCompile(`^[0-9]+[a-zA-Z]?$|^[0-9]+-[0-9]+[a-zA-Z]?$`)

// ParsedQuery represents a parsed, normalized Australian address search query.
type ParsedQuery struct {
	Raw         string
	Tokens      []string
	HouseNumber string
	StreetType  string
	CoreTokens  []string
}

// ParseSearchQuery cleans and tokenizes a search query, categorizing house numbers, street types, and core names.
func ParseSearchQuery(query string) ParsedQuery {
	clean := strings.ToLower(strings.TrimSpace(query))
	var b strings.Builder
	for _, r := range clean {
		if unicode.IsLetter(r) || unicode.IsDigit(r) || r == '-' {
			b.WriteRune(r)
		} else {
			b.WriteRune(' ')
		}
	}

	fields := strings.Fields(b.String())
	pq := ParsedQuery{
		Raw:    query,
		Tokens: fields,
	}

	for _, f := range fields {
		if pq.HouseNumber == "" && houseNumRegex.MatchString(f) {
			pq.HouseNumber = f
		} else if _, isAbbrev := streetAbbrevs[f]; isAbbrev && pq.StreetType == "" {
			pq.StreetType = f
		} else {
			pq.CoreTokens = append(pq.CoreTokens, f)
		}
	}

	return pq
}

// BuildFTS5Query generates a token-expanded FTS5 prefix query.
func (pq ParsedQuery) BuildFTS5Query() string {
	if len(pq.Tokens) == 0 {
		return ""
	}

	clauses := make([]string, 0, len(pq.Tokens))
	for _, t := range pq.Tokens {
		cleanT := strings.Trim(t, `"*'-`)
		if cleanT == "" {
			continue
		}

		if syns, ok := streetAbbrevs[cleanT]; ok {
			synClauses := make([]string, len(syns))
			for i, syn := range syns {
				synClauses[i] = syn + "*"
			}
			clauses = append(clauses, "("+strings.Join(synClauses, " OR ")+")")
		} else {
			clauses = append(clauses, cleanT+"*")
		}
	}

	if len(clauses) == 0 {
		return ""
	}
	return strings.Join(clauses, " AND ")
}

// BuildTypoFTSQueries returns candidate queries resilient to typos in the core street/suburb names.
func (pq ParsedQuery) BuildTypoFTSQueries() []string {
	var queries []string

	streetClause := ""
	if pq.StreetType != "" {
		if syns, ok := streetAbbrevs[pq.StreetType]; ok {
			synClauses := make([]string, len(syns))
			for i, syn := range syns {
				synClauses[i] = syn + "*"
			}
			streetClause = "(" + strings.Join(synClauses, " OR ") + ")"
		}
	}

	// For each core token (e.g. "cambrdige"), generate 3 and 4 character prefixes (e.g. "camb*", "cam*")
	for _, ct := range pq.CoreTokens {
		if len(ct) < 4 {
			continue
		}

		prefixes := []string{ct[:3] + "*"}
		if len(ct) >= 5 {
			prefixes = append(prefixes, ct[:4]+"*")
		}

		for _, p := range prefixes {
			// Combination 1: HouseNumber + Core Prefix + StreetType
			if pq.HouseNumber != "" && streetClause != "" {
				queries = append(queries, fmt.Sprintf("%s* AND %s AND %s", pq.HouseNumber, p, streetClause))
			}
			// Combination 2: HouseNumber + Core Prefix
			if pq.HouseNumber != "" {
				queries = append(queries, fmt.Sprintf("%s* AND %s", pq.HouseNumber, p))
			}
			// Combination 3: Core Prefix + StreetType
			if streetClause != "" {
				queries = append(queries, fmt.Sprintf("%s AND %s", p, streetClause))
			}
			// Combination 4: Just Core Prefix
			queries = append(queries, p)
		}
	}

	return queries
}

// DamerauLevenshtein computes the optimal string alignment distance
// including insertions, deletions, substitutions, and adjacent transpositions.
func DamerauLevenshtein(s1, s2 string) int {
	len1 := len(s1)
	len2 := len(s2)
	if len1 == 0 {
		return len2
	}
	if len2 == 0 {
		return len1
	}
	if s1 == s2 {
		return 0
	}

	d := make([][]int, len1+1)
	for i := range d {
		d[i] = make([]int, len2+1)
		d[i][0] = i
	}
	for j := 0; j <= len2; j++ {
		d[0][j] = j
	}

	for i := 1; i <= len1; i++ {
		for j := 1; j <= len2; j++ {
			cost := 0
			if s1[i-1] != s2[j-1] {
				cost = 1
			}

			d[i][j] = min(
				d[i-1][j]+1,      // deletion
				d[i][j-1]+1,      // insertion
				d[i-1][j-1]+cost, // substitution
			)

			if i > 1 && j > 1 && s1[i-1] == s2[j-2] && s1[i-2] == s2[j-1] {
				d[i][j] = min(d[i][j], d[i-2][j-2]+1)
			}
		}
	}

	return d[len1][len2]
}

// TokenSimilarity computes the similarity score between a query token and a target token.
func TokenSimilarity(q, target string) (score float64, matched bool) {
	if q == target {
		return 60.0, true
	}

	// Check street abbreviation synonym
	if syns, ok := streetAbbrevs[q]; ok {
		for _, s := range syns {
			if s == target {
				return 55.0, true
			}
		}
	}

	// Prefix match: target starts with q
	if strings.HasPrefix(target, q) {
		ratio := float64(len(q)) / float64(len(target))
		return 45.0 + (ratio * 10.0), true
	}

	// For typos: compute Damerau-Levenshtein distance
	qLen := len(q)
	tLen := len(target)
	if qLen >= 4 && tLen >= 4 {
		dist := DamerauLevenshtein(q, target)
		if dist == 1 {
			return 38.0, true
		}
		if qLen >= 6 && tLen >= 6 && dist == 2 {
			return 22.0, true
		}
	}

	return 0.0, false
}

// ScoreCandidate ranks a candidate address or place against the parsed query.
func ScoreCandidate(pq ParsedQuery, title, subtitle string, candidateNumber *string, userLat, userLon *float64, candLat, candLon float64) float64 {
	totalScore := 0.0

	// Tokenize candidate text
	candText := strings.ToLower(title + " " + subtitle)
	candTokens := strings.Fields(strings.Map(func(r rune) rune {
		if unicode.IsLetter(r) || unicode.IsDigit(r) || r == '-' {
			return r
		}
		return ' '
	}, candText))

	// 1. House number scoring
	if pq.HouseNumber != "" {
		exactNumMatch := false
		if candidateNumber != nil && strings.EqualFold(*candidateNumber, pq.HouseNumber) {
			exactNumMatch = true
			totalScore += 120.0
		} else if strings.HasPrefix(candText, pq.HouseNumber+" ") || strings.HasPrefix(title, pq.HouseNumber+" ") {
			exactNumMatch = true
			totalScore += 100.0
		} else {
			for _, ct := range candTokens {
				if ct == pq.HouseNumber {
					exactNumMatch = true
					totalScore += 60.0
					break
				}
			}
		}

		if !exactNumMatch {
			totalScore -= 20.0
		}
	}

	// 2. Query tokens matching against candidate tokens
	allQueryWords := append([]string{}, pq.CoreTokens...)
	if pq.StreetType != "" {
		allQueryWords = append(allQueryWords, pq.StreetType)
	}

	matchedQueryTokens := 0
	for _, qt := range allQueryWords {
		bestTokenScore := 0.0
		tokenMatched := false

		for _, ct := range candTokens {
			s, m := TokenSimilarity(qt, ct)
			if m && s > bestTokenScore {
				bestTokenScore = s
				tokenMatched = true
			}
		}

		if tokenMatched {
			matchedQueryTokens++
			totalScore += bestTokenScore
		} else {
			totalScore -= 15.0
		}
	}

	// Coverage bonus: if all query words matched
	if len(allQueryWords) > 0 && matchedQueryTokens == len(allQueryWords) {
		totalScore += 40.0
	}

	// 3. Proximity bonus (if user GPS provided)
	if userLat != nil && userLon != nil {
		distMeters := HaversineDistance(*userLat, *userLon, candLat, candLon)
		distKm := distMeters / 1000.0
		proximityBonus := 15.0 / (1.0 + (distKm / 10.0))
		totalScore += proximityBonus
	}

	return math.Round(totalScore*10.0) / 10.0
}
