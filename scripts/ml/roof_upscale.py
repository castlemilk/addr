"""Optional, source-bound Real-ESRGAN roof crops for candidate discovery only.

The saved RGB remains the evidence/acceptance image. This module never replaces
source imagery, rewrites scene geometry, or treats generated detail as a label.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import math
import os
import platform
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

VERSION = 'roof-upscale-realesrgan-v1'
RELEASE_TAG = 'v0.2.5.0'
RELEASE_URL = 'https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.2.5.0'
ARCHIVE_URL = ('https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/'
               'realesrgan-ncnn-vulkan-20220424-macos.zip')
# Pinned after downloading the named official artifact; not an upstream checksum claim.
ARCHIVE_SHA256 = 'e0ad05580abfeb25f8d8fb55aaf7bedf552c375b5b4d9bd3c8d59764d2cc333a'
MODEL_NAME = 'realesrgan-x4plus'
SCALE = 4
DEFAULT_INSTALL = Path('data/reviews/eyes/tools/realesrgan-20220424/macos')
DEFAULT_CACHE = Path('data/reviews/eyes/roof-upscale')
MAX_SOURCE_PIXELS = 32 * 1024 * 1024
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_CROP_SIDE = 1024
TILE_SIZE = 128


class UpscaleError(ValueError):
    """Unavailable runtime, invalid binding or failed inference; never a resize fallback."""


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def file_sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _architecture(executable):
    """Refuse an incompatible native binary before launching inference."""
    try:
        result = subprocess.run(['file', '-b', str(executable)], capture_output=True, text=True,
                                timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise UpscaleError('Could not inspect the Real-ESRGAN executable architecture.') from error
    description = result.stdout.strip()
    system, machine = platform.system(), platform.machine().lower()
    aliases = {'aarch64': 'arm64', 'amd64': 'x86_64'}
    machine = aliases.get(machine, machine)
    if system == 'Darwin' and ('Mach-O' not in description or machine not in description):
        raise UpscaleError(f'Real-ESRGAN binary is not native-compatible with {system} {machine}: {description}')
    if system == 'Linux' and ('ELF' not in description or (machine == 'x86_64' and 'x86-64' not in description)
                              or (machine == 'arm64' and 'aarch64' not in description.lower())):
        raise UpscaleError(f'Real-ESRGAN binary is not native-compatible with {system} {machine}: {description}')
    if system not in ('Darwin', 'Linux'):
        raise UpscaleError(f'This bounded local adapter does not support {system}.')
    return {'system': system, 'machine': machine, 'binary': description}


def install_archive(archive, destination=DEFAULT_INSTALL):
    """Explicit local install of the pinned portable release; never auto-download at inference."""
    archive, destination = Path(archive).resolve(), Path(destination).resolve()
    if not ARCHIVE_SHA256 or file_sha256(archive) != ARCHIVE_SHA256:
        raise UpscaleError('The archive does not match the pinned official macOS release artifact.')
    if platform.system() != 'Darwin':
        raise UpscaleError('This archive is for macOS. Configure a native executable on other platforms.')
    if destination.exists():
        raise UpscaleError('Install directory already exists; keep it or choose a new destination.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.realesrgan-install-', dir=destination.parent) as temp:
        unpacked = Path(temp)
        with zipfile.ZipFile(archive) as bundle:
            if sum(info.file_size for info in bundle.infolist()) > 512 * 1024 * 1024:
                raise UpscaleError('Portable release has an excessive unpacked size.')
            for info in bundle.infolist():
                target = (unpacked / info.filename).resolve()
                if not target.is_relative_to(unpacked) or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise UpscaleError('Portable archive contains an unsafe path or symlink.')
            bundle.extractall(unpacked)
        executables = list(unpacked.rglob('realesrgan-ncnn-vulkan'))
        if len(executables) != 1:
            raise UpscaleError('Portable archive does not contain one unambiguous executable.')
        executable = executables[0]
        executable.chmod(0o755)
        architecture = _architecture(executable)
        package = executable.parent
        for suffix in ('bin', 'param'):
            if not (package / 'models' / f'{MODEL_NAME}.{suffix}').is_file():
                raise UpscaleError(f'The general-photo {MODEL_NAME} model is missing from the release.')
        manifest = {'schema_version': 1, 'release_tag': RELEASE_TAG, 'archive_url': ARCHIVE_URL,
                    'archive_sha256': ARCHIVE_SHA256, 'checksum_provenance': 'locally computed and pinned; no upstream checksum verified',
                    'architecture': architecture, 'files': {str(p.relative_to(package)): file_sha256(p)
                                                           for p in package.rglob('*') if p.is_file()}}
        (package / 'installation.json').write_text(json.dumps(manifest, indent=2))
        shutil.move(str(package), destination)
    return {'status': 'installed', 'path': str(destination), **manifest}


def _runtime(executable=None):
    configured = executable or os.environ.get('ADDR_REALESRGAN_BIN')
    binary = Path(configured or DEFAULT_INSTALL / 'realesrgan-ncnn-vulkan').resolve()
    models = Path(os.environ.get('ADDR_REALESRGAN_MODELS', str(binary.parent / 'models'))).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise UpscaleError('Real-ESRGAN is unavailable. Install the pinned portable bundle or set ADDR_REALESRGAN_BIN; no interpolation fallback was run.')
    files = {'executable': binary, 'weights': models / f'{MODEL_NAME}.bin', 'parameters': models / f'{MODEL_NAME}.param'}
    if any(not file.is_file() for file in files.values()):
        raise UpscaleError(f'The {MODEL_NAME} binary/parameter files are unavailable; anime models are not substituted.')
    hashes = {name: file_sha256(file) for name, file in files.items()}
    installation = binary.parent / 'installation.json'
    archive = None
    if installation.exists():
        try:
            installed = json.loads(installation.read_text())
            archive = installed['archive_sha256']
            if not configured and archive != ARCHIVE_SHA256:
                raise UpscaleError('Installed runtime does not match the pinned release artifact.')
            for file in files.values():
                expected = installed['files'][str(file.relative_to(binary.parent))]
                if file_sha256(file) != expected:
                    raise UpscaleError('Installed Real-ESRGAN files differ from the pinned installation manifest.')
        except (OSError, ValueError, KeyError) as error:
            raise UpscaleError('Real-ESRGAN installation manifest could not be verified.') from error
    elif not configured:
        raise UpscaleError('Pinned Real-ESRGAN installation manifest is missing.')
    identity = {'name': MODEL_NAME, 'scale': SCALE, 'executable_sha256': hashes['executable'],
                'weights_sha256': hashes['weights'], 'parameters_sha256': hashes['parameters'],
                'release_tag': RELEASE_TAG if archive == ARCHIVE_SHA256 else None, 'archive_sha256': archive,
                'runtime_origin': 'pinned-portable-release' if archive == ARCHIVE_SHA256 else 'explicitly-configured',
                'architecture': _architecture(binary), 'tile_size': TILE_SIZE, 'threads': '1:1:1', 'tta': False}
    return binary, models, identity


def crop_geometry(image_size, ring_pixels, padding_px=16, max_crop_px=768):
    """Half-open integer source crop, with edge-coordinate affine [x,y,1] @ M."""
    if (type(padding_px) is not int or not 0 <= padding_px <= 128 or type(max_crop_px) is not int
            or not 16 <= max_crop_px <= MAX_CROP_SIDE):
        raise UpscaleError('Use 0..128 integer padding pixels and a crop bound of 16..1024 pixels.')
    if not isinstance(ring_pixels, (list, tuple)) or len(ring_pixels) < 3 or len(ring_pixels) > 10000:
        raise UpscaleError('Provide a bounded roof ring in source image pixels.')
    width, height = image_size
    for point in ring_pixels:
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in point)
                or not 0 <= point[0] <= width or not 0 <= point[1] <= height):
            raise UpscaleError('Roof coordinates must be finite and inside the immutable source image.')
    xs, ys = [p[0] for p in ring_pixels], [p[1] for p in ring_pixels]
    requested = [math.floor(min(xs)) - padding_px, math.floor(min(ys)) - padding_px,
                 math.ceil(max(xs)) + padding_px, math.ceil(max(ys)) + padding_px]
    box = [max(0, requested[0]), max(0, requested[1]), min(width, requested[2]), min(height, requested[3])]
    left, top, right, bottom = box
    if min(right - left, bottom - top) < 1 or max(right - left, bottom - top) > max_crop_px:
        raise UpscaleError(f'Roof crop exceeds the {max_crop_px}px bound or is empty; no source resampling was performed.')
    return {'crop_box': box, 'crop_size': [right - left, bottom - top], 'padding_px': padding_px,
            'clipped_edges': [name for name, a, b in zip(('left', 'top', 'right', 'bottom'), requested, box, strict=True) if a != b],
            'source_to_upscaled': [[SCALE, 0], [0, SCALE], [-SCALE * left, -SCALE * top]],
            'upscaled_to_source': [[1 / SCALE, 0], [0, 1 / SCALE], [left, top]],
            'coordinate_convention': 'continuous pixel-edge coordinates; [x,y,1] @ 3x2 matrix; source crop box is half-open',
            'upscaled_pixel_center_index_to_source': [[1 / SCALE, 0], [0, 1 / SCALE], [left + .5 / SCALE - .5, top + .5 / SCALE - .5]]}


def _cached(directory, identity, key):
    manifest_path = directory / 'manifest.json'
    try:
        manifest = json.loads(manifest_path.read_text())
        if manifest['cache_key'] != key or manifest['identity'] != identity:
            raise UpscaleError('Cached Real-ESRGAN identity differs from this request.')
        if set(manifest['artifacts']) != {'input.png', 'upscaled.png', 'inference.log'}:
            raise UpscaleError('Cached Real-ESRGAN artifact hashes are incomplete.')
        expected_size = [n * SCALE for n in identity['crop']['crop_size']]
        if manifest['output_size'] != expected_size:
            raise UpscaleError('Cached Real-ESRGAN dimensions do not match the source crop and scale.')
        for name, expected in manifest['artifacts'].items():
            if name not in ('input.png', 'upscaled.png', 'inference.log') or file_sha256(directory / name) != expected:
                raise UpscaleError('Cached Real-ESRGAN artifact hash mismatch; preserve the entry and choose a fresh cache directory.')
        with Image.open(directory / 'upscaled.png') as output:
            if list(output.size) != expected_size or output.format != 'PNG':
                raise UpscaleError('Cached Real-ESRGAN output dimensions changed.')
    except (OSError, ValueError, KeyError) as error:
        raise UpscaleError('Cached Real-ESRGAN result could not be verified; no resize fallback was used.') from error
    return {**manifest, **identity['crop'], 'status': 'available', 'cache_hit': True,
            'output_path': str(directory / 'upscaled.png'), 'input_path': str(directory / 'input.png'),
            'manifest_path': str(manifest_path), 'source_sha256': identity['source_sha256'], 'scale': SCALE}


def upscale_roof_crop(image_path, ring_pixels, *, cache_dir=None, padding_px=16, max_crop_px=768,
                      timeout=120, executable=None):
    """Run genuine x4plus inference or raise UpscaleError; all writes are cached derivatives."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
        raise UpscaleError('Use an inference timeout between 1 and 300 seconds.')
    source = Path(image_path).resolve()
    try:
        if source.stat().st_size > MAX_SOURCE_BYTES:
            raise UpscaleError('Source image exceeds the bounded local input size.')
        raw = source.read_bytes()
        with Image.open(io.BytesIO(raw)) as original:
            if original.width * original.height > MAX_SOURCE_PIXELS:
                raise UpscaleError('Source image exceeds the bounded local pixel count.')
            image = original.convert('RGB')
    except (OSError, Image.DecompressionBombError) as error:
        raise UpscaleError('The immutable source image could not be decoded.') from error
    crop = crop_geometry(image.size, ring_pixels, padding_px, max_crop_px)
    binary, models, runtime = _runtime(executable)
    identity = {'version': VERSION, 'code_sha256': file_sha256(__file__), 'source_path': str(source),
                'source_sha256': sha256_bytes(raw), 'source_size': list(image.size), 'crop': crop, 'runtime': runtime}
    key = sha256_bytes(_json(identity).encode())
    cache = Path(cache_dir or DEFAULT_CACHE).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    final = cache / key
    with (cache / f'{key}.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UpscaleError('This roof crop is already being upscaled; retry when it finishes.') from error
        if final.exists():
            return _cached(final, identity, key)
        with tempfile.TemporaryDirectory(prefix=f'.{key[:12]}-', dir=cache) as temp:
            working = Path(temp)
            image.crop(tuple(crop['crop_box'])).save(working / 'input.png')
            command = [str(binary), '-i', str(working / 'input.png'), '-o', str(working / 'upscaled.png'),
                       '-m', str(models), '-n', MODEL_NAME, '-s', str(SCALE), '-t', str(TILE_SIZE), '-j', '1:1:1', '-f', 'png']
            started = time.monotonic()
            try:
                result = subprocess.run(command, cwd=binary.parent, capture_output=True, text=True,
                                        timeout=timeout, check=False)
            except subprocess.TimeoutExpired as error:
                raise UpscaleError(f'Real-ESRGAN exceeded {timeout}s; no derivative was published.') from error
            except OSError as error:
                raise UpscaleError('Real-ESRGAN could not run on this host; no derivative was published.') from error
            elapsed = time.monotonic() - started
            log = (result.stdout + '\n' + result.stderr)[-32768:]
            (working / 'inference.log').write_text(log)
            if result.returncode:
                raise UpscaleError(f'Real-ESRGAN inference failed (exit {result.returncode}): {log[-1000:]}')
            expected_size = [n * SCALE for n in crop['crop_size']]
            try:
                with Image.open(working / 'upscaled.png') as output:
                    if list(output.size) != expected_size or output.format != 'PNG':
                        raise UpscaleError('Real-ESRGAN returned the wrong scale or format; output was not published.')
                    output.load()
            except OSError as error:
                raise UpscaleError('Real-ESRGAN did not produce a decodable output; no fallback was used.') from error
            manifest = {'schema_version': 1, 'cache_key': key, 'identity': identity,
                        'created_at': datetime.now(UTC).isoformat(), 'elapsed_seconds': round(elapsed, 4),
                        'output_size': expected_size, 'model': runtime,
                        'artifacts': {name: file_sha256(working / name) for name in ('input.png', 'upscaled.png', 'inference.log')},
                        'use': 'candidate discovery only; project into original pixels and gate on original RGB',
                        'training_eligible': False, 'source_modified': False,
                        'limitations': ['Generative restoration can invent edges or texture.', 'Four output pixels do not constitute four independent observations.', 'No recovered heights, hidden eaves or human labels are asserted.']}
            (working / 'manifest.json').write_text(json.dumps(manifest, indent=2))
            working.rename(final)
        return {**_cached(final, identity, key), 'cache_hit': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-archive', type=Path, help='Install the pinned official macOS ZIP explicitly')
    parser.add_argument('--install-dir', type=Path, default=DEFAULT_INSTALL)
    parser.add_argument('--image', type=Path)
    parser.add_argument('--ring-json', type=Path, help='JSON array of source-pixel [x,y] roof coordinates')
    parser.add_argument('--input', type=Path, help='Case JSON with source.image_path and raw_ring')
    parser.add_argument('--cache-dir', type=Path, default=DEFAULT_CACHE)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--padding-px', type=int, default=16)
    parser.add_argument('--max-crop-px', type=int, default=768)
    parser.add_argument('--timeout', type=float, default=120)
    args = parser.parse_args(argv)
    try:
        if args.install_archive:
            result = install_archive(args.install_archive, args.install_dir)
        else:
            if args.input:
                case = json.loads(args.input.read_text())
                image, ring = case['source']['image_path'], case['raw_ring']
                expected = case['source'].get('image_sha256')
                if expected and file_sha256(image) != expected:
                    raise UpscaleError('The case image differs from its declared source hash.')
            elif args.image and args.ring_json:
                image, ring = args.image, json.loads(args.ring_json.read_text())
            else:
                parser.error('Use --input CASE.json or both --image and --ring-json.')
            result = upscale_roof_crop(image, ring, cache_dir=args.cache_dir, padding_px=args.padding_px,
                                       max_crop_px=args.max_crop_px, timeout=args.timeout, executable=args.executable)
        print(json.dumps(result, indent=2))
        return 0
    except (UpscaleError, OSError, KeyError, json.JSONDecodeError) as error:
        print(json.dumps({'status': 'unavailable', 'error': str(error), 'fallback_used': False}, indent=2))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
