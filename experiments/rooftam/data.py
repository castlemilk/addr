"""Reviewed-data contract, spatial split checks and reversible letterboxing."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


def digest(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def local_file(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f'File must exist inside dataset directory: {name}')
    return path


def load_manifest(path: str | Path, allow_synthetic: bool = False) -> list[dict]:
    path = Path(path).resolve()
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not rows:
        raise ValueError('Empty manifest')
    ids, group_splits, image_splits, kinds = set(), {}, {}, set()
    for row in rows:
        sid = row['id']
        if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', sid) or sid in ids:
            raise ValueError(f'Unsafe or duplicate sample id: {sid}')
        ids.add(sid)
        split = row['split']
        if split not in ('train', 'val', 'test'):
            raise ValueError(f'{sid}: invalid split')
        group = row['geo_group']
        if not isinstance(group, str) or not group:
            raise ValueError(f'{sid}: geographic blocking group is required')
        if group_splits.setdefault(group, split) != split:
            raise ValueError(f'{sid}: geographic group leaks across splits')
        if split == 'train' and row.get('training_eligible') is not True:
            raise ValueError(f'{sid}: training requires explicit human-approved eligibility')
        kind = row['label_status']
        kinds.add(kind)
        if kind not in ('human_verified', 'synthetic'):
            raise ValueError(f'{sid}: agent drafts/pseudo-labels are not approved ground truth')
        if kind == 'synthetic' and not allow_synthetic:
            raise ValueError('Synthetic data needs --allow-synthetic; it is not roof evidence')
        for flag in ('training_allowed', 'cloud_upload_allowed'):
            if row.get(flag) is not True:
                raise ValueError(f'{sid}: {flag} must be explicitly true')
        if not row.get('license') or not row.get('source_ref'):
            raise ValueError(f'{sid}: licence and source provenance required')
        if row.get('label_kind') != 'visible_roof_outline':
            raise ValueError(f'{sid}: this experiment evaluates visible roof outlines only')
        if row.get('prompt_source') not in ('detector', 'human_box', 'synthetic'):
            raise ValueError(f'{sid}: use an independent detector/human box, not a label-derived box')
        if kind != 'synthetic' and row['prompt_source'] == 'synthetic':
            raise ValueError(f'{sid}: synthetic prompts are not allowed on real data')
        gsd = float(row['gsd_m'])
        if not math.isfinite(gsd) or gsd <= 0:
            raise ValueError(f'{sid}: gsd_m must be positive metres per native pixel')
        image = local_file(path.parent, row['image'])
        mask = local_file(path.parent, row['mask'])
        with Image.open(image) as im, Image.open(mask) as ma:
            w, h = im.size
            values = np.asarray(ma)
            if im.size != ma.size or values.ndim != 2:
                raise ValueError(f'{sid}: mask must be single-channel and match native image size')
            if not set(np.unique(values)).issubset({0, 1, 255}) or not values.any():
                raise ValueError(f'{sid}: expected a nonempty binary roof mask')
        box = np.asarray(row['box_xyxy'], dtype=float)
        if box.shape != (4,) or not np.isfinite(box).all():
            raise ValueError(f'{sid}: invalid prompt box')
        x0, y0, x1, y1 = box
        if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
            raise ValueError(f'{sid}: box must be inside native image bounds')
        sha = digest(image)
        if image_splits.setdefault(sha, split) != split:
            raise ValueError(f'{sid}: identical imagery leaks across splits')
        row.update(image_path=str(image), mask_path=str(mask), image_sha256=sha,
                   mask_sha256=digest(mask), native_hw=[h, w])
    if len(kinds) != 1:
        raise ValueError('Do not mix synthetic and real evidence in one dataset')
    return rows


def dataset_digest(rows: list[dict]) -> str:
    # No machine-specific absolute paths; includes content, split and provenance.
    stable = [{k: v for k, v in r.items() if k not in ('image_path', 'mask_path')}
              for r in rows]
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def load_sample(row: dict, side: int):
    import torch
    image = Image.open(row['image_path']).convert('RGB')
    mask = Image.open(row['mask_path']).convert('L')
    w, h = image.size
    scale = side / max(w, h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    # Bottom/right padding. Keep exact rounded x/y scales for inverse mapping.
    canvas = Image.new('RGB', (side, side), (124, 116, 104))
    canvas.paste(image.resize((nw, nh), Image.Resampling.BILINEAR), (0, 0))
    target = np.zeros((side, side), np.float32)
    target[:nh, :nw] = np.asarray(mask.resize((nw, nh), Image.Resampling.NEAREST)) > 0
    valid = np.zeros_like(target)
    valid[:nh, :nw] = 1
    box = np.asarray(row['box_xyxy'], np.float32) * np.array([nw/w, nh/h, nw/w, nh/h])
    return (torch.from_numpy(np.asarray(canvas).copy()).permute(2, 0, 1).float()/255,
            torch.from_numpy(target)[None], torch.from_numpy(valid)[None],
            torch.from_numpy(box.astype(np.float32)), (h, w, nh, nw))


def restore_logits(logits, geometry: tuple, side: int) -> np.ndarray:
    import torch.nn.functional as F
    h, w, nh, nw = geometry
    up = F.interpolate(logits.float().reshape(1, 1, *logits.shape[-2:]),
                       (side, side), mode='bilinear', align_corners=False)
    native = F.interpolate(up[:, :, :nh, :nw], (h, w), mode='bilinear', align_corners=False)
    return native[0, 0].detach().cpu().numpy()


def make_fixture(folder: str | Path, seed: int = 17) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.iterdir()):
        raise ValueError('Fixture output must be empty (will not overwrite a dataset)')
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(12):
        w, h = (96, 64) if i % 2 == 0 else (64, 96)
        image = Image.fromarray(rng.integers(15, 65, (h, w, 3), dtype=np.uint8))
        x0, y0, x1, y1 = 12+i % 5, 13+i % 4, w-12, h-11
        mask = Image.new('L', (w, h))
        points = [(x0, y0), (x1, y0), (x1, y1), (x0+8, y1), (x0, y1-8)]
        ImageDraw.Draw(image).polygon(points, fill=(185, 100+i*3, 90))
        ImageDraw.Draw(mask).polygon(points, fill=255)
        sid = f'synthetic_{i:02d}'
        image.save(folder / f'{sid}.png')
        mask.save(folder / f'{sid}_mask.png')
        rows.append(dict(id=sid, split='train' if i<8 else 'val' if i<10 else 'test',
                         geo_group=sid, image=f'{sid}.png', mask=f'{sid}_mask.png',
                         box_xyxy=[x0-3, y0-3, x1+3, y1+3], prompt_source='synthetic',
                         gsd_m=0.1, label_kind='visible_roof_outline', label_status='synthetic',
                         training_allowed=True, cloud_upload_allowed=True, training_eligible=True,
                         license='CC0 synthetic fixture', source_ref='generated fixture; not a real property'))
    manifest = folder / 'manifest.jsonl'
    manifest.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    return manifest
