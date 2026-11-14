"""Explicit hardware selection: requesting TPU must never succeed on CPU."""
from __future__ import annotations

import importlib.metadata as metadata
import os
import platform
import subprocess
import sys

import torch


def select_device(kind: str):
    if kind == 'cpu':
        torch.set_num_threads(min(4, torch.get_num_threads()))
        return torch.device('cpu')
    if kind == 'cuda':
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA requested but no CUDA device is available')
        return torch.device('cuda')
    if kind != 'tpu':
        raise ValueError('Choose cpu, cuda or tpu explicitly')
    try:
        xla_version = metadata.version('torch_xla')
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError('TPU requested: torch_xla is missing. Run kaggle/bootstrap.sh tpu.') from exc
    if torch.__version__.split('+')[0].split('.')[:2] != xla_version.split('+')[0].split('.')[:2]:
        raise RuntimeError(f'torch {torch.__version__} / torch_xla {xla_version} version mismatch')
    if os.environ.get('PJRT_DEVICE', 'TPU') != 'TPU':
        raise RuntimeError('TPU requested but PJRT_DEVICE is not TPU')
    os.environ['PJRT_DEVICE'] = 'TPU'
    import torch_xla
    import torch_xla.runtime as xr
    device = torch_xla.device()
    if xr.device_type() != 'TPU' or device.type != 'xla':
        raise RuntimeError('No real TPU runtime detected; refusing CPU fallback')
    if xr.world_size() != 1:
        raise RuntimeError('This first bench is single-worker; distributed training is not configured')
    return device


def sync(device):
    if device.type == 'xla':
        import torch_xla
        torch_xla.sync(wait=True)
    elif device.type == 'cuda':
        torch.cuda.synchronize(device)


def environment(device) -> dict:
    versions = {}
    for name in ('torch', 'torchvision', 'torch_xla', 'libtpu', 'timm', 'numpy', 'Pillow', 'scipy'):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    try:
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL,
                                           text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = None
    info = dict(python=sys.version, platform=platform.platform(), versions=versions,
                device=str(device), addr_revision=revision, worker_count=1)
    if device.type == 'xla':
        import torch_xla.runtime as xr
        info.update(xla_device_type=xr.device_type(),
                    global_runtime_device_count=xr.global_runtime_device_count())
    if device.type == 'cuda':
        info['device_name'] = torch.cuda.get_device_name(device)
    return info


def xla_diagnostics(device) -> dict:
    if device.type != 'xla':
        return {}
    import torch_xla.debug.metrics as met
    fallbacks = {name: met.counter_value(name) for name in met.counter_names()
                 if name.startswith('aten::')}
    return {'metrics_report': met.metrics_report(), 'aten_counters': fallbacks,
            'requires_fallback_review': bool(fallbacks)}


def cpu_tree(value):
    if isinstance(value, torch.Tensor):
        return value.detach().cpu()
    if isinstance(value, dict):
        return {k: cpu_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [cpu_tree(v) for v in value]
    if isinstance(value, tuple):
        return tuple(cpu_tree(v) for v in value)
    return value
