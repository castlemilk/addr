"""Per-unit redraws for Corners items whose outline spans several Vicmap lots (reviewer 2026-10-12: grouped townhouses /
terraces "should be units"; "the property boundaries from vic maps would have shown them as separate").

For each queue item judged 'several' by parcel_units: split the saved outline at the lot lines, then redraw each piece as the
simplest right-angle shape (roof_corners.fit). Writes a new items file (same corner_set, lines after the queue) that the
Fieldwork stage reads. The lot line is cadastral, not a measured party wall. Never writes scenes."""
import argparse, hashlib, json
from pathlib import Path

from scripts.ml import parcel_units as pu
from scripts.ml.experiments.roof_corners import fit, to_m

MIN_PIECE_M2 = 15


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--items', required=True, help='queue items.json')
    ap.add_argument('--out', required=True)
    ap.add_argument('--line-start', dest='line_start', type=int, required=True)
    args = ap.parse_args()
    doc = json.load(open(args.items))
    picks, skipped, line, seen_lots = [], [], args.line_start, {}
    for x in doc['corner']:
        scene = json.load(open(f"data/scenes/{x['scene']}/latest.json"))
        parcels = pu.scene_parcels(scene)
        if pu.unit_verdict(scene, x['current_ring'], parcels)['units'] != 'several':
            continue
        p2m = scene['pixel_to_meters']
        aff = [p2m[0][0], p2m[1][0], p2m[2][0], p2m[0][1], p2m[1][1], p2m[2][1]]
        a, b, c, d, e, f = aff
        det = a * e - b * d
        inv = lambda X, Y: [(e * (X - c) - b * (Y - f)) / det, (-d * (X - c) + a * (Y - f)) / det]
        W, H = scene['image_width'], scene['image_height']
        made = []
        for piece in pu.split_by_lots(scene, x['current_ring'], parcels):
            if piece['m2'] < MIN_PIECE_M2:
                skipped.append({'parent': x['line'], 'lot': piece['lot'], 'why': f"piece {piece['m2']} m2"}); continue
            shape_m, info = fit(to_m(piece['ring'], aff))
            if shape_m is None:
                skipped.append({'parent': x['line'], 'lot': piece['lot'], 'why': 'no right-angle shape fits', **info}); continue
            ring = [[round(v, 3) for v in inv(px, py)] for px, py in shape_m]
            ring, _ = pu.bound_to_lot(scene, ring, parcels, piece['pfi'])  # the fit may spill over the lot line: clip it back
            ring = [[round(v, 3) for v in p] for p in ring]
            if not all(0 <= px <= W and 0 <= py <= H for px, py in ring):
                skipped.append({'parent': x['line'], 'lot': piece['lot'], 'why': 'past the image edge'}); continue
            pid = hashlib.sha256(json.dumps([x['scene'], x['object_id'], piece['pfi'], ring]).encode()).hexdigest()[:16]
            made.append({**{k: x[k] for k in ('scene', 'object_id', 'kind', 'status_at_compute', 'current_ring', 'current_ring_sha',
                                                  'current_corners', 'area_m2')},
                          'proposed_ring': ring, 'unit_piece_ring': [[round(v, 3) for v in q] for q in piece['ring']], 'proposed_corners': info['corners'], 'iou': info['iou'],
                          'method': f"lot-split+{info['method']}", 'proposal_id': pid, 'split': x.get('split', 'train'),
                          'unit_of': {'line': x['line'], 'lot': piece['lot'], 'plan': piece['plan'], 'pfi': piece['pfi'],
                                      'source': pu.VERSION}})
        lots = frozenset(m['unit_of']['pfi'] for m in made)
        if lots in seen_lots:  # the same building seen from an overlapping scene: split it once
            skipped.append({'parent': x['line'], 'why': f"same lots as line {seen_lots[lots]}"}); continue
        if len(made) >= 2:
            seen_lots[lots] = x['line']
        if len(made) < 2:  # one usable unit is not a split: the reviewer answers the parent item instead
            skipped.append({'parent': x['line'], 'why': f'{len(made)} usable unit(s)'}); continue
        for m in made:
            m['line'] = line; picks.append(m); line += 1
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    (out / 'items.json').write_text(json.dumps({'corner': picks, 'corner_set': doc.get('corner_set')}, indent=1))
    summ = {'units': len(picks), 'parents': len({p['unit_of']['line'] for p in picks}), 'lines': [args.line_start, line - 1],
            'skipped': skipped}
    (out / 'summary.json').write_text(json.dumps(summ, indent=1))
    print(json.dumps({k: v for k, v in summ.items() if k != 'skipped'}), 'skipped', len(skipped))


if __name__ == '__main__':
    main()
