"""By-eye skeptic check (2026-10-06): how many touching building pairs whose smaller piece is covered >= 0.8 (or 0.5-0.8)
are labelled same_mask / occluder_between by the census's category precedence, per rendered cluster and pooled.
Read-only over data/scenes; reuses roof_fragmentation_census loaders. Counts only."""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import roof_fragmentation_census as m

RENDERED = ['roof_training_250-25113', 'roof_training_250-29501', 'roof_training_250-29761', 'roof_training_250-25156',
            'win-17b5c9b30a5d3ff9', 'win-320abd640ac09c1a', 'roof_training_250-25119', 'win-fa2e955f73294188',
            'roof_training_250-30298', 'roof_training_250-25323', 'roof_training_250-25221', 'roof_training_250-28508']
OUT = Path(m.ROOT)/'data/reviews/eyes/experiments/roof-fragmentation-2026-10-06/precedence-check.json'

def review_pairs(path):
    scene = m.load_json(path/'latest.json')
    review = m.load_json(path/'review.json')
    rows, _ = m.layer_rows(scene, review.get('objects', []), 'review')
    live = [r for r in rows if r['live']]
    buildings = [r for r in live if r['kind'] == 'building']
    return m.building_pairs(buildings, [r for r in live if r['kind'] in m.OCCLUDER_KINDS]), buildings

result = {'rendered': {}, 'pooled': {}}
pooled = {g: Counter() for g in ('windows', 'review_set')}
for path, group in m.population():
    if not (path/'review.json').exists():
        continue
    pairs, buildings = review_pairs(path)
    c = pooled[group]
    for p in pairs:
        cat = m.pair_category(p)
        c['pairs'] += 1
        if p['band'] == 'ge_0.8':
            c['band_ge_0.8'] += 1
            c[f'ge_0.8_as_{cat}'] += 1
        if p['band'] == 'band_0.5_0.8':
            c['band_0.5_0.8'] += 1
            c[f'band_0.5_0.8_as_{cat}'] += 1
    if path.name in RENDERED:
        big = max(buildings, key=lambda b: b['area_m2'])
        covers = {}
        for b in buildings:
            if b['id'] == big['id']:
                continue
            inter = b['_metric'].intersection(big['_metric']).area
            if inter > 0:
                covers[b['id'][:8]] = round(inter/b['area_m2'], 2)
        result['rendered'][path.name] = {
            'largest': f"{big['id'][:8]} {big['area_m2']:.0f} m2 {big['status']}",
            'pieces_covered_by_largest_ge_0.8': sum(1 for v in covers.values() if v >= .8),
            'pieces_covered_by_largest_0.5_0.8': sum(1 for v in covers.values() if .5 <= v < .8),
            'pairs_ge_0.8_by_category': dict(Counter(m.pair_category(p) for p in pairs if p['band'] == 'ge_0.8')),
            'pairs_band_by_category': dict(Counter(m.pair_category(p) for p in pairs if p['band'] == 'band_0.5_0.8')),
            'largest_vertices': big['vertices']}
result['pooled'] = {g: dict(c) for g, c in pooled.items()}
OUT.write_text(json.dumps(result, indent=1, sort_keys=True))
print(json.dumps(result, indent=1, sort_keys=True))
