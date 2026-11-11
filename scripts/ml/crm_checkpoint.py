"""CRM checkpoint reporting for the roof "eyes" work.

Each checkpoint is published as one append-only ``pipeline-checkpoint`` POI plus a
read-modify-write ``UpdateAddressMeta`` that adds ``eyes:`` namespaced tags and one
newest-first ``eyes:`` note line, and is filed in full under
``data/references/eyes-checkpoints/<cp_id>/``. These are diagnostics and review
hand-offs, not labels or accuracy. The module never touches spatial features: the
only RPCs it can emit are the allow-listed read and POI/meta methods below, and
``UpdateAddressMeta`` always re-sends the current status and owner fields verbatim
because the server overwrites every column.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from scripts.ml import eyes_index

VERSION = 'crm-checkpoint-v1'
POI_CATEGORY = 'pipeline-checkpoint'
TAG_PREFIX = 'eyes:'
NOTE_PREFIX = 'eyes:'
NOTE_CAP = 12
POI_NOTE_CAP = 120
NOTE_LINE_CAP = 400
TAG_CAP = 120
PRINCIPAL = 'pipeline'
DRY_RUN_DIR = 'dry-run'
DEFAULT_BASE = 'http://localhost:8091'
SERVICE_PATH = '/crm.v1.CrmService/'
CHECKPOINTS = Path('data/references/eyes-checkpoints')
TOKEN_FILE = Path('.run/eyes-crm-token')
ENV_FILE = Path('.env.local')
GEO_DB = Path('data/australia_geo.db')
WRITE_METHODS = frozenset({'CreatePoi', 'UpdateAddressMeta'})
ALLOWED_METHODS = frozenset({'WhoAmI', 'GetAddressCrm', 'ListPois'}) | WRITE_METHODS
PRESERVED_FIELDS = ('status', 'ownerName', 'ownerEmail', 'ownerPhone', 'fullAddress')
SENSITIVE_FIELDS = ('ownerName', 'ownerEmail', 'ownerPhone')
_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


class CrmError(RuntimeError):
    """Non-2xx Connect response; carries the HTTP status and decoded body."""

    def __init__(self, method, status, body):
        super().__init__(f'{method} failed with HTTP {status}: {body}')
        self.method, self.status, self.body = method, status, body


class CheckpointRefused(RuntimeError):
    """A write was refused before it was sent (missing token, wrong principal, suppressed status, guard drift)."""


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _now():
    return datetime.now(UTC).isoformat(timespec='seconds')


def base_url(root=Path('.')):
    """GEO_SERVICE_URL from <root>/.env.local, else the local geo service."""
    file = Path(root) / ENV_FILE
    if file.exists():
        for line in file.read_text().splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and key.strip() == 'GEO_SERVICE_URL':
                value = value.strip().strip('"').strip("'").rstrip('/')
                if value:
                    return value
    return DEFAULT_BASE


def token(root=Path('.'), env=None):
    """Bearer token from <root>/.run/eyes-crm-token, else env CRM_TOKEN, else None."""
    file = Path(root) / TOKEN_FILE
    if file.exists():
        value = file.read_text().strip()
        if value:
            return value
    return (os.environ if env is None else env).get('CRM_TOKEN') or None


class Client:
    """Minimal Connect-JSON client restricted to ALLOWED_METHODS; every call is logged."""

    def __init__(self, base=DEFAULT_BASE, token=None, timeout=15):
        self.base, self.token, self.timeout, self.log = base.rstrip('/'), token, timeout, []

    def call(self, method, body):
        if method not in ALLOWED_METHODS:
            raise ValueError(f'{method} is not an allowed CRM method for checkpoints')
        if method in WRITE_METHODS and not self.token:
            raise CheckpointRefused(f'refusing {method}: no CRM token in {TOKEN_FILE} or CRM_TOKEN')
        headers = {'Content-Type': 'application/json', 'Connect-Protocol-Version': '1'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        request = urllib.request.Request(self.base + SERVICE_PATH + method, data=json.dumps(body).encode(),
                                         headers=headers, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        payload = _decode(raw)
        self.log.append({'method': method, 'request': body, 'status': status, 'at': _now()})
        if status >= 300:
            raise CrmError(method, status, payload)
        return payload


def _decode(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {'raw': raw.decode('utf-8', 'replace')}


def _int(value):
    return None if value in (None, '') else int(value)


def whoami(client):
    return client.call('WhoAmI', {})


def parse_tags(value):
    """Tags as a list; tolerates None, the literal 'null', a JSON array string or a double-encoded one."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(tag) for tag in value if tag is not None]
    text = str(value).strip()
    if text in ('', 'null'):
        return []
    try:
        decoded = json.loads(text)
    except ValueError:
        return [text]
    if isinstance(decoded, (list, str)) or decoded is None:
        return parse_tags(decoded)
    return [text]


