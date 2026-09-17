#!/usr/bin/env python3
"""Relocatable ownership boundary for a source-built static platform SDK.

Only the archives actually present in the verified GN graph are referenced.
They remain owned by their dependency ports; they are never copied into CEF.
This module does not grant runtime or publication qualification.
"""
from __future__ import annotations
import sys
from pathlib import Path
import gn_platform
import platform_contract as contract


class PlatformExport:
    def __init__(self, selection: dict, receipt: dict, graph: dict,
                 source: Path, out: Path):
        contract.require(sys.platform == 'linux' and isinstance(selection, dict)
                         and set(selection) == {'manifest', 'prefix', 'sha256'},
                         'Static platform export requires explicit native Linux inputs')
        self.prefix = Path(selection['prefix']).resolve(strict=True)
        self.manifest = Path(selection['manifest']).resolve(strict=True)
        self.sha256 = selection['sha256']
        checked = {'manifest': str(self.manifest), 'prefix': str(self.prefix), 'sha256': self.sha256}
        contract.require(receipt.get('platform_build_inputs') == checked,
                         'Reference executable was not verified with these platform inputs')
        self.value = gn_platform.inputs(self.manifest, self.prefix, self.sha256)
        observed = gn_platform.audit_graph(graph, source, out, self.prefix, self.value)
        observed['manifest_sha256'] = self.sha256
        contract.require(receipt.get('platform_graph') == observed,
                         'Reference platform graph differs from export graph')
        self.archives = observed['archives']
        self.seen: set[str] = set()

    def owns(self, path: Path) -> bool:
        """Classify before the exporter's ordinary source-tree ownership check."""
        # Do not resolve a symlink first and accidentally bless its destination.
        if not path.is_relative_to(self.prefix):
            return False
        name = path.relative_to(self.prefix).as_posix()
        contract.regular(self.prefix, name)
        contract.require(name in self.archives, 'Uncaptured or unused platform link input: ' + name)
        self.seen.add(name)
        return True

    def finish(self) -> None:
        contract.require(self.seen == set(self.archives), 'Incomplete external archive export')
        # Recheck after materializing Chromium archives. Detect a concurrently
        # changed dependency instead of publishing a stale hash contract.
        contract.load(self.manifest, self.sha256, self.prefix)

    def cmake(self) -> tuple[list[str], list[str]]:
        lines, targets = [], []
        for index, name in enumerate(self.archives):
            contract.member(name)
            expected = self.value['files'][name]['sha256']
            target = f'CEF::platform_archive_{index}'
            targets.append(target)
            path = '${_cef_static_prefix}/' + name
            lines += [
                f'if(NOT EXISTS "{path}" OR IS_DIRECTORY "{path}" OR IS_SYMLINK "{path}")',
                f'  message(FATAL_ERROR "Missing static CEF dependency: {name}")',
                'endif()',
                f'file(SHA256 "{path}" _cef_platform_hash)',
                f'if(NOT _cef_platform_hash STREQUAL "{expected}")',
                f'  message(FATAL_ERROR "Static CEF dependency changed: {name}")',
                'endif()',
                f'add_library({target} STATIC IMPORTED GLOBAL)',
                f'set_property(TARGET {target} PROPERTY IMPORTED_LOCATION "{path}")',
            ]
        lines += [
            'unset(_cef_platform_hash)',
            'set(CEF_STATIC_DEPENDENCY_PROFILE "static-platform-experimental")',
            'set(CEF_STATIC_PLATFORM_RUNTIME_QUALIFIED FALSE)',
            f'set(CEF_STATIC_PLATFORM_MANIFEST_SHA256 "{self.sha256}")',
        ]
        return lines, targets

    def write_inventory(self, share: Path) -> dict:
        # Keep the original content-addressed, relative-path manifest unchanged.
        (share/'platform-build-inputs.json').write_bytes(self.manifest.read_bytes())
        result = {
            'schema': 1, 'kind': 'external-vcpkg-archives',
            'manifest_sha256': self.sha256,
            'archives': [dict(path=n, **self.value['files'][n]) for n in self.archives],
            'runtime_verified': False,
        }
        (share/'static-platform-inventory.json').write_bytes(contract.canonical(result))
        return result


def prepare(selection, receipt, graph, source, out):
    if selection is None:
        contract.require('platform_build_inputs' not in receipt and not (source/gn_platform.MARKER).exists(),
                         'A platform-built engine requires explicit platform export inputs')
        return None
    return PlatformExport(selection, receipt, graph, source, out)
