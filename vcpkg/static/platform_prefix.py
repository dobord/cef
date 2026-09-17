#!/usr/bin/env python3
"""Snapshot vcpkg target inputs for GN/checkpoints, preserving file clocks.

Materialize only internal regular-file aliases. Library format validation is
performed on the copied bytes; symlinks never weaken the closed-input contract.
Host tools are not target inputs and do not travel in the Chromium checkpoint.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import shutil
import stat
import tempfile
import platform_contract as contract
import gn_platform

MAX_BYTES = 12 * 1024**3
MAX_FILES = 200000
# These five files are gettext build-host helpers, not linkable target payload.
# They remain in the original export; only the Chromium target snapshot omits
# them. Unknown executables anywhere else still fail validation.
HOST_TOOLS = frozenset('lib/gettext/' + name for name in
                       ('hostname', 'user-email', 'project-id', 'urlget', 'cldr-plurals'))


def source_file(root: Path, path: Path) -> tuple[Path, str | None]:
    resolved = path.resolve(strict=True)
    contract.require(resolved.is_relative_to(root) and resolved.is_file(),
                     'External or non-file dependency alias: ' + str(path))
    mode = resolved.stat().st_mode
    contract.require(stat.S_ISREG(mode), 'Nonregular dependency: ' + str(path))
    return resolved, resolved.relative_to(root).as_posix() if path.is_symlink() else None


def copy_payload(root: Path, destination: Path) -> list[dict]:
    """Copy include/lib/share without changing the original package install."""
    contract.require(root.is_dir() and not root.is_symlink(), 'A native target prefix is required')
    root = root.resolve(strict=True)
    aliases, total, count = [], 0, 0
    for folder in ('include', 'lib', 'share'):
        base = root / folder
        if not base.exists():
            continue
        contract.require(base.is_dir() and not base.is_symlink(), 'Redirected dependency payload root')
        for directory, dirs, files in os.walk(base, followlinks=False):
            dirs.sort(); files.sort()
            for name in dirs:
                item = Path(directory) / name
                contract.require(not item.is_symlink(), 'Directory aliases require explicit port normalization')
            relative_dir = Path(directory).relative_to(root)
            (destination / relative_dir).mkdir(parents=True, exist_ok=True)
            for name in files:
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                contract.member(relative)
                source, alias = source_file(root, path)
                if relative in HOST_TOOLS:
                    contract.require((root/'share/gettext/copyright').is_file() and alias is None,
                                     'Unowned or redirected gettext host helper')
                    continue
                before = source.stat()
                total += before.st_size; count += 1
                contract.require(total <= MAX_BYTES and count <= MAX_FILES, 'Oversized dependency prefix')
                target = destination / relative
                with source.open('rb') as inp, target.open('xb') as out:
                    shutil.copyfileobj(inp, out, 1024 * 1024)
                after = source.stat()
                contract.require((before.st_size, before.st_mtime_ns, before.st_ino) ==
                                 (after.st_size, after.st_mtime_ns, after.st_ino),
                                 'Dependency changed while snapshotting: ' + relative)
                os.chmod(target, stat.S_IMODE(before.st_mode))
                os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
                with target.open('rb') as stream:
                    magic = stream.read(8)
                if folder == 'lib':
                    contract.require(not ('.so' in name or name.endswith(('.dll', '.dylib'))),
                                     'Shared library in target prefix: ' + relative)
                    if name.endswith('.a') or magic in (b'!<arch>\n', b'!<thin>\n'):
                        contract.archive(target)
                    elif magic[:4] == b'\x7fELF' or magic[:2] == b'MZ':
                        raise ValueError('Shared/executable or foreign ELF payload: ' + relative)
                if alias:
                    aliases.append({'path': relative, 'target': alias, 'sha256': contract.digest(target)})
    contract.require(count > 0, 'Empty dependency prefix')
    return aliases


def freeze(installed: Path, destination: Path, manifest: Path, pkgconf: Path,
           *, modules: list[str] | None = None) -> dict:
    installed = installed.absolute()
    destination = destination.absolute(); manifest = manifest.absolute()
    contract.require(not destination.exists() and not destination.is_symlink() and not manifest.exists() and not manifest.is_symlink(),
                     'A fresh dependency snapshot and manifest are required')
    root = installed.resolve(strict=True)
    contract.require(not destination.is_relative_to(root) and not manifest.is_relative_to(destination),
                     'Recursive dependency snapshot or manifest inside prefix')
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    for path in (destination.parent, manifest.parent):
        contract.require(path.resolve() == path, 'Redirected snapshot parent')
    selected = list(gn_platform.MODULES if modules is None else modules)
    contract.require(selected and len(selected) == len(set(selected)), 'Explicit unique modules required')
    with tempfile.TemporaryDirectory(prefix='.cef-platform-', dir=destination.parent) as temporary:
        stage = Path(temporary) / 'prefix'; stage.mkdir()
        aliases = copy_payload(installed, stage)
        value = contract.capture(stage, pkgconf.resolve(strict=True), selected)
        probe = Path(temporary) / 'platform.json'; probe.write_bytes(contract.canonical(value))
        sha256 = contract.digest(probe)
        contract.load(probe, sha256, stage)
        stage.rename(destination)
        created_manifest = False
        try:
            with manifest.open('xb') as stream:
                created_manifest = True
                stream.write(probe.read_bytes())
        except BaseException:
            shutil.rmtree(destination)
            if created_manifest:
                manifest.unlink(missing_ok=True)
            raise
    return {'schema': 1, 'manifest_sha256': sha256, 'aliases': aliases,
            'modules': sorted(value['modules']), 'runtime_verified': False,
            'omitted_host_tools': sorted(name for name in HOST_TOOLS if (installed/name).is_file())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('installed', 'destination', 'manifest', 'pkgconf'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--modules', nargs='+')
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    value = freeze(args.installed, args.destination, args.manifest, args.pkgconf, modules=args.modules)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_bytes(contract.canonical(value))
    print(value['manifest_sha256'])


if __name__ == '__main__':
    main()
