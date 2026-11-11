"""Pull a published training manifest from Fieldwork and materialise its files, every byte hash-verified.

Standard library only, so a training box needs nothing but Python 3.12, the Fieldwork URL and a token.
Chunks land in <out>/blobs/<sha256> first; files under <out>/data/... are written (O_EXCL) only after every
chunk of the selection verified, so a hash mismatch never leaves a half-restored tree. --with-scenes adds
the referenced addr-scene items as data/scenes/<id>/ (image, latest.json, compact review.json, model
snapshot) so export_dataset runs on the pulled root; it requires the eyes group because
export_dataset.eyes_references silently drops registry rows when eyes.sqlite is absent.
Pulled values are model estimates and review agreement as published, never human labels.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REMOTE_URL = 'https://fieldwork-annotations.ben-ebsworth.workers.dev'
DEFAULT_TOKEN_FILE = 'deploy/annotation/admin-token.txt'
TRAINING_PROJECT = 'addr-training'
MANIFEST_KIND = 'addr-training-manifest'
SHA = re.compile(r'^[0-9a-f]{64}$')
ITEM_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
MAX_ASSET_BYTES = 64 * 1024 * 1024
RETRY_STATUSES = (429, 500, 502, 503, 504)
USER_AGENT = 'addr-training-pull/1 (Python urllib; Fieldwork training artefacts)'


class PullError(RuntimeError):
    """A pull stopped before writing anything unverified; the message never carries a token."""


def _token(origin, token_file=None):
    if token_file:
        path = Path(token_file).expanduser()
        path = path if path.is_absolute() else ROOT / path
        return path.read_text().strip()
    if env := os.environ.get('ANNOTATION_TOKEN'):
        return env
    if env := os.environ.get('FIELDWORK_TOKEN_FILE'):
        return _token(origin, env)
    default = ROOT / DEFAULT_TOKEN_FILE
    if origin.rstrip('/') == DEFAULT_REMOTE_URL and default.is_file():
        return default.read_text().strip()
    return None


def _safe_path(value):
    return (isinstance(value, str) and value and value == os.path.normpath(value) and not value.startswith(('/', '../'))
            and '..' not in Path(value).parts and ':' not in value and '\\' not in value and '\x00' not in value)


class Remote:
    """Minimal Fieldwork client: RPC POST and content-addressed asset GET with retries and sha verification."""

    def __init__(self, origin, token=None, timeout=120, retries=3):
        if not re.match(r'^https?://', origin or ''):
            raise PullError('--url must be an http(s) origin')
        self.origin, self.token, self.timeout, self.retries = origin.rstrip('/'), token, timeout, retries
        self.requests = []

    def _open(self, path, *, method='GET', body=None, headers=None):
        request = urllib.request.Request(self.origin + path, data=body, method=method)
        request.add_header('Accept', 'application/json')
        # Cloudflare's bot rules (error 1010) reject urllib's default agent string.
        request.add_header('User-Agent', USER_AGENT)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if body is not None:
            request.add_header('Content-Type', 'application/json')
        if self.token:
            request.add_header('Authorization', 'Bearer ' + self.token)
        self.requests.append((method, path))
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - origin is validated
                    return response.status, response.headers, response.read()
            except urllib.error.HTTPError as error:
                detail = error.read(2048).decode('utf-8', 'replace')
                if error.code in RETRY_STATUSES and attempt < self.retries:
                    time.sleep(0.5 * 2 ** attempt)
                    continue
                try:
                    message = json.loads(detail).get('message') or detail
                except ValueError:
                    message = detail
                raise PullError(f'HTTP {error.code} for {method} {path}: {message[:300]}') from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                if attempt < self.retries:
                    time.sleep(0.5 * 2 ** attempt)
                    continue
                raise PullError(f'{method} {path} failed: {getattr(error, "reason", error)}') from None
        raise PullError(f'{method} {path} failed after retries')  # pragma: no cover

    def rpc(self, method, payload):
        _, _, data = self._open(f'/api/v1/rpc/{method}', method='POST', body=json.dumps(payload).encode())
        try:
            decoded = json.loads(data)
        except ValueError as error:
            raise PullError(f'{method} returned invalid JSON') from error
        if not isinstance(decoded, dict):
            raise PullError(f'{method} returned an unexpected response')
        return decoded

    def asset(self, sha256, size=None):
        if not SHA.match(sha256 or ''):
            raise PullError(f'invalid asset digest {sha256!r}')
        _, headers, data = self._open(f'/api/v1/assets/{sha256}', headers={'Accept-Encoding': 'gzip'})
        if (headers.get('Content-Encoding') or '').lower() == 'gzip':
            try:
                data = gzip.decompress(data)
            except (OSError, EOFError) as error:
                raise PullError(f'asset {sha256[:12]} has an invalid gzip body') from error
        if len(data) > MAX_ASSET_BYTES:
            raise PullError(f'asset {sha256[:12]} exceeds 64 MiB')
        digest = hashlib.sha256(data).hexdigest()
        if digest != sha256:
            raise PullError(f'asset {sha256[:12]}: downloaded bytes hash to {digest[:12]}; nothing written')
        if size is not None and len(data) != size:
            raise PullError(f'asset {sha256[:12]}: {len(data)} bytes, manifest says {size}')
        return data


def _manifest_summary(item):
    source = item.get('source') if isinstance(item.get('source'), dict) else {}
    ref = source.get('manifest') if isinstance(source.get('manifest'), dict) else {}
    addr = (item.get('metadata') or {}).get('addr') if isinstance(item.get('metadata'), dict) else {}
    addr = addr if isinstance(addr, dict) else {}
    counts = addr.get('counts') if isinstance(addr.get('counts'), dict) else {}
    return {'itemId': item.get('id'), 'revision': item.get('revision'), 'manifestSha256': ref.get('sha256'),
            'publishedAt': addr.get('publishedAt') or item.get('createdAt'), 'groups': sorted((source.get('groups') or {}).keys()),
            'bytes': counts.get('bytes'), 'files': counts.get('files'), 'snapshotId': source.get('snapshotId'),
            'parents': source.get('parents') or [], 'codeSha256': addr.get('codeSha256'), 'kind': source.get('kind')}


def _list_items(remote, project, limit):
    cursor, items = '', []
    while True:
        page = remote.rpc('ListItems', {'projectId': project, 'adapter': 'addr-report', 'cursor': cursor, 'limit': limit})
        items.extend(i for i in page.get('items', []) if isinstance(i, dict))
        cursor = page.get('nextCursor') or ''
        if not cursor:
            return items


def list_manifests(url, token_file=None, project=TRAINING_PROJECT, limit=20):
    """Published training manifests, newest first; read-only."""
    origin = (url or os.environ.get('ANNOTATION_URL') or DEFAULT_REMOTE_URL).rstrip('/')
    remote = Remote(origin, _token(origin, token_file))
    projects, cursor = [], ''
    while True:
        page = remote.rpc('ListProjects', {'cursor': cursor, 'limit': 500})
        projects.extend(p.get('id') for p in page.get('projects', []) if isinstance(p, dict))
        cursor = page.get('nextCursor') or ''
        if not cursor:
            break
    if project not in projects:
        return {'origin': origin, 'projectId': project, 'projectExists': False, 'manifests': []}
    items = [_manifest_summary(i) for i in _list_items(remote, project, max(1, min(limit, 500)))]
    items = [i for i in items if i['kind'] == 'training-manifest' and i['manifestSha256']]
    items.sort(key=lambda i: (i['publishedAt'] or '', i['itemId'] or ''), reverse=True)
    return {'origin': origin, 'projectId': project, 'projectExists': True, 'manifests': items[:limit]}


def resolve_item(remote, project, manifest=None, latest=False):
    if latest == bool(manifest):
        raise PullError('Choose exactly one of --manifest ITEMID_OR_SHA or --latest')
    if latest:
        items = [i for i in _list_items(remote, project, 500) if (i.get('source') or {}).get('kind') == 'training-manifest']
        if not items:
            raise PullError(f'No training manifests published in project {project!r}')
        items.sort(key=lambda i: (((i.get('metadata') or {}).get('addr') or {}).get('publishedAt') or '', i.get('id') or ''))
        return items[-1]
    if SHA.match(manifest):
        for item in _list_items(remote, project, 500):
            if ((item.get('source') or {}).get('manifest') or {}).get('sha256') == manifest:
                return item
        raise PullError(f'No item in {project!r} references manifest {manifest[:12]}')
    if not ITEM_ID.match(manifest):
        raise PullError('--manifest must be an item id or a manifest SHA-256')
    response = remote.rpc('GetItem', {'id': manifest})
    item = response.get('item')
    if not isinstance(item, dict):
        raise PullError(f'GetItem {manifest} returned no item')
    return item


def load_manifest(remote, item):
    ref = ((item.get('source') or {}).get('manifest') or {})
    sha, size = ref.get('sha256'), ref.get('bytes')
    if not SHA.match(sha or ''):
        raise PullError(f'item {item.get("id")} has no valid source.manifest')
    raw = remote.asset(sha, size if isinstance(size, int) else None)
    try:
        manifest = json.loads(raw)
    except ValueError as error:
        raise PullError('manifest asset is not valid JSON') from error
    if manifest.get('schemaVersion') != 1 or manifest.get('kind') != MANIFEST_KIND or not isinstance(manifest.get('files'), list):
        raise PullError('manifest is not an addr-training-manifest (schemaVersion 1)')
    for entry in manifest['files']:
        if not _safe_path(entry.get('path')) or not SHA.match(entry.get('sha256') or '') or not isinstance(entry.get('bytes'), int) \
                or not isinstance(entry.get('chunks'), list) or not entry['chunks'] \
                or any(not SHA.match(c.get('sha256') or '') or not isinstance(c.get('bytes'), int) for c in entry['chunks']) \
                or sum(c['bytes'] for c in entry['chunks']) != entry['bytes']:
            raise PullError(f'manifest file entry is invalid: {entry.get("path")!r}')
    return raw, sha, manifest


def select_files(manifest, only=None):
    """Files of the selected groups (prefix match: edit-diffs matches edit-diffs/<date>); None selects everything."""
    if not only:
        return list(manifest['files']), sorted((manifest.get('groups') or {}).keys())
    groups = manifest.get('groups') or {}
    chosen, paths = [], set()
    for wanted in only:
        matches = [name for name in groups if name == wanted or name.startswith(wanted + '/')]
        if not matches:
            raise PullError(f'group {wanted!r} is not in this manifest; available: {", ".join(sorted(groups)) or "none"}')
        for name in matches:
            chosen.append(name)
            paths.update(groups[name].get('files', []))
    return [entry for entry in manifest['files'] if entry['path'] in paths], sorted(set(chosen))


def _sha_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _write_new(path, chunks_dir, chunks, expected_sha, expected_bytes, resume, mtime_ns=None):
    """Reassemble ordered chunks into path with O_EXCL; with resume an identical existing file is kept."""
    path = Path(path)
    if resume and path.exists():
        if path.is_file() and path.stat().st_size == expected_bytes and _sha_file(path) == expected_sha:
            return 'kept'
        raise PullError(f'{path} exists with different content; refusing to overwrite')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(descriptor, 'wb') as out:
            for chunk in chunks:
                with open(chunks_dir / chunk['sha256'], 'rb') as source:
                    while block := source.read(1 << 20):
                        digest.update(block)
                        size += len(block)
                        out.write(block)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if size != expected_bytes or digest.hexdigest() != expected_sha:
        path.unlink(missing_ok=True)
        raise PullError(f'{path.name}: reassembled bytes do not match the manifest')
    if mtime_ns:
        os.utime(path, ns=(int(mtime_ns), int(mtime_ns)))
    return 'written'


def _fetch_chunk(remote, blobs, sha256, size, resume, counts):
    target = blobs / sha256
    if target.exists():
        if target.stat().st_size == size and _sha_file(target) == sha256:
            counts['blobsReused' if resume else 'blobsVerified'] += 1
            return
        target.unlink()
    data = remote.asset(sha256, size)
    temporary = blobs / f'.{sha256}.part'
    temporary.write_bytes(data)
    os.replace(temporary, target)
    counts['blobsDownloaded'] += 1
    counts['bytesDownloaded'] += len(data)


def _scene_refs(source):
    refs = {}
    for key in ('image', 'model', 'review', 'reviewModel', 'confidence'):
        ref = source.get(key)
        if isinstance(ref, dict) and SHA.match(ref.get('sha256') or ''):
            refs[key] = ref
    return refs


def _download_ref(remote, blobs, ref, resume, counts):
    chunks = ref.get('chunks') if isinstance(ref.get('chunks'), list) and ref['chunks'] else [ref]
    for chunk in chunks:
        if not SHA.match(chunk.get('sha256') or '') or not isinstance(chunk.get('bytes'), int):
            raise PullError('scene asset reference is invalid')
        _fetch_chunk(remote, blobs, chunk['sha256'], chunk['bytes'], resume, counts)
    return chunks


def pull_scenes(remote, manifest, item, out, blobs, resume, counts):
    """Materialise each referenced addr-scene item under out/data/scenes/<id>/ after all its assets verified."""
    scenes = manifest.get('scenes') or (item.get('source') or {}).get('scenes') or []
    pulled = []
    for entry in scenes:
        item_id = entry.get('itemId') if isinstance(entry, dict) else None
        if not item_id or not ITEM_ID.match(item_id):
            raise PullError(f'scene entry has no valid itemId: {entry!r}')
        scene_item = remote.rpc('GetItem', {'id': item_id}).get('item')
        if not isinstance(scene_item, dict) or scene_item.get('adapter') != 'addr-scene':
            raise PullError(f'{item_id} is not an addr-scene item')
        source = scene_item.get('source') or {}
        scene_id = source.get('sceneId')
        if not isinstance(scene_id, str) or not ITEM_ID.match(scene_id):
            raise PullError(f'{item_id} has no valid source.sceneId')
        refs = _scene_refs(source)
        if 'image' not in refs or 'model' not in refs:
            raise PullError(f'{item_id} lacks image or model assets')
        chunks = {key: _download_ref(remote, blobs, ref, resume, counts) for key, ref in refs.items()}
        folder = out / 'data' / 'scenes' / scene_id
        signature = source.get('modelSignature')
        model_bytes = b''.join((blobs / c['sha256']).read_bytes() for c in chunks['model'])
        if not isinstance(signature, str) or not SHA.match(signature):
            signature = (json.loads(model_bytes) or {}).get('model_signature')
        if not isinstance(signature, str) or not SHA.match(signature):
            raise PullError(f'{item_id}: model signature unknown')
        targets = {'image': folder / f"{refs['image']['sha256']}.png", 'model': folder / 'latest.json'}
        review_signature = signature
        if 'review' in refs:
            targets['review'] = folder / 'review.json'
            review = json.loads(b''.join((blobs / c['sha256']).read_bytes() for c in chunks['review']))
            if isinstance(review, dict) and SHA.match(str(review.get('model_signature') or '')):
                review_signature = review['model_signature']
        if 'reviewModel' in refs:
            targets['reviewModel'] = folder / 'models' / f'{review_signature}.json'
        elif review_signature == signature:
            targets['model_copy'] = folder / 'models' / f'{signature}.json'
        if 'confidence' in refs:
            targets['confidence'] = folder / 'confidence.json'
        for key, target in targets.items():
            ref = refs['model' if key == 'model_copy' else key]
            _write_new(target, blobs, chunks['model' if key == 'model_copy' else key], ref['sha256'], ref['bytes'], resume)
        pulled.append({'sceneId': scene_id, 'itemId': item_id, 'revision': scene_item.get('revision'), 'modelSignature': signature,
                       'reviewModelSignature': review_signature if 'review' in refs else None,
                       'imageSha256': refs['image']['sha256'], 'files': sorted(str(t.relative_to(out)) for t in targets.values())})
    return pulled


def pull(url, token_file, out, *, project=TRAINING_PROJECT, manifest=None, latest=False, only=None, with_scenes=False, resume=False):
    """Fetch one manifest and its selected groups into out; every chunk and whole file is sha-verified."""
    origin = (url or os.environ.get('ANNOTATION_URL') or DEFAULT_REMOTE_URL).rstrip('/')
    out = Path(out).expanduser().resolve()
    if out.exists() and not resume:
        raise PullError(f'{out} already exists; choose a new directory or pass --resume')
    if out.exists() and not out.is_dir():
        raise PullError(f'{out} is not a directory')
    remote = Remote(origin, _token(origin, token_file))
    item = resolve_item(remote, project, manifest, latest)
    raw_manifest, manifest_sha, document = load_manifest(remote, item)
    files, groups = select_files(document, only)
    if with_scenes:
        if 'eyes' not in (document.get('groups') or {}) or 'eyes' not in groups:
            raise PullError("--with-scenes needs the 'eyes' group in the manifest and the selection: export_dataset silently "
                            'drops registry references when eyes.sqlite is absent')
        if not (document.get('scenes') or (item.get('source') or {}).get('scenes')):
            raise PullError('--with-scenes: this manifest lists no scenes (publish with --with-scenes)')
    counts = {'blobsDownloaded': 0, 'blobsReused': 0, 'blobsVerified': 0, 'bytesDownloaded': 0, 'filesWritten': 0, 'filesKept': 0}
    blobs = out / 'blobs'
    blobs.mkdir(parents=True, exist_ok=True)
    # Phase 1: every chunk of the selection, verified in memory before it touches disk.
    for entry in files:
        for chunk in entry['chunks']:
            _fetch_chunk(remote, blobs, chunk['sha256'], chunk['bytes'], resume, counts)
    # Phase 2: reassemble into the original repo-relative paths; whole-file sha checked again.
    restored = []
    for entry in files:
        outcome = _write_new(out / entry['path'], blobs, entry['chunks'], entry['sha256'], entry['bytes'], resume, entry.get('mtimeNs'))
        counts['filesWritten' if outcome == 'written' else 'filesKept'] += 1
        restored.append({'path': entry['path'], 'sha256': entry['sha256'], 'bytes': entry['bytes']})
    scenes = pull_scenes(remote, document, item, out, blobs, resume, counts) if with_scenes else []
    (out / 'TRAINING_MANIFEST.json').write_bytes(raw_manifest)
    receipt = {'schemaVersion': 1, 'origin': origin, 'projectId': project, 'itemId': item.get('id'), 'revision': item.get('revision'),
               'manifestSha256': manifest_sha, 'snapshotId': document.get('snapshotId'), 'codeSha256': document.get('codeSha256'),
               'gitHead': document.get('gitHead'), 'groups': groups, 'files': restored, 'scenes': scenes, 'counts': counts,
               'pulledAt': datetime.now(UTC).isoformat(), 'complete': True,
               'note': 'Pulled values are model estimates and review agreement as published; not human labels.'}
    (out / '.pull-receipt.json').write_text(json.dumps(receipt, indent=2, sort_keys=True))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('list', help='list published training manifests (read-only)')
    pulling = commands.add_parser('pull', help='download one manifest and its files with sha verification')
    for command in (listing, pulling):
        command.add_argument('--url', help='Fieldwork origin (ANNOTATION_URL)')
        command.add_argument('--token-file', help='bearer token file (FIELDWORK_TOKEN_FILE / ANNOTATION_TOKEN)')
        command.add_argument('--project', default=TRAINING_PROJECT)
    listing.add_argument('--limit', type=int, default=20)
    pulling.add_argument('--manifest', help='item id or manifest SHA-256')
    pulling.add_argument('--latest', action='store_true')
    pulling.add_argument('--out', required=True, help='new directory (or an existing pull with --resume)')
    pulling.add_argument('--only', help='comma-separated groups, e.g. dataset-v2,edit-diffs,eyes')
    pulling.add_argument('--with-scenes', action='store_true', help='also pull the referenced addr-scene items (needs eyes)')
    pulling.add_argument('--resume', action='store_true', help='reuse verified blobs and identical files in --out')
    args = parser.parse_args(argv)
    try:
        if args.command == 'list':
            result = list_manifests(args.url, args.token_file, project=args.project, limit=args.limit)
        else:
            only = [part.strip() for part in args.only.split(',') if part.strip()] if args.only else None
            result = pull(args.url, args.token_file, args.out, project=args.project, manifest=args.manifest, latest=args.latest,
                          only=only, with_scenes=args.with_scenes, resume=args.resume)
    except PullError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