def _tag_key(tag):
    if tag.startswith(TAG_PREFIX) and '=' in tag:
        return tag.split('=', 1)[0] + '='
    return None


def merge_tags(existing, add_tags):
    """Existing tags minus duplicates and any 'eyes:<key>=' entries superseded by add_tags, then add_tags."""
    replaced = {_tag_key(tag) for tag in add_tags if _tag_key(tag)}
    merged = []
    for tag in list(parse_tags(existing)) + list(add_tags):
        if tag in merged or (tag not in add_tags and _tag_key(tag) in replaced):
            continue
        merged.append(tag)
    return merged


def _one_line(text, cap=NOTE_LINE_CAP):
    line = ' '.join(str(text or '').split())
    return line if len(line) <= cap else line[:cap - 3].rstrip() + '...'


def _note_key(line):
    head = line.split(' ', 1)[0]
    return head if len(head) > len(NOTE_PREFIX) else None


def _split_notes(notes):
    """(eyes: lines, the other text with surrounding blank lines trimmed)."""
    lines = str(notes or '').split('\n')
    other = '\n'.join(text for text in lines if not text.startswith(NOTE_PREFIX)).strip('\n')
    return [text for text in lines if text.startswith(NOTE_PREFIX)], other


def merge_notes(existing, note_line, cap=NOTE_CAP):
    """Prepend one eyes: line (newest first, at most `cap` eyes: lines); non-eyes text stays intact below.

    An older line for the same checkpoint (same leading 'eyes:<id>' token) is replaced, so re-publishing
    a checkpoint does not stack duplicate lines."""
    line = _one_line(note_line)
    if not line.startswith(NOTE_PREFIX):
        line = f'{NOTE_PREFIX} {line}'
    key = _note_key(line)
    lines = str(existing or '').split('\n')
    eyes = [line] + [text for text in lines if text.startswith(NOTE_PREFIX) and text != line
                     and (key is None or _note_key(text) != key)]
    rest = [text for text in lines if not text.startswith(NOTE_PREFIX)]
    while rest and rest[0] == '':  # drop the separator this function inserted last time
        rest.pop(0)
    return '\n'.join(eyes[:cap] + ([''] + rest if rest else []))


def _checkpoint_id(cp):
    cp_id = str(cp.get('id') or '')
    if not _ID_PATTERN.fullmatch(cp_id) or cp_id.endswith(f'.{DRY_RUN_DIR}'):
        raise ValueError(f'checkpoint id {cp_id!r} must match {_ID_PATTERN.pattern} and not end in .{DRY_RUN_DIR}')
    return cp_id


def report_path(cp):
    return str(CHECKPOINTS / _checkpoint_id(cp) / 'REVIEW.md')


