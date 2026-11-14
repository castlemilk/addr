"""Run with python -m experiments.rooftam.bench --help from repository root."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import traceback

import numpy as np
from PIL import Image
import torch
import torch.nn.functional as F

from .data import dataset_digest, digest, load_manifest, load_sample, make_fixture, restore_logits
from .metrics import score_masks, summarize
from .models import EDGETAM_REV, EdgeRoof, TinyControl, load_trainable, segmentation_loss, trainable_state
from .runtime import cpu_tree, environment, select_device, sync, xla_diagnostics


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, allow_nan=False)+'\n')


def prepare_output(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise ValueError(f'Output must be empty; use a new run directory: {path}')
    return path


def make_model(args, device):
    torch.manual_seed(args.seed)
    if args.model == 'edgetam':
        if args.side != 1024:
            raise ValueError('EdgeTAM side must be 1024; only tiny control supports smaller sizes')
        model = EdgeRoof(args.checkpoint)
    else:
        model = TinyControl()
    model = model.to(device)
    # Math attention only; no CUDA flash kernels in the TPU experiment.
    if device.type == 'xla':
        torch.backends.cuda.enable_flash_sdp(False)
        torch.backends.cuda.enable_mem_efficient_sdp(False)
        torch.backends.cuda.enable_math_sdp(True)
    if args.adapter:
        saved = torch.load(args.adapter, map_location='cpu', weights_only=True)
        expected = digest(args.checkpoint) if args.model == 'edgetam' else None
        if saved['model'] != args.model or saved['base_checkpoint_sha256'] != expected or saved['side'] != args.side:
            raise ValueError('Adapter was trained against a different model/checkpoint/input size')
        load_trainable(model, saved['weights'])
    return model


def metadata_for(args, rows, device):
    synthetic = rows[0]['label_status'] == 'synthetic'
    return dict(schema_version=1, command=args.command, configuration=vars(args),
                dataset_sha256=dataset_digest(rows), manifest_sha256=digest(args.manifest),
                synthetic=synthetic, evidence='PLUMBING ONLY' if synthetic else 'human-reviewed roof masks',
                task='box-prompted visible roof segmentation; not roof structure or detector recall',
                edgetam_revision=EDGETAM_REV if args.model == 'edgetam' else None,
                base_checkpoint_sha256=digest(args.checkpoint) if args.model == 'edgetam' else None,
                adapter_sha256=digest(args.adapter) if args.adapter else None,
                environment=environment(device))


def batch(rows, side, device):
    loaded = [load_sample(r, side) for r in rows]
    return tuple(torch.stack([sample[i] for sample in loaded]).to(device) for i in range(4))


def preflight(args, out):
    device = select_device(args.device)
    started = time.perf_counter()
    a = torch.ones((16, 16), device=device, requires_grad=True)
    loss = (a @ a).mean()
    loss.backward()
    sync(device)
    value = float(loss.detach().cpu())
    if value != 16.0 or not torch.isfinite(a.grad.cpu()).all():
        raise RuntimeError('Hardware arithmetic/gradient preflight failed')
    write_json(out/'preflight.json', dict(status='passed', stage='arithmetic only, not EdgeTAM',
               seconds=time.perf_counter()-started, environment=environment(device),
               xla=xla_diagnostics(device)))


def train(args, out):
    if args.steps < 1 or args.batch_size < 1 or args.lr <= 0:
        raise ValueError('steps, batch-size and learning rate must be positive')
    rows = load_manifest(args.manifest, args.allow_synthetic)
    selected = [r for r in rows if r['split'] == 'train']
    if not selected:
        raise ValueError('No train samples')
    device = select_device(args.device)
    report = metadata_for(args, rows, device)
    write_json(out/'run.json', {**report, 'status': 'running'})
    model = make_model(args, device).train()
    initial = trainable_state(model)
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                                 lr=args.lr, foreach=False)
    order = np.random.default_rng(args.seed).permutation(len(selected))
    history = []
    for step in range(args.steps):
        # Fixed batch shape, including the final/repeated batch, avoids XLA recompiles.
        examples = [selected[order[(step*args.batch_size+i) % len(order)]] for i in range(args.batch_size)]
        images, targets, valid, boxes = batch(examples, args.side, device)
        optimizer.zero_grad(set_to_none=True)
        sync(device)
        started = time.perf_counter()
        logits = model(images, boxes)
        loss = segmentation_loss(logits, targets, valid)
        loss.backward()
        optimizer.step()
        sync(device)
        seconds = time.perf_counter()-started
        value = float(loss.detach().cpu())
        if not np.isfinite(value):
            raise RuntimeError('Non-finite training loss')
        history.append(dict(step=step+1, loss=value, compute_seconds=seconds))
        with (out/'train.jsonl').open('a') as f:
            f.write(json.dumps(history[-1])+'\n')
    updated = trainable_state(model)
    changed = any(not torch.equal(initial[k], updated[k]) for k in initial)
    if not changed or not all(torch.isfinite(v).all() for v in updated.values()):
        raise RuntimeError('Decoder/control weights did not update or became non-finite')
    state = dict(model=args.model, side=args.side, base_checkpoint_sha256=report['base_checkpoint_sha256'],
                 weights=updated, optimizer=cpu_tree(optimizer.state_dict()), steps=args.steps,
                 dataset_sha256=report['dataset_sha256'], seed=args.seed)
    torch.save(state, out/'adapter.pt')
    warm = [r['compute_seconds'] for r in history[args.warmup:]]
    report.update(status='passed', steps=args.steps, train_samples=len(selected),
                  trainable_parameters=sum(p.numel() for p in model.parameters() if p.requires_grad),
                  weights_changed=True, initial_loss=history[0]['loss'], final_loss=history[-1]['loss'],
                  first_step_seconds=history[0]['compute_seconds'],
                  warm_step_p50_seconds=float(np.median(warm)) if warm else None,
                  warm_step_p95_seconds=float(np.percentile(warm,95)) if warm else None,
                  timing_scope='synchronized model/loss/backward/optimizer; excludes data I/O and checkpointing',
                  adapter_sha256=digest(out/'adapter.pt'), xla=xla_diagnostics(device))
    write_json(out/'run.json', report)


def evaluate(args, out):
    rows = load_manifest(args.manifest, args.allow_synthetic)
    selected = [r for r in rows if r['split'] == args.split]
    if not selected:
        raise ValueError(f'No {args.split} samples')
    device = select_device(args.device)
    report = metadata_for(args, rows, device)
    model = make_model(args, device).eval()
    results = []
    (out/'predictions').mkdir()
    (out/'overlays').mkdir()
    with torch.no_grad():
        # Warmup has the same fixed batch/shape as evaluation; no GT passed to model.
        first = load_sample(selected[0], args.side)
        for _ in range(args.warmup):
            model(first[0][None].to(device), first[3][None].to(device))
            sync(device)
        for row in selected:
            image, _, _, box, geometry = load_sample(row, args.side)
            image, box = image[None].to(device), box[None].to(device)
            sync(device)
            started = time.perf_counter()
            logits = model(image, box)
            sync(device)
            elapsed = time.perf_counter()-started
            pred = restore_logits(logits[0,0].cpu(), geometry, args.side) > 0
            truth = np.asarray(Image.open(row['mask_path'])) > 0
            Image.fromarray(pred.astype(np.uint8)*255).save(out/'predictions'/f"{row['id']}.png")
            from scipy.ndimage import binary_erosion
            overlay = np.asarray(Image.open(row['image_path']).convert('RGB')).copy()
            overlay[truth & ~binary_erosion(truth, border_value=0)] = (255, 165, 0)
            overlay[pred & ~binary_erosion(pred, border_value=0)] = (0, 220, 255)
            Image.fromarray(overlay).save(out/'overlays'/f"{row['id']}.png")
            result = dict(id=row['id'], image_sha256=row['image_sha256'], mask_sha256=row['mask_sha256'],
                          model_seconds=elapsed, **score_masks(pred, truth, row['gsd_m']))
            results.append(result)
    report.update(status='passed', split=args.split, summary=summarize(results), samples=results,
                  prediction_dir='predictions',
                  model_latency_p50_seconds=float(np.median([r['model_seconds'] for r in results])),
                  model_latency_p95_seconds=float(np.percentile([r['model_seconds'] for r in results],95)),
                  timing_scope='synchronized model only; excludes image I/O, inverse transform and scoring',
                  xla=xla_diagnostics(device))
    write_json(out/'evaluation.json', report)


def score_export(args, out):
    rows = load_manifest(args.manifest, args.allow_synthetic)
    selected = [r for r in rows if r['split'] == args.split]
    baseline = json.loads(Path(args.predictions).read_text())
    if baseline['dataset_sha256'] != dataset_digest(rows):
        raise ValueError('Baseline was exported against a different dataset/content/split')
    records = baseline['samples']
    ids = [r['id'] for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate baseline sample ids')
    index = {r['id']:r for r in records}
    results = []
    for row in selected:
        if row['id'] not in index or index[row['id']]['image_sha256'] != row['image_sha256']:
            raise ValueError(f"Missing/stale baseline: {row['id']}")
        record = index[row['id']]
        from .data import local_file
        pred = np.asarray(Image.open(local_file(Path(args.predictions).parent, record['mask']))) > 0
        truth = np.asarray(Image.open(row['mask_path'])) > 0
        results.append(dict(id=row['id'], image_sha256=row['image_sha256'], mask_sha256=row['mask_sha256'],
                            **score_masks(pred, truth, row['gsd_m'])))
    write_json(out/'evaluation.json', dict(status='passed', schema_version=1,
        dataset_sha256=dataset_digest(rows), split=args.split,
        synthetic=rows[0]['label_status']=='synthetic', source=baseline['source'],
        summary=summarize(results), samples=results,
        timing_note='ONNX baseline latency is not TPU model latency; do not infer a speedup'))


def compare(args, out):
    reports = [json.loads(Path(p).read_text()) for p in args.reports]
    key = lambda r: (r['dataset_sha256'], r['split'], r['synthetic'],
                     sorted((s['id'],s['image_sha256'],s['mask_sha256']) for s in r['samples']))
    if len(reports) < 2 or any(r['status']!='passed' or key(r)!=key(reports[0]) for r in reports):
        raise ValueError('Compare at least two successful runs on identical samples, labels and split')
    write_json(out/'comparison.json', dict(synthetic=reports[0]['synthetic'],
        warning='Synthetic results validate plumbing, not roof quality. Cross-device timings are not speedups.',
        reports=[dict(path=p, summary=r['summary'],
                      delta_iou_from_first=r['summary']['iou']-reports[0]['summary']['iou'])
                 for p,r in zip(args.reports,reports)]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('fixture','validate','preflight','train','evaluate','score-export','compare'))
    parser.add_argument('--output', required=True)
    parser.add_argument('--manifest')
    parser.add_argument('--allow-synthetic', action='store_true')
    parser.add_argument('--model', choices=('tiny','edgetam'), default='edgetam')
    parser.add_argument('--device', choices=('cpu','cuda','tpu'), default='cpu')
    parser.add_argument('--checkpoint')
    parser.add_argument('--adapter', help='Warm-start weights; does not resume the optimizer or scheduler')
    parser.add_argument('--side', type=int, default=1024)
    parser.add_argument('--steps', type=int, default=3)
    parser.add_argument('--batch-size', type=int, default=1)
    parser.add_argument('--lr', type=float, default=1e-4)
    parser.add_argument('--seed', type=int, default=17)
    parser.add_argument('--warmup', type=int, default=1)
    parser.add_argument('--split', choices=('val','test'), default='val')
    parser.add_argument('--predictions', help='SAM3/other baseline predictions.json')
    parser.add_argument('--reports', nargs='+')
    args = parser.parse_args(argv)
    if args.side < 16 or args.warmup < 0:
        parser.error('side must be >=16 and warmup non-negative')
    if args.command in ('validate','train','evaluate','score-export') and not args.manifest:
        parser.error('--manifest is required')
    if args.command in ('train','evaluate') and args.model == 'edgetam' and not args.checkpoint:
        parser.error('--checkpoint is required for EdgeTAM')
    if args.command == 'score-export' and not args.predictions:
        parser.error('--predictions is required')
    if args.command == 'compare' and not args.reports:
        parser.error('--reports is required')
    if args.command == 'fixture':
        print(make_fixture(args.output, args.seed))
        return
    out = prepare_output(args.output)
    try:
        if args.command == 'validate':
            rows = load_manifest(args.manifest, args.allow_synthetic)
            write_json(out/'validation.json', dict(status='passed', n=len(rows),
                dataset_sha256=dataset_digest(rows), splits={s:sum(r['split']==s for r in rows) for s in ('train','val','test')},
                synthetic=rows[0]['label_status']=='synthetic'))
        else:
            {'preflight':preflight, 'train':train, 'evaluate':evaluate,
             'score-export':score_export, 'compare':compare}[args.command](args,out)
    except Exception as exc:
        write_json(out/'failure.json', dict(status='failed', command=args.command,
                   error=type(exc).__name__, message=str(exc), traceback=traceback.format_exc()))
        raise
    print(f'{args.command}: artifacts written to {out}')


if __name__ == '__main__':
    main()
