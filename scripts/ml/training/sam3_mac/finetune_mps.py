"""Single-process SAM3 fine-tune on Apple Silicon (MPS) from the vendored fork's own dataset, loss and matcher code.

The fork's Hydra/submitit trainer assumes CUDA and torch.distributed. This loop keeps its components (Sam3ImageDataset
and transforms, collate_fn_api, Sam3LossWrapper with box, classification+presence and mask terms, the Hungarian
matcher) and drops the launcher: one process, one device, AdamW, optional bf16 autocast, gradient accumulation,
JSONL step log with seconds and MPS memory, checkpoints of the trainable parameters only.

Defaults freeze the vision and language backbones (about 96 percent of the 840M parameters) and train the
transformer encoder/decoder, presence head, box and mask heads on 'building roof' instances. Loss normalisation is
'local' (per process) because there is no process group.

  # shake out MPS operator coverage and step time without weights (random init; 1008 px is fixed: the ViT's rotary
  # tables are precomputed for 1008, smaller resolutions assert)
  scripts/ml/training/.venv-sam3/bin/python -m scripts.ml.training.sam3_mac.finetune_mps \
      --coco data/reviews/eyes/training/sam3-mac/coco-v1 --random-init --limit 8 --steps 3 --dry-run

  # first real run (weights from fetch_weights.sh)
  scripts/ml/training/.venv-sam3/bin/python -m scripts.ml.training.sam3_mac.finetune_mps \
      --coco data/reviews/eyes/training/sam3-mac/coco-v1 --steps 300 --dtype bf16 --out data/reviews/eyes/training/sam3-mac/run-001
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import math
import random
import sys
import time
from datetime import UTC
from functools import partial
from pathlib import Path

import numpy as np

VERSION = 'sam3-mac-finetune-v1'
REPO = Path(__file__).resolve().parents[4]
VENDOR = REPO / 'scripts/ml/training/vendor/sam3-apple-silicon'
BPE = VENDOR / 'assets/bpe_simple_vocab_16e6.txt.gz'
WEIGHTS = REPO / 'scripts/ml/models/sam3-hf/sam3.pt'
FREEZE_DEFAULT = ('backbone.vision_backbone.*', 'backbone.language_backbone.*')
NORM_MEAN = NORM_STD = (.5, .5, .5)
# Loss weights the fork's roboflow config hard-codes; the round-4 objective arms move one at a time through the flags
# --presence-weight / --pos-weight / --o2m-weight. These defaults ARE the round 1-3 constants, so every earlier run line
# reproduces unchanged; run.json records the values under args.
LOSS_DEFAULTS = {'presence_weight': 20., 'pos_weight': 10., 'o2m_weight': 2.}


def now_iso():
    from datetime import datetime
    return datetime.now(UTC).isoformat(timespec='seconds')


def _vendor():
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))


# --- data ----------------------------------------------------------------------------------------------------------

def build_transforms(resolution, training, max_objects=200):
    """The roboflow fine-tune pipeline from sam3/train/configs, written out (segmentation on)."""
    _vendor()
    from sam3.train.transforms.basic import get_random_resize_max_size, get_random_resize_scales
    from sam3.train.transforms.basic_for_api import (
        ComposeAPI,
        NormalizeAPI,
        PadToSizeAPI,
        RandomResizeAPI,
        ToTensorAPI,
    )
    from sam3.train.transforms.filter_query_transforms import (
        FilterCrowds,
        FilterEmptyTargets,
        FilterFindQueriesWithTooManyOut,
        FlexibleFilterFindGetQueries,
    )
    from sam3.train.transforms.point_sampling import RandomizeInputBbox
    from sam3.train.transforms.segmentation import DecodeRle
    if training:
        inner = ComposeAPI([
            FlexibleFilterFindGetQueries(FilterCrowds()),
            RandomizeInputBbox(box_noise_std=.1, box_noise_max=20),
            DecodeRle(),
            RandomResizeAPI(sizes=get_random_resize_scales(size=resolution, min_size=min(480, resolution), rounded=False),
                            consistent_transform=False, max_size=get_random_resize_max_size(size=resolution), square=True),
            PadToSizeAPI(size=resolution, consistent_transform=False),
            ToTensorAPI(),
            FlexibleFilterFindGetQueries(FilterEmptyTargets()),
            NormalizeAPI(mean=list(NORM_MEAN), std=list(NORM_STD)),
            FlexibleFilterFindGetQueries(FilterEmptyTargets()),
        ])
        return [inner, FlexibleFilterFindGetQueries(FilterFindQueriesWithTooManyOut(max_num_objects=max_objects))]
    return [ComposeAPI([
        DecodeRle(),
        RandomResizeAPI(sizes=resolution, consistent_transform=False, max_size=get_random_resize_max_size(size=resolution),
                        square=True),
        ToTensorAPI(),
        NormalizeAPI(mean=list(NORM_MEAN), std=list(NORM_STD)),
    ])]


def build_dataset(coco_root, split, resolution, training, limit=None):
    _vendor()
    from sam3.train.data.sam3_image_dataset import Sam3ImageDataset
    folder = Path(coco_root) / split
    return Sam3ImageDataset(img_folder=str(folder), ann_file=str(folder / '_annotations.coco.json'),
                            transforms=build_transforms(resolution, training), max_ann_per_img=500000, multiplier=1,
                            training=training, load_segmentation=True, max_train_queries=50000, max_val_queries=50000,
                            use_caching=False, limit_ids=limit)


def build_loader(dataset, batch_size, workers, shuffle, seed):
    import torch
    _vendor()
    from sam3.train.data.collator import collate_fn_api
    generator = torch.Generator().manual_seed(seed)
    return torch.utils.data.DataLoader(dataset, batch_size=batch_size, shuffle=shuffle, num_workers=workers, drop_last=shuffle,
                                       generator=generator, collate_fn=partial(collate_fn_api, dict_key='all', with_seg_masks=True))


# --- model and loss ------------------------------------------------------------------------------------------------

def build_model(device, weights=None, random_init=False):
    _vendor()
    import torch
    from sam3.model_builder import build_sam3_image_model
    model = build_sam3_image_model(bpe_path=str(BPE), device=device, eval_mode=False, load_from_HF=False,
                                   checkpoint_path=None if random_init else str(weights), enable_segmentation=True)
    if random_init:
        # the builder allocates some tensors with torch.empty (CLIP positional_embedding, text_projection) and relies on the
        # checkpoint to fill them; uninitialised memory is the source of non-finite matcher costs in a dry run
        with torch.no_grad():
            for name, param in model.named_parameters():
                if not torch.isfinite(param).all() or name.endswith(('positional_embedding', 'text_projection')):
                    torch.nn.init.normal_(param, std=.02)
    return model


def build_loss(presence_weight=LOSS_DEFAULTS['presence_weight'], pos_weight=LOSS_DEFAULTS['pos_weight'],
               o2m_weight=LOSS_DEFAULTS['o2m_weight']):
    """The fork's roboflow fine-tune loss. Only three weights are exposed (image-level presence weight, IABCEMdetr
    pos_weight on matched queries, the one-to-many auxiliary branch weight); loss_ce 20, alpha .25, gamma 2, boxes 5/2,
    masks 200/10 and the matchers stay the fork's constants."""
    _vendor()
    from sam3.train.loss.loss_fns import Boxes, IABCEMdetr, Masks
    from sam3.train.loss.sam3_loss import Sam3LossWrapper
    from sam3.train.matcher import BinaryHungarianMatcherV2, BinaryOneToManyMatcher
    # stable=False (the fork's config) computes the focal class cost with logsigmoid on the raw logits; stable=True takes
    # torch.log(sigmoid * rescaled_giou) and overflows when p saturates. Keep fork parity.
    matcher = BinaryHungarianMatcherV2(focal=True, cost_class=2., cost_bbox=5., cost_giou=2., alpha=.25, gamma=2, stable=False)
    return Sam3LossWrapper(
        loss_fns_find=[
            Boxes(weight_dict={'loss_bbox': 5., 'loss_giou': 2.}),
            IABCEMdetr(weak_loss=False, weight_dict={'loss_ce': 20., 'presence_loss': float(presence_weight)}, pos_weight=float(pos_weight),
                       alpha=.25, gamma=2, use_presence=True, pos_focal=False, pad_n_queries=200, pad_scale_pos=1.),
            Masks(focal_alpha=.25, focal_gamma=2., weight_dict={'loss_mask': 200., 'loss_dice': 10.}, compute_aux=False),
        ],
        normalization='local', matcher=matcher, o2m_matcher=BinaryOneToManyMatcher(alpha=.3, threshold=.4, topk=4),
        o2m_weight=float(o2m_weight), use_o2m_matcher_on_o2m_aux=False, loss_fn_semantic_seg=None, scale_by_find_batch_size=True)


