"""Revision-bound draft edits through the canonical roof-review API.

This client never opens feedback.json. The app owns authentication, validation,
locking, saved history and persistence; an unavailable app is not a file fallback.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

MAX_REVISION = 9007199254740990
DRAFT_STATUSES = {'unreviewed', 'needs_adjustment'}
ISSUES = {'boundary', 'apex', 'roof_planes', 'vegetation', 'wrong_building'}
OPTIONAL_FIELDS = ('annotation', 'objects', 'model_signature', 'object_reviewed_classes',
                   'apex_verified', 'roof_form_verified', 'objects_verified')


class RoofFeedbackError(ValueError):
    """An anticipated refusal; callers must reload instead of silently retrying."""


def _integer(value, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= MAX_REVISION


def _cut(value):
    return (isinstance(value, list) and len(value) == 2 and all(
        isinstance(point, list) and len(point) == 2 and all(
            isinstance(n, (int, float)) and not isinstance(n, bool) and abs(n) <= 10000 and math.isfinite(n)
            for n in point) for point in value))


def _nonfinite(value):
    raise ValueError(f'Non-finite JSON value {value}')


def _request_json(base, path, body=None):
    token = os.environ.get('CRM_TOKEN')
    headers = {'Accept': 'application/json', **({'Authorization': 'Bearer '+token} if token else {})}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    try:
        encoded = json.dumps(body, allow_nan=False).encode() if body is not None else None
    except (ValueError, TypeError) as error:
        raise RoofFeedbackError('The draft contains invalid JSON fields. Check the saved feedback in the CMS before editing.') from error
    request = Request(base+path, headers=headers, method='POST' if body is not None else 'GET', data=encoded)
    try:
        with urlopen(request, timeout=30) as response:
            value = json.load(response, parse_constant=_nonfinite)
    except HTTPError as error:
        try:
            message = json.loads(error.read()).get('error')
        except (ValueError, AttributeError):
            message = None
        raise RoofFeedbackError(f'{error.code}: {message if isinstance(message, str) else "Roof review request failed."}') from error
    except OSError as error:
        message = ('The roof review API is unavailable. Restore the app and CRM service, then reload before retrying.'
                   if body is None else 'The save response was lost. Reload saved feedback before retrying; the save may have completed.')
        raise RoofFeedbackError(message) from error
    except (ValueError, TypeError) as error:
        raise RoofFeedbackError('The roof review API response could not be read. Reload before retrying; a save may already have completed.') from error
    if not isinstance(value, dict):
        raise RoofFeedbackError('The roof review API response could not be verified. Reload before retrying.')
    return value


def _base_url(base_url):
    base = (base_url or os.environ.get('ADDR_APP_URL', 'http://localhost:3008')).rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RoofFeedbackError('Configure an HTTP(S) app URL without credentials, a query or a fragment.')
    return base


def load_roof_feedback(address_id: int, cohort: str, *, base_url: str | None = None) -> dict:
    """Read one exact roof and its write prerequisites without using local files."""
    if not _integer(address_id, 1) or not isinstance(cohort, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', cohort):
        raise RoofFeedbackError('Choose a positive address ID and a valid explicit cohort.')
    result = _request_json(_base_url(base_url), '/api/reviews?'+urlencode({'cohort': cohort}))
    if result.get('cohort') != cohort or not isinstance(result.get('entries'), list):
        raise RoofFeedbackError('The roof review response identity could not be verified.')
    matching = [entry for entry in result['entries'] if isinstance(entry, dict)
                and _integer(entry.get('address_id'), 1) and entry['address_id'] == address_id]
    if len(matching) != 1:
        raise RoofFeedbackError('This roof is absent or ambiguous in the requested cohort.')
    entry = matching[0]
    if (not isinstance(entry.get('boundary_signature'), str) or not entry['boundary_signature']
            or not isinstance(entry.get('feedback'), dict)
            or not isinstance(entry.get('model_signature'), str) or not re.fullmatch(r'[a-f0-9]{64}', entry['model_signature'])
            or any(key in entry and not isinstance(entry[key], bool) for key in ('stale_review', 'annotation_stale'))):
        raise RoofFeedbackError('The captured boundary or saved feedback could not be verified. Existing reviews are preserved.')
    feedback = entry['feedback']
    if feedback and (not _integer(feedback.get('revision')) or not isinstance(feedback.get('status'), str)
                     or feedback['status'] not in DRAFT_STATUSES | {'approved', 'rejected'}
                     or not isinstance(feedback.get('notes'), str) or len(feedback['notes']) > 5000
                     or not isinstance(feedback.get('boundary_verified'), bool)
                     or not isinstance(feedback.get('issues'), list)
                     or any(not isinstance(issue, str) or issue not in ISSUES for issue in feedback['issues'])
                     or not isinstance(feedback.get('rejected_cuts'), list) or len(feedback['rejected_cuts']) > 500
                     or not all(_cut(cut) for cut in feedback['rejected_cuts'])
                     or any(key in feedback and not isinstance(feedback[key], bool)
                            for key in ('apex_verified', 'roof_form_verified', 'objects_verified'))):
        raise RoofFeedbackError('Saved feedback is incomplete or invalid. Check it in the review CMS; existing reviews are preserved.')
    return {'cohort': cohort, 'address_id': address_id, 'revision': feedback.get('revision', 0),
            'boundary_signature': entry['boundary_signature'], 'model_signature': entry.get('model_signature'),
            'stale_review': entry.get('stale_review', False), 'annotation_stale': entry.get('annotation_stale', False),
            'feedback': copy.deepcopy(feedback), 'review_url': '/reviews?'+urlencode({'cohort': cohort, 'id': address_id})}


def apply_roof_feedback(address_id: int, cohort: str, expected_revision: int, boundary_signature: str, *,
                        action: str = 'notes', cut_coords: list[list[float]] | None = None,
                        status: str = 'needs_adjustment', notes: str | None = None,
                        model_signature: str | None = None, base_url: str | None = None) -> dict:
    """Read-modify-write an unchecked assessment, preserving unrelated saved fields."""
    if action not in ('notes', 'reject_cut', 'restore_cuts', 'set_status'):
        raise RoofFeedbackError('Choose notes, reject_cut, restore_cuts or set_status; other actions are unsupported.')
    if not isinstance(status, str) or status not in DRAFT_STATUSES:
        raise RoofFeedbackError('MCP edits support only unreviewed or needs_adjustment drafts. Human approval and rejection stay in the CMS.')
    if not _integer(expected_revision) or not isinstance(boundary_signature, str) or not boundary_signature:
        raise RoofFeedbackError('Supply the saved revision and captured boundary signature from the review API.')
    if notes is not None and (not isinstance(notes, str) or len(notes) > 5000):
        raise RoofFeedbackError('Notes must be at most 5,000 characters.')
    if action == 'notes' and notes is None:
        raise RoofFeedbackError('Supply notes; an empty string explicitly clears them.')
    if (action == 'reject_cut' and not _cut(cut_coords)) or (action != 'reject_cut' and cut_coords is not None):
        raise RoofFeedbackError('Only reject_cut accepts cut_coords: two finite points in metres.')
    current = load_roof_feedback(address_id, cohort, base_url=base_url)
    if current['revision'] != expected_revision:
        raise RoofFeedbackError('409: This review changed in another session. Reload before saving.')
    if current['boundary_signature'] != boundary_signature:
        raise RoofFeedbackError('409: The captured boundary changed. Reload before saving.')
    saved = current['feedback']
    if saved and saved.get('boundary_signature') != boundary_signature:
        raise RoofFeedbackError('409: Saved feedback belongs to another or unverified boundary. Review it in the CMS before editing.')
    needs_model = action == 'reject_cut' or saved.get('annotation') is not None or saved.get('objects') is not None
    if needs_model and model_signature is None:
        raise RoofFeedbackError('Supply the current model_signature before editing cuts or retaining saved geometry.')
    if model_signature is not None and (not isinstance(model_signature, str)
                                       or not re.fullmatch(r'[a-f0-9]{64}', model_signature)
                                       or current['model_signature'] != model_signature):
        raise RoofFeedbackError('409: The roof model changed. Reload before saving.')
    if current['annotation_stale'] or (needs_model and saved.get('model_signature') not in (None, current['model_signature'])):
        raise RoofFeedbackError('409: Saved geometry is stale. Recover/check it in the CMS before editing.')
    body = {'address_id': address_id, 'cohort': cohort, 'revision': expected_revision, 'boundary_signature': boundary_signature,
            'status': status, 'notes': saved.get('notes', '') if notes is None else notes,
            'boundary_verified': saved.get('boundary_verified', False), 'issues': copy.deepcopy(saved.get('issues', [])),
            'rejected_cuts': copy.deepcopy(saved.get('rejected_cuts', []))}
    body.update({key: copy.deepcopy(saved[key]) for key in OPTIONAL_FIELDS if key in saved})
    if model_signature is not None:
        body['model_signature'] = model_signature
    if action == 'reject_cut' and cut_coords not in body['rejected_cuts']:
        body['rejected_cuts'].append(copy.deepcopy(cut_coords))
    elif action == 'restore_cuts':
        body['rejected_cuts'] = []
    if len(body['rejected_cuts']) > 500:
        raise RoofFeedbackError('A review can retain at most 500 rejected cuts.')
    result = _request_json(_base_url(base_url), '/api/reviews', body)
    feedback = result.get('feedback')
    if (result.get('success') is not True or not _integer(result.get('address_id'), 1)
            or result['address_id'] != address_id or not isinstance(feedback, dict)
            or not _integer(feedback.get('revision'), 1) or feedback['revision'] != expected_revision + 1
            or feedback.get('boundary_signature') != boundary_signature
            or feedback.get('status') != status or feedback.get('notes') != body['notes']
            or feedback.get('rejected_cuts') != body['rejected_cuts']
            or feedback.get('boundary_verified') != body['boundary_verified'] or feedback.get('issues') != body['issues']
            or ('model_signature' in body and feedback.get('model_signature') != body['model_signature'])):
        raise RoofFeedbackError('The save response could not be verified. Reload before retrying; the app may already have saved this draft.')
    return {'cohort': cohort, 'address_id': address_id, 'feedback': feedback, 'training_eligible': False,
            'review_url': current['review_url'], 'note': 'Saved through the canonical API as a draft; no new human approval or training label was granted.'}