def eyes_tags(cp):
    """'eyes:checkpoint=<id>' plus cp['tags'] (dict key->value or list of eyes: strings); one value per key."""
    tags = [f'{TAG_PREFIX}checkpoint={_checkpoint_id(cp)}']
    raw = cp.get('tags') or {}
    raw = [raw] if isinstance(raw, str) else raw
    items = [f'{k}={v}' for k, v in raw.items()] if isinstance(raw, dict) else [str(t) for t in raw]
    for item in items:
        item = _one_line(item, TAG_CAP)
        tag = item if item.startswith(TAG_PREFIX) else TAG_PREFIX + item
        key = _tag_key(tag)
        if tag in tags or (key and any(_tag_key(existing) == key for existing in tags)):
            continue
        tags.append(tag)
    return tags


def note_line(cp, when=None):
    date = (when or _now())[:10]
    return _one_line(f'{NOTE_PREFIX}{_checkpoint_id(cp)} {date} {cp.get("summary") or ""} | {report_path(cp)}')


def create_poi(client, cp):
    """Append-only POI (category pipeline-checkpoint); notes capped, website = report path."""
    summary = _one_line(cp.get('summary'), POI_NOTE_CAP) or None
    name = _one_line(cp.get('title'), POI_NOTE_CAP) or f'{_checkpoint_id(cp)} pipeline checkpoint'
    body = {'name': name, 'category': POI_CATEGORY,
            'addressText': cp.get('address_text'), 'latitude': float(cp['latitude']), 'longitude': float(cp['longitude']),
            'website': cp.get('report_url') or report_path(cp), 'addressId': _int(cp.get('address_id')), 'notes': summary}
    body = {k: v for k, v in body.items() if v is not None}
    response = client.call('CreatePoi', body)
    return {'method': 'CreatePoi', 'at': _now(), 'request': body, 'poi': response.get('poi') or {}}


def existing_poi(client, cp):
    """The pipeline-checkpoint POI already filed for this checkpoint (same address and report link), or None."""
    website = cp.get('report_url') or report_path(cp)
    response = client.call('ListPois', {'addressId': _int(cp.get('address_id')), 'limit': 500})
    for poi in response.get('pois') or []:
        if poi.get('category') == POI_CATEGORY and poi.get('website') == website:
            return poi
    return None


def _feature_stamp(payload):
    return sorted((str(f.get('id')), f.get('updatedAt')) for f in payload.get('features') or [])


def update_address_meta(client, address_id, *, add_tags, note_line, allow_suppressed=False):
    """GetAddressCrm -> preserve status/owner verbatim, merge tags, prepend note -> UpdateAddressMeta -> read back."""
    if not client.token:
        raise CheckpointRefused(f'refusing UpdateAddressMeta: no CRM token in {TOKEN_FILE} or CRM_TOKEN')
    address_id = int(address_id)
    before = client.call('GetAddressCrm', {'addressId': address_id})
    meta = before.get('meta') or {}
    if str(meta.get('status') or '').strip().lower() == 'suppressed' and not allow_suppressed:
        raise CheckpointRefused(f'address {address_id} has status suppressed; pass allow_suppressed=True to annotate it')
    body = {'addressId': address_id}
    for field in ('status', 'ownerName', 'ownerEmail', 'ownerPhone'):
        if field in meta:
            body[field] = meta[field]
    body['tags'] = merge_tags(meta.get('tags'), list(add_tags))
    body['notes'] = merge_notes(meta.get('notes'), note_line)
    client.call('UpdateAddressMeta', body)
    after = client.call('GetAddressCrm', {'addressId': address_id})
    readback = after.get('meta') or {}
    preserved = {field: {'before': meta.get(field), 'after': readback.get(field),
                         'same': (meta.get(field) or '') == (readback.get(field) or '')} for field in PRESERVED_FIELDS}
    other_same = _split_notes(meta.get('notes'))[1] == _split_notes(readback.get('notes'))[1]
    diff = {'preserved': preserved, 'tags': {'before': parse_tags(meta.get('tags')), 'after': parse_tags(readback.get('tags'))},
            'notes': {'before': meta.get('notes'), 'after': readback.get('notes'), 'other_text_same': other_same}}
    return {'method': 'UpdateAddressMeta', 'at': _now(), 'address_id': address_id, 'request': body, 'diff': diff,
            'preserved_ok': all(item['same'] for item in preserved.values()) and other_same,
            'applied_ok': diff['tags']['after'] == body['tags'] and (readback.get('notes') or '') == body['notes'],
            'features_unchanged': _feature_stamp(before) == _feature_stamp(after),
            'tags_after': diff['tags']['after']}