def freeze(model, patterns):
    frozen = trainable = 0
    for name, param in model.named_parameters():
        if any(fnmatch.fnmatch(name, p) for p in patterns):
            param.requires_grad_(False)
            frozen += param.numel()
        else:
            trainable += param.numel()
    return {'frozen_params': frozen, 'trainable_params': trainable, 'patterns': list(patterns)}


def param_groups(model, lr, lr_backbone, weight_decay):
    import torch
    groups = {'decay': [], 'no_decay': [], 'backbone_decay': [], 'backbone_no_decay': []}
    for name, param in model.named_parameters():
        if not param.requires_grad:
            continue
        backbone = name.startswith('backbone.')
        no_decay = name.endswith('bias') or 'norm' in name.lower() or param.ndim == 1
        key = ('backbone_' if backbone else '') + ('no_decay' if no_decay else 'decay')
        groups[key].append(param)
    spec = []
    for key, params in groups.items():
        if params:
            spec.append({'params': params, 'lr': lr_backbone if key.startswith('backbone_') else lr,
                         'weight_decay': 0. if key.endswith('no_decay') else weight_decay, 'name': key})
    return torch.optim.AdamW(spec, betas=(.9, .999))


def core_loss(loss):
    """Sam3LossWrapper returns a dict with the summed 'core_loss' plus components."""
    _vendor()
    from sam3.train.loss.sam3_loss import CORE_LOSS_KEY
    if isinstance(loss, dict):
        parts = {k: float(v) for k, v in loss.items() if k != CORE_LOSS_KEY and hasattr(v, 'item')}
        # the fork logs presence agreement under an '_acc' key; this project never reports agreement as accuracy
        return loss[CORE_LOSS_KEY], {k.replace('_acc', '_agreement'): v for k, v in parts.items()}
    return loss, {}


