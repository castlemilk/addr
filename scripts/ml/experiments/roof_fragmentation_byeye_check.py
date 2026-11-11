"""By-eye skeptic helper (2026-10-06): for the 12 rendered worst clusters, list every touching pair's cover band next to the
precedence category the census assigned, and count pairs where an umbrella / overlap (>= .8 cover) is relabelled by an occluder.
Read-only; imports roof_fragmentation_census and reuses its loaders."""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import roof_fragmentation_census as c

OUT = Path('data/reviews/eyes/experiments/roof-fragmentation-2026-10-06/byeye-pair-check.json')
SCENES = ['roof_training_250-25113', 'roof_training_250-29501', 'roof_training_250-29761', 'roof_training_250-25156',
          'win-17b5c9b30a5d3ff9', 'win-320abd640ac09c1a', 'roof_training_250-25119', 'win-fa2e955f73294188',
          'roof_training_250-30298', 'roof_training_250-25323', 'roof_training_250-25221', 'roof_training_250-28508']
out = {}
for sid in SCENES:
    path = c.SCENES / sid
    scene = c.load_json(path/'latest.json')
    review = c.load_json(path/'review.json')
    rows, _ = c.layer_rows(scene, review.get('objects', []), 'review')
    live = [r for r in rows if r['live']]
    buildings = [r for r in live if r['kind'] == 'building']
    occl = [r for r in live if r['kind'] in c.OCCLUDER_KINDS]
    pairs = c.building_pairs(buildings, occl)
    clusters = c.building_clusters(buildings, pairs, sid, 'review', 'x')
    big = max(clusters, key=lambda k: k['pieces'])
    ids = set(big['ids'])
    area = {b['id']: b['area_m2'] for b in buildings}
    largest = max(ids, key=lambda i: area[i])
    cp = [p for p in pairs if p['a'] in ids and p['b'] in ids]
    frag = [p for p in cp if c.fragmentation_pair(p)]
    frag_with_largest = [p for p in frag if largest in (p['a'], p['b'])]
    ge8 = [p for p in cp if p['band'] == 'ge_0.8']
    ge8_as_occl = [p for p in ge8 if c.pair_category(p) == 'occluder_between']
    small = [p for p in frag if p['small_piece']]
    out[sid] = {'pieces': big['pieces'], 'largest_id': largest[:8], 'largest_m2': area[largest], 'pairs': len(cp),
                'fragmentation_pairs': len(frag), 'frag_pairs_with_largest': len(frag_with_largest),
                'frag_pairs_with_largest_cover_ge_0.8': sum(1 for p in frag_with_largest if p['cover_of_smaller'] >= 0.8),
                'pairs_cover_ge_0.8': len(ge8), 'pairs_cover_ge_0.8_labelled_occluder': len(ge8_as_occl),
                'frag_pairs_smaller_lt_12': len(small), 'frag_pairs_same_mask': sum(1 for p in frag if p['same_mask']),
                'categories': dict(Counter(c.pair_category(p) for p in cp)), 'bands': dict(Counter(p['band'] for p in cp)),
                'pair_list': [{'a': p['a'][:8], 'b': p['b'][:8], 'cover': p['cover_of_smaller'], 'smaller_m2': p['smaller_m2'],
                               'band': p['band'], 'cat': c.pair_category(p), 'occl': p['occluders_between']} for p in cp]}
OUT.write_text(json.dumps(out, indent=1))
for sid, v in out.items():
    print(sid[-20:], 'pcs', v['pieces'], 'largest', v['largest_m2'], 'pairs', v['pairs'], 'frag', v['fragmentation_pairs'],
          'fragWithLargest', v['frag_pairs_with_largest'], 'ofWhichCover>=.8', v['frag_pairs_with_largest_cover_ge_0.8'],
          'ge.8', v['pairs_cover_ge_0.8'], 'ge.8->occl', v['pairs_cover_ge_0.8_labelled_occluder'], 'sameMask', v['frag_pairs_same_mask'])