def _masked(value):
    return None if value is None else f'<redacted {len(str(value))} chars>'


def _notes_view(notes):
    if notes is None:
        return None
    eyes, other = _split_notes(notes)
    return {'eyes_lines': eyes, 'other_text': _masked(other)}


def redact(record):
    """Copy of a write record safe to file: owner fields and non-eyes note text are masked; `same` flags stay."""
    if record.get('method') != 'UpdateAddressMeta' or record.get('redacted'):
        return record
    request = {k: (_masked(v) if k in SENSITIVE_FIELDS else v) for k, v in record['request'].items()}
    request['notes'] = _notes_view(request.get('notes'))
    diff = record['diff']
    preserved = {field: ({**item, 'before': _masked(item['before']), 'after': _masked(item['after'])}
                         if field in SENSITIVE_FIELDS else item) for field, item in diff['preserved'].items()}
    notes = {**diff['notes'], 'before': _notes_view(diff['notes']['before']), 'after': _notes_view(diff['notes']['after'])}
    return {**record, 'request': request, 'diff': {**diff, 'preserved': preserved, 'notes': notes}, 'redacted': True}


def _cell(value):
    if value is None:
        return 'n/a'
    if isinstance(value, float):
        value = f'{value:.4g}'
    return str(value).replace('|', '\\|').replace('\n', ' ')


def render_review(cp):
    """Markdown review: title, summary, then each section's text, table and items."""
    lines = [f'# {_one_line(cp.get("title") or _checkpoint_id(cp))}', '',
             f'Checkpoint `{_checkpoint_id(cp)}` | {VERSION} | address {cp.get("address_id")} | {_now()}', '',
             'Diagnostics and reference agreement for review; bands and agreement are not accuracy.', '']
    if cp.get('summary'):
        lines += [str(cp['summary']).strip(), '']
    for section in cp.get('sections') or []:
        lines += [f'## {_one_line(section.get("title") or "Section")}', '']
        if section.get('text'):
            lines += [str(section['text']).strip(), '']
        table = section.get('table')
        if table and table.get('columns'):
            columns = list(table['columns'])
            lines.append('| ' + ' | '.join(_cell(c) for c in columns) + ' |')
            lines.append('|' + '|'.join(' --- ' for _ in columns) + '|')
            for row in table.get('rows') or []:
                cells = [_cell(v) for v in ([row.get(c) for c in columns] if isinstance(row, dict) else row)]
                lines.append('| ' + ' | '.join(cells) + ' |')
            lines.append('')
        for item in section.get('items') or []:
            lines.append(f'- {_cell(item)}')
        if section.get('items'):
            lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def _write_json(path, payload):
    data = json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False, default=str).encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _write_receipt(folder, cp_id, report_sha, writes, dry_run, computed_at, **extra):
    return _write_json(folder / 'crm-writes.json', {'version': VERSION, 'code_sha256': code_sha256(), 'computed_at': computed_at,
                                                    'cp_id': cp_id, 'dry_run': dry_run, 'report_sha256': report_sha,
                                                    'writes': [redact(w) for w in writes], **extra})