def memory_mb(device):
    import torch
    if device == 'mps':
        return {'allocated_mb': round(torch.mps.current_allocated_memory() / 2 ** 20, 1),
                'driver_mb': round(torch.mps.driver_allocated_memory() / 2 ** 20, 1)}
    if device.startswith('cuda'):
        return {'allocated_mb': round(torch.cuda.memory_allocated() / 2 ** 20, 1), 'peak_mb': round(torch.cuda.max_memory_allocated() / 2 ** 20, 1)}
    return {}


def lr_at(step, warmup, total, base):
    """Linear warmup then cosine to 5 percent."""
    if step < warmup:
        return base * (step + 1) / max(1, warmup)
    progress = (step - warmup) / max(1, total - warmup)
    return base * (.05 + .95 * .5 * (1 + math.cos(math.pi * min(1., progress))))


def save_checkpoint(model, optimizer, step, out, tag):
    import torch
    out.mkdir(parents=True, exist_ok=True)
    state = {name: p.detach().cpu() for name, p in model.named_parameters() if p.requires_grad}
    path = out / f'trainable-{tag}.pt'
    torch.save({'version': VERSION, 'step': step, 'trainable_state': state, 'optimizer': optimizer.state_dict(),
                'saved_at': now_iso()}, path)
    return str(path)


