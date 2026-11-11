"""Fixed-budget fine-tune of the corner-preserving roof outline model, with a hard budget guard.

    python -m scripts.ml.training.train --config scripts/ml/training/configs/a10.json --run-dir RUN
    python -m scripts.ml.training.train --smoke --dataset DATASET --run-dir RUN       # CPU/MPS, 20 images, 2 steps

Budget guard (every limit is checked before each step and at every evaluation):
  --max-minutes      total training wall time across resumes, minus --reserve-minutes kept for the final
                     held-out evaluation; the learning-rate schedule follows whichever of steps/time binds first
  --max-steps        optimiser steps
  early stop         after --patience evaluations without --min-delta gain in the validation boundary F-score
  SIGTERM / SIGINT   save last.pt and exit 3 (a preempted rented box resumes with --resume auto)
Checkpoints: last.pt every --checkpoint-minutes and at exit, best.pt on every validation gain (atomic writes).
Resume: --resume auto picks up RUN/last.pt; batches are a pure function of (seed, step), so the resumed run sees
exactly the batches the interrupted one would have. Logs: RUN/train_log.jsonl (one JSON object per event).

The validation split holds no human outline, so the early-stop metric is agreement with SAM3 pseudo-labels; the
held-out evaluation at the end (RUN/test_summary.json) is the only boundary statement against people, and it is
reference agreement with a few dozen outlines, not accuracy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import signal
import sys
import time
from pathlib import Path

import numpy as np
import torch

from . import common, evaluate, metrics
from .common import IGNORE, JsonlLogger
from .data import RoofDataset, StepBatchSampler, collate, seed_worker
from .infer import paste_masks
from .model import (
    MODEL_VERSION,
    build_model,
    clear_instance_weights,
    install_weighted_mask_loss,
    predict,
    set_instance_weights,
)

CONFIG_DIR = Path(__file__).resolve().parent / 'configs'
EXIT_INTERRUPTED = 3
DEFAULTS = {
    'dataset': str(common.DEFAULT_OUT / 'dataset-v1'), 'run_dir': None, 'seed': 20260927, 'device': 'auto',
    'pretrained': 'coco', 'target': 'raw', 'crop': 1024, 'batch_size': 4, 'workers': 6, 'max_train_images': None,
    'lr': .01, 'momentum': .9, 'weight_decay': 1e-4, 'warmup_steps': 300, 'grad_clip': 10., 'schedule': 'budget',
    'max_steps': 4000, 'max_minutes': 90., 'reserve_minutes': 12., 'checkpoint_minutes': 10.,
    'eval_every_steps': 500, 'eval_every_minutes': 20., 'patience': 4, 'min_delta': .002,
    'val_images': 49, 'val_score_threshold': .5, 'test_images': None, 'test_score_threshold': .45,
    'log_every': 20, 'amp': 'bf16', 'mask_resolution': 56, 'boundary_weight': 3.,
    'anchor_sizes': [16, 32, 64, 128, 256], 'detections_per_img': 200, 'trainable_backbone_layers': 5,
    'skip_test': False, 'colour_jitter': .1,
}
SMOKE = {'max_train_images': 20, 'max_steps': 2, 'batch_size': 2, 'crop': 384, 'pretrained': 'none', 'device': 'cpu',
         'workers': 0, 'val_images': 2, 'test_images': 2, 'eval_every_steps': 0, 'eval_every_minutes': 0.,
         'max_minutes': 20., 'reserve_minutes': 5., 'warmup_steps': 1, 'log_every': 1, 'schedule': 'steps',
         'amp': 'none', 'detections_per_img': 50}
MODEL_KEYS = ('pretrained', 'mask_resolution', 'anchor_sizes', 'detections_per_img', 'trainable_backbone_layers',
              'boundary_weight')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', help='JSON config (configs/a10.json, a100.json, smoke.json); CLI flags override it.')
    parser.add_argument('--smoke', action='store_true', help='20 images, 2 steps, CPU, random init: proves the loop runs.')
    parser.add_argument('--resume', default='auto', help="'auto' (RUN/last.pt when present), a checkpoint path, or 'none'.")
    for key, value in DEFAULTS.items():
        flag = '--' + key.replace('_', '-')
        if isinstance(value, bool):
            parser.add_argument(flag, dest=key, action='store_true', default=None)
        elif isinstance(value, list):
            parser.add_argument(flag, dest=key, type=int, nargs='+', default=None)
        else:
            kind = type(value) if value is not None else str
            kind = int if key in ('max_train_images', 'test_images') else kind
            parser.add_argument(flag, dest=key, type=kind, default=None)
    args = parser.parse_args(argv)
    config = dict(DEFAULTS)
    if args.config:
        config.update(json.loads(Path(args.config).read_text()))
    if args.smoke:
        config.update(SMOKE)
    config.update({k: v for k, v in vars(args).items() if k in DEFAULTS and v is not None})
    config['smoke'] = bool(args.smoke)
    config['config_file'] = args.config
    if config['target'] not in ('raw', 'regularised'):
        parser.error('--target must be raw or regularised')
    return config, args.resume


def resolve_device(name):
    if name == 'auto':
        if torch.cuda.is_available():
            return torch.device('cuda')
        if torch.backends.mps.is_available():
            return torch.device('mps')
        return torch.device('cpu')
    return torch.device(name)


def set_determinism(seed):
    os.environ.setdefault('CUBLAS_WORKSPACE_CONFIG', ':4096:8')
    random.seed(seed)
    np.random.seed(seed)  # noqa: NPY002
    torch.manual_seed(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    # roi_align's CUDA backward uses atomics: identical batches and init, not bit-identical gradients.
    torch.use_deterministic_algorithms(True, warn_only=True)


def default_run_dir(config):
    manifest = Path(config['dataset']) / 'manifest.json'
    tag = common.sha256_file(manifest)[:8] if manifest.exists() else 'nodata'
    name = 'smoke' if config['smoke'] else Path(config['config_file'] or 'default').stem
    return common.DEFAULT_OUT / 'runs' / f"{name}-{config['target']}-{tag}-s{config['seed']}"


def learning_rate(config, step, elapsed_s):
    base = config['lr'] * config['batch_size'] / 8.
    if step < config['warmup_steps']:
        return base * (.001 + .999 * step / max(1, config['warmup_steps']))
    progress = step / max(1, config['max_steps'])
    if config['schedule'] == 'budget':
        budget = max(1., (config['max_minutes'] - config['reserve_minutes']) * 60.)
        progress = max(progress, elapsed_s / budget)
    return base * .5 * (1. + math.cos(math.pi * min(1., progress)))


def save_checkpoint(path, model, optimizer, state, config, manifest_sha):
    payload = {'model': model.state_dict(), 'optimizer': optimizer.state_dict(), 'state': dict(state), 'config': config,
               'model_config': model.addr_config, 'model_version': MODEL_VERSION, 'dataset_manifest_sha256': manifest_sha,
               'code_sha256': common.code_sha256(), 'saved_at': common.now_iso(),
               'rng': {'torch': torch.get_rng_state(), 'cuda': torch.cuda.get_rng_state_all() if torch.cuda.is_available() else None}}
    temporary = Path(path).with_suffix('.tmp')
    torch.save(payload, temporary)
    os.replace(temporary, path)
    return path


def _gt_labels(label):
    labels = label.astype(np.int64)
    ignore = label == IGNORE
    labels[ignore] = 0
    return labels, ignore


def _pred_labels(masks, scores, shape):
    labels = np.zeros(shape, dtype=np.int64)
    for rank, index in enumerate(np.argsort(scores, kind='stable'), start=1):  # higher scores paint last and win
        labels[masks[index]] = rank
    return labels


@torch.no_grad()
def validate(model, dataset, device, config, limit):
    """Mean image-level instance-boundary F@0.5 m and foreground IoU against pseudo-labels (IGNORE excluded)."""
    model.eval()
    rows = []
    for index in range(min(len(dataset), limit)):
        meta, image, label = dataset.load(index)
        tensor = torch.from_numpy(np.ascontiguousarray(image.transpose(2, 0, 1)) / 255.).float()
        boxes, scores, probs = predict(model, tensor, device)
        keep = scores >= config['val_score_threshold']
        masks = paste_masks(probs[keep], boxes[keep], *label.shape)
        target, ignore = _gt_labels(label)
        pred = _pred_labels(masks, scores[keep], label.shape)
        result = metrics.label_boundary_f(pred, target, meta['pixel_size_m'], ignore)
        valid = ~ignore
        union = ((pred > 0) | (target > 0)) & valid
        iou = float((((pred > 0) & (target > 0)) & valid).sum() / union.sum()) if union.any() else None
        rows.append({'scene_id': meta['scene_id'], 'boundary_f': result['boundary_f'], 'iou': iou, 'n_pred': int(keep.sum())})
    model.train()
    f = [r['boundary_f'] for r in rows if r['boundary_f'] is not None]
    ious = [r['iou'] for r in rows if r['iou'] is not None]
    return {'val_boundary_f': float(np.mean(f)) if f else None, 'val_iou': float(np.mean(ious)) if ious else None,
            'n_images': len(rows), 'n_scored': len(f)}


def torch_predictor(model, device, threshold):
    def run(image, _refs):
        tensor = torch.from_numpy(np.asarray(image, dtype=np.float32).transpose(2, 0, 1).copy() / 255.)
        boxes, scores, probs = predict(model, tensor, device)
        keep = scores >= threshold
        masks = paste_masks(probs[keep], boxes[keep], image.height, image.width)
        return list(zip(masks, scores[keep].tolist(), strict=True))
    return run


class Stop:
    signal = None

    @classmethod
    def handler(cls, signum, _frame):
        cls.signal = signal.Signals(signum).name


def versions(device):
    info = {'python': platform.python_version(), 'torch': torch.__version__, 'platform': platform.platform(),
            'device': str(device)}
    import torchvision
    info['torchvision'] = torchvision.__version__
    if device.type == 'cuda':
        info['gpu'] = torch.cuda.get_device_name(device)
        info['cuda'] = torch.version.cuda
    return info


def train(config, resume='auto'):
    set_determinism(config['seed'])
    device = resolve_device(config['device'])
    run_dir = Path(config['run_dir'] or default_run_dir(config))
    run_dir.mkdir(parents=True, exist_ok=True)
    dataset_dir = Path(config['dataset'])
    manifest_sha = common.sha256_file(dataset_dir / 'manifest.json')
    run_id = hashlib.sha256(f"{run_dir.resolve()}:{manifest_sha}:{config['seed']}".encode()).hexdigest()[:12]
    log = JsonlLogger(run_dir / 'train_log.jsonl', run_id)
    signal.signal(signal.SIGTERM, Stop.handler)
    signal.signal(signal.SIGINT, Stop.handler)

    install_weighted_mask_loss()
    model = build_model({k: config[k] for k in MODEL_KEYS}).to(device)
    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.SGD(params, lr=config['lr'], momentum=config['momentum'], weight_decay=config['weight_decay'])
    state = {'step': 0, 'elapsed_s': 0., 'best_metric': None, 'best_step': None, 'evals_since_best': 0, 'n_evals': 0,
             'nonfinite': 0, 'last_eval_elapsed_s': 0., 'last_eval_step': 0}

    checkpoint_path = run_dir / 'last.pt' if resume == 'auto' else (None if resume == 'none' else Path(resume))
    if checkpoint_path is not None and checkpoint_path.exists():
        checkpoint = torch.load(checkpoint_path, map_location=device, weights_only=False)
        if checkpoint['dataset_manifest_sha256'] != manifest_sha:
            raise SystemExit(f'{checkpoint_path} was trained on another dataset export; refusing to resume')
        model.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        state.update(checkpoint['state'])
        torch.set_rng_state(checkpoint['rng']['torch'].cpu())
        if checkpoint['rng'].get('cuda') and torch.cuda.is_available():
            torch.cuda.set_rng_state_all(checkpoint['rng']['cuda'])
        log.log('resume', checkpoint=str(checkpoint_path), **state)
    elif resume not in ('auto', 'none'):
        raise SystemExit(f'no checkpoint at {resume}')

    train_set = RoofDataset(dataset_dir, 'train', crop=config['crop'], train=True, target=config['target'],
                            max_images=config['max_train_images'], colour_jitter=config['colour_jitter'])
    val_set = RoofDataset(dataset_dir, 'val', train=False)
    val_source = 'val'
    if len(val_set) == 0 and config['smoke']:
        val_set, val_source = RoofDataset(dataset_dir, 'train', train=False, max_images=config['val_images']), 'train (smoke only: val split empty)'
    if len(train_set) == 0:
        raise SystemExit('the training split has no image with a target instance')
    sampler = StepBatchSampler(train_set.image_weights(), config['batch_size'], config['seed'], state['step'], config['max_steps'])
    loader = torch.utils.data.DataLoader(
        train_set, batch_sampler=sampler, collate_fn=collate, num_workers=config['workers'], worker_init_fn=seed_worker,
        pin_memory=device.type == 'cuda', persistent_workers=config['workers'] > 0)
    use_amp = device.type == 'cuda' and config['amp'] == 'bf16'
    if len(val_set) == 0 or not config['val_images']:
        log.log('warning', message='no validation images: early stopping is off and last.pt is the exported checkpoint')
    log.log('start', config=config, device=str(device), versions=versions(device), run_dir=str(run_dir),
            dataset_manifest_sha256=manifest_sha, n_train_images=len(train_set), n_val_images=len(val_set),
            val_source=val_source, amp=use_amp, model_version=MODEL_VERSION, code_sha256=common.code_sha256())

    budget_s = max(0., (config['max_minutes'] - config['reserve_minutes']) * 60.)
    started, elapsed_at_start = time.monotonic(), state['elapsed_s']
    last_checkpoint = time.monotonic()
    window_start, window_images = time.monotonic(), 0
    stop_reason = None

    def elapsed():
        return elapsed_at_start + (time.monotonic() - started)

    def run_eval(reason):
        result = validate(model, val_set, device, config, config['val_images'])
        state['n_evals'] += 1
        state['last_eval_elapsed_s'], state['last_eval_step'] = elapsed(), state['step']
        metric = result['val_boundary_f']
        improved = metric is not None and (state['best_metric'] is None or metric > state['best_metric'] + config['min_delta'])
        if improved:
            state.update(best_metric=metric, best_step=state['step'], evals_since_best=0)
            save_checkpoint(run_dir / 'best.pt', model, optimizer, state, config, manifest_sha)
        elif metric is not None:
            state['evals_since_best'] += 1
        log.log('eval', reason=reason, step=state['step'], improved=improved, val_source=val_source, **result,
                best_metric=state['best_metric'], best_step=state['best_step'], evals_since_best=state['evals_since_best'])

    model.train()
    batches = iter(loader)
    while True:
        state['elapsed_s'] = elapsed()
        if Stop.signal:
            stop_reason = f'signal:{Stop.signal}'
        elif state['step'] >= config['max_steps']:
            stop_reason = 'max_steps'
        elif state['elapsed_s'] >= budget_s:
            stop_reason = 'max_minutes'
        elif config['patience'] and state['evals_since_best'] >= config['patience']:
            stop_reason = 'early_stop'
        if stop_reason:
            break
        images, targets = next(batches)
        lr = learning_rate(config, state['step'], state['elapsed_s'])
        for group in optimizer.param_groups:
            group['lr'] = lr
        images = [image.to(device, non_blocking=True) for image in images]
        device_targets = [{k: t[k].to(device) for k in ('boxes', 'labels', 'masks')} for t in targets]
        set_instance_weights([t['weights'].to(device) for t in targets], config['boundary_weight'])
        with torch.autocast(device_type=device.type, dtype=torch.bfloat16, enabled=use_amp):
            losses = model(images, device_targets)
        clear_instance_weights()
        loss = sum(losses.values())
        state['step'] += 1
        if not torch.isfinite(loss):
            state['nonfinite'] += 1
            optimizer.zero_grad(set_to_none=True)
            log.log('nonfinite', step=state['step'], losses={k: float(v) for k, v in losses.items()})
            if state['nonfinite'] > 10:
                stop_reason = 'nonfinite_loss'
                break
            continue
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        grad_norm = torch.nn.utils.clip_grad_norm_(params, config['grad_clip'])
        optimizer.step()
        window_images += len(images)
        if state['step'] % config['log_every'] == 0 or state['step'] == 1:
            now = time.monotonic()
            log.log('step', step=state['step'], lr=lr, loss=float(loss.detach()), grad_norm=float(grad_norm),
                    losses={k: round(float(v.detach()), 5) for k, v in losses.items()},
                    images_per_s=round(window_images / max(now - window_start, 1e-9), 3), train_elapsed_s=round(elapsed(), 1),
                    instances=int(sum(len(t['labels']) for t in targets)))
            window_start, window_images = now, 0
        due_steps = config['eval_every_steps'] and state['step'] - state['last_eval_step'] >= config['eval_every_steps']
        due_time = config['eval_every_minutes'] and elapsed() - state['last_eval_elapsed_s'] >= config['eval_every_minutes'] * 60
        if due_steps or due_time:
            run_eval('schedule')
        if (time.monotonic() - last_checkpoint) >= config['checkpoint_minutes'] * 60:
            state['elapsed_s'] = elapsed()
            save_checkpoint(run_dir / 'last.pt', model, optimizer, state, config, manifest_sha)
            log.log('checkpoint', path=str(run_dir / 'last.pt'), step=state['step'])
            last_checkpoint = time.monotonic()

    if state['last_eval_step'] != state['step'] and not stop_reason.startswith('signal'):
        run_eval('final')
    state['elapsed_s'] = elapsed()
    save_checkpoint(run_dir / 'last.pt', model, optimizer, state, config, manifest_sha)
    log.log('stop', reason=stop_reason, **state)
    if stop_reason.startswith('signal'):
        return {'stop_reason': stop_reason, 'run_dir': str(run_dir), **state}, EXIT_INTERRUPTED

    selected = 'best.pt' if (run_dir / 'best.pt').exists() else 'last.pt'
    weights = torch.load(run_dir / selected, map_location=device, weights_only=False)['model']
    model.load_state_dict(weights)
    model.eval()
    test = None
    if not config['skip_test'] and (dataset_dir / 'references.json').exists():
        rows, timing = evaluate.evaluate_references(dataset_dir, torch_predictor(model, device, config['test_score_threshold']),
                                                    max_images=config['test_images'])
        test = evaluate.summarise(rows)
        evaluate.write_report(run_dir, rows, test, {'checkpoint': selected, 'timing': timing,
                                                    'dataset_manifest_sha256': manifest_sha})
        log.log('test', checkpoint=selected, n_rows=len(rows), timing=timing)
    summary = {'run_dir': str(run_dir), 'run_id': run_id, 'stop_reason': stop_reason, 'selected_checkpoint': selected,
               'state': state, 'config': config, 'versions': versions(device), 'dataset_manifest_sha256': manifest_sha,
               'model_version': MODEL_VERSION, 'code_sha256': common.code_sha256(), 'val_source': val_source,
               'checkpoints': {name: common.sha256_file(run_dir / name) for name in ('last.pt', 'best.pt') if (run_dir / name).exists()},
               'test_brief': _brief(test), 'note': common.NOTE, 'finished_at': common.now_iso()}
    common.write_json_atomic(run_dir / 'summary.json', summary)
    log.log('done', stop_reason=stop_reason, selected=selected, train_elapsed_s=round(state['elapsed_s'], 1))
    return summary, 0


def _brief(test):
    if not test:
        return None
    out = {}
    for stratum, block in test['strata'].items():
        out[stratum] = {method: {'n_references': v['n_references'], 'n_matched': v['n_matched'],
                                 'hausdorff_m_median': v['hausdorff_m']['median'],
                                 'vertex_distance_median_m': v['vertex_distance_median_m']['median'],
                                 'boundary_f_median': v['boundary_f']['median'], 'iou_median': v['iou']['median'],
                                 'n_hausdorff_le_0_45_m': v['n_hausdorff_le_0_45_m']}
                        for method, v in block.items() if 'minus' not in method}
    return out


def main(argv=None):
    config, resume = parse_args(argv)
    summary, code = train(config, resume)
    json.dump({k: summary.get(k) for k in ('run_dir', 'stop_reason', 'selected_checkpoint', 'state', 'test_brief')},
              sys.stdout, indent=1, default=str)
    sys.stdout.write('\n')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