def file_checkpoint(root, cp, writes=(), dry_run=False):
    """Write report.json, REVIEW.md and crm-writes.json under data/references/eyes-checkpoints/<cp_id>/.

    Dry runs go to <cp_id>/dry-run/ so they never overwrite the receipt of a live publish."""
    cp_id = _checkpoint_id(cp)
    folder = Path(root) / CHECKPOINTS / cp_id
    folder = folder / DRY_RUN_DIR if dry_run else folder
    folder.mkdir(parents=True, exist_ok=True)
    computed_at = _now()
    report_sha = _write_json(folder / 'report.json', {'version': VERSION, 'code_sha256': code_sha256(),
                                                      'computed_at': computed_at, 'checkpoint': cp})
    (folder / 'REVIEW.md').write_text(render_review(cp), encoding='utf-8')
    _write_receipt(folder, cp_id, report_sha, writes, dry_run, computed_at)
    return {'folder': str(folder), 'report_sha256': report_sha, 'review': str(folder / 'REVIEW.md'),
            'crm_writes': str(folder / 'crm-writes.json')}


def _archive_previous(folder):
    """Move an earlier live publish's files to history/<report sha[:12]>/ instead of overwriting its receipt."""
    try:
        previous = json.loads((folder / 'crm-writes.json').read_text())
    except (OSError, ValueError):
        return None
    if previous.get('dry_run') or not previous.get('writes'):
        return None
    name = str(previous.get('report_sha256') or 'unknown')[:12]
    target, n = folder / 'history' / name, 1
    while target.exists():
        target, n = folder / 'history' / f'{name}-{n}', n + 1
    target.mkdir(parents=True)
    for file in ('report.json', 'REVIEW.md', 'crm-writes.json'):
        if (folder / file).exists():
            (folder / file).replace(target / file)
    return str(target)


def address_location(root, address_id):
    """Read-only lookup of an address centroid from data/australia_geo.db, or None."""
    file = Path(root) / GEO_DB
    if not file.exists():
        return None
    with closing(sqlite3.connect(f'file:{file}?mode=ro', uri=True)) as db:
        row = db.execute('SELECT full_address, lat, lon FROM addresses WHERE id=?', (int(address_id),)).fetchone()
    if not row:
        return None
    return {'address_id': int(address_id), 'full_address': row[0], 'latitude': row[1], 'longitude': row[2]}


def _address_id(value, what):
    raw = value.get('address_id') if isinstance(value, dict) else value
    try:
        number = None if isinstance(raw, bool) else int(raw)
    except (TypeError, ValueError):
        number = None
    if number is None or number <= 0:
        raise ValueError(f'{what}: {value!r} is not a positive address id')
    return number


def _addresses(cp):
    extra = cp.get('addresses') or []
    if not isinstance(extra, list):
        raise ValueError('checkpoint addresses must be a list of address ids or {address_id} objects')
    ids = [_address_id(cp.get('address_id'), 'checkpoint address_id')]
    ids += [_address_id(item, 'checkpoint addresses entry') for item in extra]
    return list(dict.fromkeys(ids))


def _validate(cp):
    if not isinstance(cp, dict):
        raise ValueError('checkpoint payload must be a JSON object')
    cp_id = _checkpoint_id(cp)
    _addresses(cp)
    for key, limit in (('latitude', 90), ('longitude', 180)):
        if cp.get(key) is None:
            raise ValueError(f'checkpoint {cp_id} needs {key} (address_location(root, address_id) can supply it)')
        try:
            value = float(cp[key])
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value) or abs(value) > limit:
            raise ValueError(f'checkpoint {cp_id} {key} {cp[key]!r} is not a finite coordinate within +/-{limit}')
    sections = cp.get('sections') or []
    if not isinstance(sections, list) or not all(isinstance(section, dict) for section in sections):
        raise ValueError(f'checkpoint {cp_id} sections must be a list of objects')
    return cp_id


def _guard(guard, root):
    """(ok, failed check names) from an eyes_guard-style check; a guard that crashes counts as failed."""
    try:
        result = guard(root) or {}
    except Exception as error:
        return False, [f'guard raised {type(error).__name__}: {error}']
    return bool(result.get('ok')), [c.get('name') for c in result.get('checks') or [] if not c.get('ok')]


