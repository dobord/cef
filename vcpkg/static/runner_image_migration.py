#!/usr/bin/env python3
"""An exact, reviewed legacy Windows image transition; never a wildcard fallback."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import runner_fingerprint as fingerprint


def checked_digest(value: dict) -> str:
    """Require a complete self-consistent inventory, not a supplied digest alone."""
    if (not isinstance(value, dict) or type(value.get('schema')) is not int or value.get('schema') != fingerprint.SCHEMA
            or not isinstance(value.get('components'), dict) or not value['components']):
        raise ValueError('Missing host toolchain inventory')
    for component in value['components'].values():
        if (not isinstance(component, dict) or not isinstance(component.get('path'), str)
                or not component['path'] or type(component.get('files')) is not int
                or component['files'] < 1 or type(component.get('bytes')) is not int
                or component['bytes'] < 0
                or not re.fullmatch(r'[0-9a-f]{64}', str(component.get('sha256')))):
            raise ValueError('Invalid host toolchain component')
    actual = hashlib.sha256(fingerprint.json_bytes(value['components'])).hexdigest()
    if value.get('sha256') != actual:
        raise ValueError('Host toolchain inventory digest mismatch')
    return actual


def input_identity(identity: dict, transition: dict, receipt: Path, *, collect=None) -> dict:
    """Caller must first bind the migration to exact run/SHA/attempt/recipes.

    Only the image field of the INPUT identity may change. Newly produced
    checkpoints retain the real current image; no ImageVersion env override.
    """
    status = {'schema': 1, 'status': 'checking', 'engine_runtime_verified': False,
              'consumer_image': identity.get('image'), 'transition': transition}
    receipt.parent.mkdir(parents=True, exist_ok=True)
    try:
        keys = {'from_image', 'to_image', 'toolchain_sha256', 'collector_sha256',
                'evidence_run', 'evidence_sha'}
        if not isinstance(transition, dict) or set(transition) != keys:
            raise ValueError('Invalid reviewed Windows image transition')
        if identity.get('platform') != 'windows-x64':
            raise ValueError('Windows image transition cannot apply to another platform')
        for key in ('from_image', 'to_image'):
            if not re.fullmatch(r'[0-9]{8}\.[0-9]+\.[0-9]+', str(transition[key])):
                raise ValueError('Invalid reviewed image version')
        if transition['from_image'] == transition['to_image']:
            raise ValueError('Image transition must name two distinct reviewed images')
        for key in ('toolchain_sha256', 'collector_sha256'):
            if not re.fullmatch(r'[0-9a-f]{64}', str(transition[key])):
                raise ValueError('Invalid reviewed fingerprint')
        if (type(transition['evidence_run']) is not int or transition['evidence_run'] < 1
                or not re.fullmatch(r'[0-9a-f]{40}', str(transition['evidence_sha']))):
            raise ValueError('Missing reviewed native evidence provenance')
        if identity.get('image') not in (transition['from_image'], transition['to_image']):
            raise ValueError('Unreviewed runner image; no checkpoint download or cold fallback')
        collector_digest = hashlib.sha256(Path(fingerprint.__file__).read_text(
            encoding='utf-8').encode('utf-8')).hexdigest()
        if collector_digest != transition['collector_sha256']:
            raise ValueError('Host inventory implementation differs from reviewed evidence')
        observed = (collect or fingerprint.collect)()
        status['toolchain'] = observed
        if checked_digest(observed) != transition['toolchain_sha256']:
            raise ValueError('Host build inputs differ from the reviewed producer toolchain')
        result = dict(identity, image=transition['from_image'])
        status.update(status='verified', input_image=result['image'],
                      image_transition_used=identity['image'] != result['image'])
        return result
    except Exception as error:
        status.update(status='failed', error=type(error).__name__ + ': ' + str(error))
        raise
    finally:
        receipt.write_text(json.dumps(status, indent=2) + '\n', encoding='utf-8')
