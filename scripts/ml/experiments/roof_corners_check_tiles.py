"""Per-item check images for the Corners queue agent check (roof corners card 2026-10-12, item 1).

For each queue item: the aerial crop around the roof twice, side by side - plain on the left, the PROPOSED right-angle outline
(cyan, corners dotted) on the right. A checking agent reads these and writes hints; nothing here writes scenes or Fieldwork."""
import argparse, json, math
from pathlib import Path

from PIL import Image, ImageDraw

T = 384


def scene_image(scene):
    L = json.load(open(f'data/scenes/{scene}/latest.json'))
    return Image.open(f"data/scenes/{scene}/{L['image_sha256']}.png").convert('RGB')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--items', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    for x in json.load(open(args.items))['corner']:
        img = scene_image(x['scene'])
        xs = [p[0] for p in x['current_ring']]; ys = [p[1] for p in x['current_ring']]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        half = max(max(xs) - min(xs), max(ys) - min(ys)) * 0.8 + 8
        box = (cx - half, cy - half, cx + half, cy + half)
        crop = img.crop(tuple(int(round(v)) for v in box)).resize((T, T), Image.BICUBIC)
        k = T / (box[2] - box[0])
        ring = [((px - box[0]) * k, (py - box[1]) * k) for px, py in x['proposed_ring']]
        drawn = crop.copy(); d = ImageDraw.Draw(drawn)
        d.line(ring + ring[:1], fill=(0, 230, 255), width=2)
        for px, py in ring:
            d.ellipse((px - 3, py - 3, px + 3, py + 3), fill=(0, 230, 255))
        tile = Image.new('RGB', (2 * T + 8, T), (0, 0, 0))
        tile.paste(crop, (0, 0)); tile.paste(drawn, (T + 8, 0))
        tile.save(out / f"line-{x['line']:03d}.png")
    print('wrote', out)


if __name__ == '__main__':
    main()