def _write_crm(client, cp, receipt, tags, line, addresses, allow_suppressed):
    poi = existing_poi(client, cp)
    receipt['poi_reused'] = poi is not None
    if poi is None:
        record = create_poi(client, cp)
        receipt['writes'].append(record)
        poi = record['poi']
    receipt['poi'] = poi
    for address_id in addresses:
        try:
            record = update_address_meta(client, address_id, add_tags=tags, note_line=line, allow_suppressed=allow_suppressed)
        except (CheckpointRefused, CrmError) as error:
            receipt['errors'].append(f'address {address_id}: {error}')
            continue
        receipt['writes'].append(redact(record))
        checks = {key: record[key] for key in ('preserved_ok', 'applied_ok', 'features_unchanged')}
        receipt['addresses'].append({'address_id': address_id, **checks, 'tags_after': record['tags_after']})
        if not all(checks.values()):
            receipt['errors'].append(f'address {address_id}: read-back check failed {sorted(k for k, v in checks.items() if not v)}')


def _record(root, receipt, tags, line):
    receipt['ok'] = not receipt['errors']
    payload = {'code_sha256': receipt['code_sha256'], 'computed_at': receipt['computed_at'], 'dry_run': receipt['dry_run'],
               'report_sha256': receipt['report_sha256'], 'principal': (receipt['whoami'] or {}).get('principal'),
               'poi_id': (receipt['poi'] or {}).get('id'), 'poi_reused': receipt['poi_reused'],
               'addresses': receipt['addresses'], 'tags': tags, 'note_line': line, 'guard': receipt['guard'],
               'archived': receipt['archived'], 'ok': receipt['ok'], 'errors': receipt['errors']}
    row_id = f'{receipt["cp_id"]}.{DRY_RUN_DIR}' if receipt['dry_run'] else receipt['cp_id']
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        eyes_index.replace_rows(db, 'checkpoints', [{'id': row_id, 'created_at': receipt['computed_at'],
                                                     'version': VERSION, 'payload': payload}])


def publish(root=Path('.'), cp=None, dry_run=False, client=None, allow_suppressed=False, guard=None):
    """File the checkpoint, then (unless dry_run) POI + address updates; record a checkpoints row; return a receipt.

    Refusals (bad input, no token, principal other than 'pipeline', guard drift before) raise before any file
    or CRM write. Once writes start, CRM/connection errors and guard drift after are recorded in the receipt,
    crm-writes.json and the checkpoints row (ok=False) rather than raised, so no write goes unrecorded. Dry
    runs file under <cp_id>/dry-run/ and index row '<cp_id>.dry-run'."""
    root = Path(root)
    cp_id = _validate(cp)
    tags, line, addresses = eyes_tags(cp), note_line(cp), _addresses(cp)
    receipt = {'version': VERSION, 'code_sha256': code_sha256(), 'computed_at': _now(), 'cp_id': cp_id, 'dry_run': dry_run,
               'planned': {'tags': tags, 'note_line': line, 'addresses': addresses}, 'writes': [], 'addresses': [],
               'poi': None, 'poi_reused': None, 'whoami': None, 'guard': None, 'archived': None, 'errors': []}
    if not dry_run:
        client = client or Client(base_url(root), token(root))
        if not client.token:
            raise CheckpointRefused(f'refusing checkpoint {cp_id}: no CRM token in {TOKEN_FILE} or CRM_TOKEN')
        if guard is not None:
            ok, failed = _guard(guard, root)
            if not ok:
                raise CheckpointRefused(f'eyes_guard failed before checkpoint: {failed}')
            receipt['guard'] = {'before': True, 'after': None, 'failed_after': []}
        receipt['whoami'] = whoami(client)
        if receipt['whoami'].get('principal') != PRINCIPAL:
            raise CheckpointRefused(f'refusing checkpoint {cp_id}: token principal {receipt["whoami"].get("principal")!r} '
                                    f'is not the dedicated {PRINCIPAL!r} principal')
        receipt['archived'] = _archive_previous(root / CHECKPOINTS / cp_id)
    files = file_checkpoint(root, cp, writes=[], dry_run=dry_run)
    receipt['report_sha256'], receipt['files'] = files['report_sha256'], files
    if dry_run:
        _record(root, receipt, tags, line)
        return receipt
    try:
        _write_crm(client, cp, receipt, tags, line, addresses, allow_suppressed)
    except (CrmError, OSError) as error:
        receipt['errors'].append(f'{type(error).__name__}: {error}')
    except BaseException as error:
        receipt['errors'].append(f'aborted by {type(error).__name__}: {error}')
        raise
    finally:
        if guard is not None:
            ok, failed = _guard(guard, root)
            receipt['guard'].update(after=ok, failed_after=failed)
            if not ok:
                receipt['errors'].append(f'eyes_guard failed after checkpoint: {failed}')
        _write_receipt(Path(files['folder']), cp_id, receipt['report_sha256'], receipt['writes'], False,
                       receipt['computed_at'], poi_reused=receipt['poi_reused'], errors=receipt['errors'],
                       ok=not receipt['errors'])
        _record(root, receipt, tags, line)
    return receipt