def load_trainable(model, path, strict_trainable=False):
    """Load a trainable-*.pt over a built model.

    Every key in the checkpoint must exist in the model (no unexpected keys). With strict_trainable (resume), every
    parameter the model marks trainable must also be in the checkpoint; for inference the base weights simply stay
    where the checkpoint is silent (frozen backbones)."""
    import torch
    payload = torch.load(path, map_location='cpu')
    result = model.load_state_dict(payload['trainable_state'], strict=False)
    if result.unexpected_keys:
        raise SystemExit(f'{path}: {len(result.unexpected_keys)} unexpected keys (first: {result.unexpected_keys[0]})')
    if strict_trainable:
        wanted = {name for name, p in model.named_parameters() if p.requires_grad}
        absent = sorted(wanted - set(payload['trainable_state']))
        if absent:
            raise SystemExit(f'{path}: {len(absent)} trainable parameters missing (first: {absent[0]})')
    return payload.get('step', 0), payload


# --- loop ----------------------------------------------------------------------------------------------------------

def train(args):
    import torch
    _vendor()
    from sam3.model.utils.misc import copy_data_to_device
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    device = args.device
    if device == 'mps' and not torch.backends.mps.is_available():
        raise SystemExit('MPS is not available in this torch build')
    torch.manual_seed(args.seed)
    random.seed(args.seed)                 # the fork's transforms draw resize scales and box noise from these
    np.random.seed(args.seed)
    started = time.monotonic()
    model = build_model(device, args.weights, args.random_init)
    load_s = round(time.monotonic() - started, 1)
    model.train()
    freezing = freeze(model, () if args.train_all else tuple(args.freeze))
    if not args.train_all:      # frozen trunks run in eval mode so drop-path and dropout do not add noise to their features
        for module_name in ('vision_backbone', 'language_backbone'):
            module = getattr(model.backbone, module_name, None)
            if module is not None and any(fnmatch.fnmatch(f'backbone.{module_name}.x', p) for p in args.freeze):
                module.eval()
    loss_fn = build_loss(args.presence_weight, args.pos_weight, args.o2m_weight).to(device)
    optimizer = param_groups(model, args.lr, args.lr_backbone, args.weight_decay)
    start_step = 0
    if args.resume:
        start_step, payload = load_trainable(model, args.resume, strict_trainable=True)
        if payload.get('optimizer'):
            optimizer.load_state_dict(payload['optimizer'])
    dataset = build_dataset(args.coco, 'train', args.resolution, training=True, limit=args.limit)
    loader = build_loader(dataset, args.batch_size, args.workers, shuffle=True, seed=args.seed)
    if len(loader) == 0:
        raise SystemExit('no training batches: the train split is empty or smaller than the batch size')
    amp_dtype = {'bf16': torch.bfloat16, 'fp16': torch.float16, 'fp32': None}[args.dtype]
    autocast = (torch.autocast(device_type=device.split(':')[0], dtype=amp_dtype) if amp_dtype is not None
                else torch.autocast(device_type='cpu', enabled=False))
    meta = {'version': VERSION, 'started_at': now_iso(), 'args': vars(args), 'device': device, 'model_load_s': load_s,
            'freezing': freezing, 'train_images': len(dataset), 'torch': torch.__version__,
            'note': 'losses are training diagnostics; nothing here is accuracy'}
    with open(out / 'run.json', 'w') as handle:
        json.dump(meta, handle, indent=1, sort_keys=True, default=str)
    log = open(out / 'steps.jsonl', 'a' if args.resume else 'w')
    step, micro, epoch, skipped = start_step, 0, 0, 0
    accum, running = [], []
    optimizer.zero_grad(set_to_none=True)
    checkpoints = []
    while step < args.steps:
        epoch += 1
        for batch in loader:
            key, data = next(iter(batch.items()))
            if micro % args.grad_accum == 0:
                t0 = time.monotonic()
            data = copy_data_to_device(data, torch.device(device), non_blocking=False)
            try:
                with autocast:
                    find_stages = model(data)
                    find_targets = [model.back_convert(x) for x in data.find_targets]
                    loss = loss_fn(find_stages, find_targets)
                total, parts = core_loss(loss)
                if not torch.isfinite(total):
                    raise ValueError('non-finite loss')
            except ValueError as error:      # scipy's matcher raises on NaN/inf costs; a non-finite loss is treated the same
                if args.fail_on_nan or 'numeric' not in str(error) and 'non-finite' not in str(error):
                    raise
                skipped += 1
                find_stages = find_targets = loss = None
                log.write(json.dumps({'step': step, 'skipped_batch': skipped, 'reason': str(error)[:120]}) + '\n')
                log.flush()
                optimizer.zero_grad(set_to_none=True)
                accum = []
                micro = 0
                if skipped > args.max_skipped:
                    raise SystemExit(f'{skipped} batches skipped for non-finite costs; stopping (raise --max-skipped to continue)') from error
                continue
            (total / args.grad_accum).backward()
            micro += 1
            accum.append(float(total))
            if micro % args.grad_accum:
                continue
            lr_now = lr_at(step, args.warmup, args.steps, args.lr)
            for group in optimizer.param_groups:
                group['lr'] = lr_now * (args.lr_backbone / args.lr if group.get('name', '').startswith('backbone_') else 1.)
            grad_norm = torch.nn.utils.clip_grad_norm_((p for p in model.parameters() if p.requires_grad),
                                                       args.clip if args.clip > 0 else float('inf'))
            if not torch.isfinite(grad_norm):       # the fork's GradScaler would skip this step; do the same
                optimizer.zero_grad(set_to_none=True)
                skipped += 1
                accum = []
                log.write(json.dumps({'step': step, 'skipped_batch': skipped, 'reason': 'non-finite gradient norm'}) + '\n')
                log.flush()
                if skipped > args.max_skipped:
                    raise SystemExit(f'{skipped} batches skipped for non-finite costs or gradients; stopping')
                continue
            optimizer.step()
            optimizer.zero_grad(set_to_none=True)
            if device == 'mps':
                torch.mps.synchronize()
                # the MPS caching allocator keeps every freed block: with 100 to 200 variable-size mask tensors per
                # image the driver footprint grew to 58 GB over 3,000 steps (allocated stayed at 4 GB) and exhausted
                # swap on 2026-09-29; returning the cache each step costs little and keeps the footprint flat
                torch.mps.empty_cache()
            step += 1
            seconds = round(time.monotonic() - t0, 3)
            running.append(sum(accum) / len(accum))
            row = {'step': step, 'epoch': epoch, 'loss': round(running[-1], 4), 'lr': lr_now, 'seconds': seconds, 'grad_norm': round(float(grad_norm), 4),
                   'images': len(data.img_batch) * args.grad_accum, **memory_mb(device),
                   'parts': {k: round(v, 4) for k, v in parts.items()}}
            accum = []
            log.write(json.dumps(row) + '\n')
            log.flush()
            if step % args.log_every == 0 or step == 1 or args.dry_run:
                print(json.dumps({k: row[k] for k in ('step', 'loss', 'seconds', 'lr') if k in row} | memory_mb(device)), flush=True)
            if args.save_every and step % args.save_every == 0 and not args.dry_run:
                checkpoints.append(save_checkpoint(model, optimizer, step, out, f'step{step:06d}'))
            if step >= args.steps:
                break
    log.close()
    final = None if args.dry_run else save_checkpoint(model, optimizer, step, out, 'final')
    first, last = (running[0], running[-1]) if running else (None, None)
    summary = {**meta, 'finished_at': now_iso(), 'steps': step, 'epochs': epoch, 'skipped_batches': skipped,
               'loss_first': first, 'loss_last': last,
               'loss_median_last_10': (sorted(running[-10:])[len(running[-10:]) // 2] if running else None),
               'checkpoints': checkpoints, 'final_checkpoint': final,
               'seconds_per_step_median': None, 'memory_last': memory_mb(device)}
    rows = [json.loads(line) for line in open(out / 'steps.jsonl')]
    secs = sorted(r['seconds'] for r in rows if 'seconds' in r)
    if secs:
        summary['seconds_per_step_median'] = secs[len(secs) // 2]
    with open(out / 'summary.json', 'w') as handle:
        json.dump(summary, handle, indent=1, sort_keys=True, default=str)
    return summary


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--coco', required=True, help='prepare_coco output root (train/ val/ test/ folders)')
    parser.add_argument('--out', default=str(REPO / 'data/reviews/eyes/training/sam3-mac/run-dry'))
    parser.add_argument('--weights', default=str(WEIGHTS))
    parser.add_argument('--random-init', action='store_true', help='no checkpoint: operator coverage and timing only')
    parser.add_argument('--resume', help='trainable-*.pt from a previous run')
    parser.add_argument('--device', default='mps')
    parser.add_argument('--dtype', choices=('fp32', 'bf16', 'fp16'), default='fp32')
    parser.add_argument('--resolution', type=int, default=1008)
    parser.add_argument('--steps', type=int, default=100)
    parser.add_argument('--batch-size', type=int, default=1)
    parser.add_argument('--grad-accum', type=int, default=1)
    parser.add_argument('--lr', type=float, default=8e-5, help='transformer / heads (fork config: 8e-4 x 0.1)')
    parser.add_argument('--lr-backbone', type=float, default=2.5e-5)
    parser.add_argument('--weight-decay', type=float, default=.1)
    parser.add_argument('--warmup', type=int, default=20)
    parser.add_argument('--clip', type=float, default=.1)
    parser.add_argument('--freeze', nargs='*', default=list(FREEZE_DEFAULT), help='fnmatch patterns of parameter names to freeze')
    parser.add_argument('--train-all', action='store_true')
    parser.add_argument('--workers', type=int, default=0)
    parser.add_argument('--limit', type=int, help='use only the first N training images')
    parser.add_argument('--seed', type=int, default=20260928)
    parser.add_argument('--log-every', type=int, default=10)
    parser.add_argument('--save-every', type=int, default=0)
    parser.add_argument('--dry-run', action='store_true', help='no checkpoints; print every step')
    parser.add_argument('--fail-on-nan', action='store_true', help='stop at the first non-finite cost or loss instead of skipping the batch')
    parser.add_argument('--max-skipped', type=int, default=20, help='stop after this many skipped batches')
    parser.add_argument('--presence-weight', type=float, default=LOSS_DEFAULTS['presence_weight'],
                        help='IABCEMdetr presence_loss weight (image-level presence logit; its target is 1 on every image of a fold without empty images)')
    parser.add_argument('--pos-weight', type=float, default=LOSS_DEFAULTS['pos_weight'],
                        help='IABCEMdetr pos_weight on matched-query BCE (round-4 H6 arm: 3)')
    parser.add_argument('--o2m-weight', type=float, default=LOSS_DEFAULTS['o2m_weight'],
                        help='Sam3LossWrapper o2m_weight on the one-to-many auxiliary losses (round-4 H7 arm: 0)')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.random_init and not Path(args.weights).exists():
        raise SystemExit(f'weights missing at {args.weights}: run scripts/ml/training/sam3_mac/fetch_weights.sh, or pass --random-init')
    summary = train(args)
    json.dump({k: summary[k] for k in ('steps', 'skipped_batches', 'loss_first', 'loss_last', 'seconds_per_step_median', 'memory_last',
                                       'freezing', 'final_checkpoint')}, sys.stdout, indent=1, default=str)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
