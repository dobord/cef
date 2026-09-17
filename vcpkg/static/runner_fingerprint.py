#!/usr/bin/env python3
"""Content evidence for a reviewed Windows runner-image transition, not CEF proof.

Do not infer compatibility from version strings or an image label. This script
reads host toolchain files without modifying them. Source-pinned Clang/GN/Ninja
remain covered by the workspace/checkpoint recipe, not by this host inventory.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

SCHEMA = 1


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def file_digest(path: Path) -> str:
    before = path.stat()
    with path.open('rb') as stream:
        result = hashlib.file_digest(stream, 'sha256').hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Host toolchain changed while hashing: ' + str(path))
    return result


def inventory(root: Path, excluded=()) -> dict:
    root = root.resolve(strict=True)
    rows = []
    if root.is_file():
        files = [root]
    else:
        files = []
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in excluded)
            for name in dirs + sorted(names):
                path = Path(directory) / name
                if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                    raise ValueError('Redirected host toolchain entry: ' + str(path))
                if name in names:
                    files.append(path)
    for path in sorted(files):
        if not path.is_file():
            raise ValueError('Non-regular host toolchain entry: ' + str(path))
        relative = path.relative_to(root).as_posix() if path != root else path.name
        rows.append([relative, path.stat().st_size, file_digest(path)])
    if not rows:
        raise ValueError('Empty host toolchain component: ' + str(root))
    return {'path': root.as_posix(), 'files': len(rows),
            'bytes': sum(r[1] for r in rows),
            'sha256': hashlib.sha256(json_bytes(rows)).hexdigest()}


def collect() -> dict:
    if os.name != 'nt':
        raise ValueError('This inventory is specific to native Windows x64')
    program = Path(os.environ['ProgramFiles'])
    program86 = Path(os.environ['ProgramFiles(x86)'])
    vswhere = program86/'Microsoft Visual Studio/Installer/vswhere.exe'
    text = subprocess.check_output([str(vswhere), '-latest', '-products', '*',
        '-version', '[17.0,18.0)', '-requires',
        'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property',
        'installationPath'], text=True, timeout=60).strip()
    if not text or '\n' in text:
        raise ValueError('Ambiguous or absent Visual Studio 2022')
    vs = Path(text)
    cmake = shutil.which('cmake.exe')
    if not cmake:
        raise ValueError('Native CMake not found')
    cmake_root = Path(cmake).resolve().parents[1]
    sdk = program86/'Windows Kits/10'
    python = Path(sys.base_prefix)
    roots = {
        'vswhere': vswhere,
        'msvc': vs/'VC/Tools/MSVC',
        'vc-build': vs/'VC/Auxiliary/Build',
        'vc-include': vs/'VC/Auxiliary/VS/include',
        'msbuild': vs/'MSBuild/Current',
        'ninja': vs/'Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe',
        'sdk-include': sdk/'Include', 'sdk-lib': sdk/'Lib', 'sdk-bin': sdk/'bin',
        'netfx-sdk': program86/'Windows Kits/NETFXSDK/4.8',
        'cmake-bin': cmake_root/'bin', 'cmake-share': cmake_root/'share',
        'python-exe': Path(sys.executable), 'python-dlls': python/'DLLs',
        'python-stdlib': python/'Lib',
        'git-bin': program/'Git/bin', 'git-cmd': program/'Git/cmd',
        'git-core': program/'Git/mingw64/libexec/git-core',
    }
    for dll in sorted(python.glob('python*.dll')):
        roots['python-' + dll.name] = dll
    components = {}
    for name, path in sorted(roots.items()):
        print('Fingerprinting ' + name, flush=True)
        components[name] = inventory(path, excluded=('__pycache__', 'site-packages'))
    return {'schema': SCHEMA, 'components': components,
            'sha256': hashlib.sha256(json_bytes(components)).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    value = {'image': os.environ.get('ImageVersion'), 'image_os': os.environ.get('ImageOS'),
             'run_id': os.environ.get('GITHUB_RUN_ID'), 'sha': os.environ.get('GITHUB_SHA'),
             'toolchain': collect(), 'engine_verified': False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(json_bytes(value))
    print(json.dumps(value, indent=2), flush=True)


if __name__ == '__main__':
    main()