def _load_cp(root, cp_id, cp_file):
    file = Path(cp_file) if cp_file else Path(root) / 'data/reviews/eyes/checkpoints' / f'{cp_id}.json'
    if not file.exists():
        raise FileNotFoundError(f'checkpoint payload {file} missing; write it or pass --cp FILE')
    cp = json.loads(file.read_text())
    if cp_id and cp.get('id') != cp_id:
        raise ValueError(f'--id {cp_id} does not match payload id {cp.get("id")!r}')
    if cp.get('address_id') is not None and (cp.get('latitude') is None or cp.get('longitude') is None):
        location = address_location(root, cp['address_id'])
        if location:
            cp = {**cp, 'latitude': location['latitude'], 'longitude': location['longitude'],
                  'address_text': cp.get('address_text') or location['full_address']}
    return cp


def main(argv=None):
    parser = argparse.ArgumentParser(description='Publish an eyes checkpoint to the CRM (POI + address tags/note) and file it.')
    parser.add_argument('--root', default='.')
    parser.add_argument('--id', help='Checkpoint id, e.g. CP1 (payload data/reviews/eyes/checkpoints/<id>.json unless --cp).')
    parser.add_argument('--cp', help='Checkpoint payload JSON file.')
    parser.add_argument('--dry-run', action='store_true', help='Write files and the index row; no HTTP.')
    parser.add_argument('--allow-suppressed', action='store_true')
    parser.add_argument('--whoami', action='store_true', help='Print the CRM principal for the configured token and exit.')
    parser.add_argument('--skip-guard', action='store_true', help='Publish without eyes_guard hash checks (not recommended).')
    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        if args.whoami:
            json.dump(whoami(Client(base_url(root), token(root))), sys.stdout)
            sys.stdout.write('\n')
            return 0
        if not (args.id or args.cp):
            parser.print_help()
            return 2
        guard = None
        if not (args.dry_run or args.skip_guard):
            try:  # protected-reference hash checks before/after writes (spec principle 7)
                from scripts.ml import eyes_guard
            except ImportError as error:
                raise CheckpointRefused(f'eyes_guard unavailable ({error}); pass --skip-guard to publish anyway') from error
            guard = eyes_guard.check
        cp = _load_cp(root, args.id, args.cp)
        receipt = publish(root, cp, dry_run=args.dry_run, allow_suppressed=args.allow_suppressed, guard=guard)
        json.dump(receipt, sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write('\n')
        return 0 if receipt['ok'] else 1
    except (CheckpointRefused, CrmError, FileNotFoundError, ValueError, OSError) as error:
        sys.stderr.write(f'{error}\n')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
