#!/usr/bin/env python3
"""Ubuntu full-workspace checkpoints; not a runtime-tested vcpkg SDK.

Reuse schema-3 streaming and security primitives without changing the Windows
recipe hash. Store hardlinks as regular data; extract names case-sensitively. Keep its
security gates in sync with checkpoint.restore when changing the archive format.
"""
from __future__ import annotations
import argparse
import hashlib
import gzip
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import checkpoint as shared
from checkpoint import (CHUNK, SCHEMA, OMITTED_FILES, SECRET_NAMES, PartsReader,
                        digest, relative, link_target, resolve_link, set_link_mtime,
                        PART_BYTES, SplitWriter, workspace_entries, inspect_entry)

ROOT = Path(__file__).resolve().parents[2]
POLICY_VERSION = 1


def require_linux() -> None:
    if sys.platform != 'linux':
        raise ValueError('Linux checkpoints require a native Linux host')


def save(work: Path, destination: Path, identity: dict, *, limit: int = PART_BYTES) -> dict:
    require_linux()
    work, destination = work.resolve(), destination.resolve()
    if destination.is_relative_to(work) or work.is_relative_to(destination):
        raise ValueError('Checkpoint archive and workspace must be disjoint')
    if destination.exists():
        raise ValueError('Checkpoint destination must not exist')
    destination.mkdir(parents=True)
    writer = SplitWriter(destination, limit)
    total, count = 0, 0
    links, omitted, directories = [], [], []
    try:
        with gzip.GzipFile(fileobj=writer, mode='wb', compresslevel=1, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT, dereference=True) as archive:
                for path in workspace_entries(work):
                    name = relative(path.relative_to(work).as_posix())
                    # ci.py owns disposable SDK/manager sessions; they are not
                    # inputs of the Chromium/Ninja build and may contain reports.
                    if name.split('/')[0].startswith('cef-sdk-test-'):
                        continue
                    kind, record = inspect_entry(path, work)
                    if kind == 'omit':
                        omitted.append(name)
                        continue
                    if kind == 'link':
                        links.append(record)
                        continue
                    if kind == 'directory':
                        directories.append(name)
                        continue
                    info = archive.gettarinfo(str(path), arcname=name)
                    info.uid = info.gid = 0; info.uname = info.gname = ''
                    # tarfile's float mtime loses sub-microsecond precision,
                    # particularly with NTFS. Preserve exact integer times.
                    info.pax_headers['CEF.mtime_ns'] = str(path.stat().st_mtime_ns)
                    with path.open('rb') as stream:
                        archive.addfile(info, stream)
                    total += info.size; count += 1
        writer.flush_part()
        if not count:
            raise ValueError('Refusing an empty checkpoint')
        manifest = {'schema': SCHEMA, 'kind': 'build-checkpoint-not-sdk',
                    'identity': identity, 'files': count, 'unpacked_bytes': total,
                    'parts': writer.parts, 'links': links, 'omitted_files': omitted,
                    'directories': directories,
                    'engine_runtime_verified': False}
        (destination/'checkpoint.json').write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
        return manifest
    except BaseException:
        writer.flush_part()
        # A manifest is the commit record. Never leave one for partial data.
        (destination/'checkpoint.json').unlink(missing_ok=True)
        raise



def restore(package: Path, work: Path, identity: dict) -> dict:
    require_linux()
    package, work = package.resolve(), work.resolve()
    manifest = json.loads((package/'checkpoint.json').read_text(encoding='utf-8'))
    if (manifest.get('schema') != SCHEMA or manifest.get('kind') != 'build-checkpoint-not-sdk'
            or manifest.get('identity') != identity or manifest.get('engine_runtime_verified') is not False):
        raise ValueError('Checkpoint identity/schema mismatch; refusing reuse')
    links, omitted = manifest.get('links'), manifest.get('omitted_files')
    if not isinstance(links, list) or not isinstance(omitted, list):
        raise ValueError('Checkpoint link/exclusion manifest is missing')
    if any(not isinstance(n, str) or n not in OMITTED_FILES for n in omitted):
        raise ValueError('Unreviewed checkpoint exclusion')
    directories = manifest.get('directories')
    if (not isinstance(directories, list) or any(not isinstance(n, str) for n in directories) or
            len(set(directories)) != len(directories)):
        raise ValueError('Invalid checkpoint directories')
    for name in directories:
        relative(name)
    directory_names = set(directories)
    link_names = set()
    for link in links:
        if (not isinstance(link, dict) or not isinstance(link.get('name'), str) or
                not isinstance(link.get('target'), str) or type(link.get('directory')) is not bool or
                type(link.get('mtime_ns')) is not int or not 0 <= link['mtime_ns'] < 2**63):
            raise ValueError('Invalid checkpoint link metadata')
        name = relative(link['name'])
        if (name in link_names or name in OMITTED_FILES or
                name in directory_names):
            raise ValueError('Duplicate or excluded checkpoint link')
        if PurePosixPath(name).name.casefold() in SECRET_NAMES:
            raise ValueError('Credentials must not be restored')
        link_names.add(name)
        link_target(name, link['target'])
    if work.exists() and any(work.iterdir()):
        raise ValueError('Refusing to overwrite an existing source workspace')
    parts = manifest.get('parts')
    if not isinstance(parts, list) or not parts:
        raise ValueError('Checkpoint has no parts')
    for index, part in enumerate(parts):
        if part['name'] != f'workspace.tar.gz.part{index:04d}':
            raise ValueError('Missing, duplicate or reordered checkpoint part')
        p = package/part['name']
        if p.stat().st_size != part['bytes'] or digest(p) != part['sha256']:
            raise ValueError(f'Checkpoint checksum mismatch: {p.name}')
    for field in ('files', 'unpacked_bytes'):
        if type(manifest.get(field)) is not int or manifest[field] < 1:
            raise ValueError('Invalid checkpoint size')
    work.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(work.parent).free < manifest['unpacked_bytes'] + 1024**3:
        raise ValueError('Not enough space to restore checkpoint (archive still occupies disk)')
    stage = Path(tempfile.mkdtemp(prefix='cef-restore-', dir=work.parent))
    seen, total = set(), 0
    try:
        for name in directories:
            (stage/name).mkdir(parents=True, exist_ok=True)
        with PartsReader(package, parts) as raw, io.BufferedReader(raw) as stream:
            with tarfile.open(fileobj=stream, mode='r|gz') as archive:
                for member in archive:
                    name = relative(member.name)
                    if (not member.isfile() or name in seen or
                            name in link_names):
                        raise ValueError('Links, special files or duplicate paths in checkpoint')
                    if name in OMITTED_FILES or PurePosixPath(name).name.casefold() in SECRET_NAMES:
                        raise ValueError('Credentials must not be restored')
                    seen.add(name); total += member.size
                    if total > manifest['unpacked_bytes'] or len(seen) > manifest['files']:
                        raise ValueError('Archive exceeds declared size')
                    path = stage/name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as src, path.open('xb') as dest:
                        shutil.copyfileobj(src, dest, CHUNK)
                    timestamp = int(member.pax_headers['CEF.mtime_ns'])
                    os.utime(path, ns=(timestamp, timestamp))
                    if path.stat().st_mtime_ns != timestamp:
                        raise ValueError('Filesystem cannot preserve checkpoint timestamps')
                    os.chmod(path, member.mode & 0o777)
        if total != manifest['unpacked_bytes'] or len(seen) != manifest['files']:
            raise ValueError('Truncated checkpoint')
        # Materialize symlinks only AFTER all file writes. A symlink in an
        # archive can therefore never redirect extraction outside the staging dir.
        for link in links:
            path = stage / link['name']
            if any(parent.is_symlink() for parent in path.parents if parent != stage.parent):
                raise ValueError('Checkpoint link nested below another link')
            path.parent.mkdir(parents=True, exist_ok=True)
            # Linux retains the validated relative symlink spelling.
            os.symlink(str(Path(link_target(link['name'], link['target']))), path,
                       target_is_directory=link['directory'])
            set_link_mtime(path, link['mtime_ns'])
        for link in links:
            path = stage / link['name']
            try:
                resolved = resolve_link(path)
            except (RuntimeError, OSError) as error:
                raise ValueError('Invalid restored link chain') from error
            if not resolved.is_relative_to(stage):
                raise ValueError('Restored link chain escapes workspace')
            if resolved.name.casefold() in SECRET_NAMES:
                raise ValueError('Credential link must not be restored')
        if work.exists():
            work.rmdir()  # empty only, never remove user data
        stage.rename(work)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return manifest


def ci_identity(work: Path) -> dict:
    require_linux()
    image = os.environ.get('ImageVersion')
    if not image:
        raise ValueError('ImageVersion is required for hosted Linux checkpoints')
    recipe = hashlib.sha256()
    paths = list((ROOT/'vcpkg/ports/cef-static').rglob('*')) + [
        ROOT/'vcpkg/static/ci.py', ROOT/'vcpkg/static/checkpoint.py',
        ROOT/'vcpkg/static/windows_slice.py', Path(__file__),
        ROOT/'vcpkg/static/linux_slice.py',
        ROOT/'vcpkg/static/triplets/x64-linux.cmake']
    for path in sorted(paths):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            recipe.update(path.relative_to(ROOT).as_posix().encode()+b'\0')
            recipe.update(path.read_bytes())
    return {'recipe': recipe.hexdigest(), 'work': str(work.resolve()),
            'repository': os.environ['GITHUB_REPOSITORY'], 'ref': os.environ['GITHUB_REF'],
            'platform': 'linux-x64', 'image': image, 'schema': SCHEMA,
            'posix_policy': POLICY_VERSION}


def artifact_name(identity: dict) -> str:
    if identity.get('platform') != 'linux-x64':
        raise ValueError('A Windows checkpoint cannot be used for Linux')
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return 'cef-linux-checkpoint-'+key


def restore_previous(work: Path, package: Path, identity: dict) -> dict | None:
    """Only completed trusted branch runs, or an earlier attempt of this run."""
    name = artifact_name(identity)
    repo = identity['repository']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Invalid repository')
    branch = identity['ref'].removeprefix('refs/heads/')
    for page in range(1, 6):
        listing = shared.gh_json(f'repos/{repo}/actions/artifacts?per_page=100&page={page}')
        candidates = sorted(listing['artifacts'], key=lambda a: a['id'], reverse=True)
        for artifact in candidates:
            match = re.fullmatch(re.escape(name)+r'-(\d+)-(\d+)', artifact['name'])
            origin = artifact.get('workflow_run', {})
            if (artifact.get('expired') or not match or
                    str(origin.get('id')) != match[1] or origin.get('head_branch') != branch):
                continue
            run_id = origin['id']
            same_run = str(run_id) == os.environ['GITHUB_RUN_ID']
            if same_run and int(match[2]) >= int(os.environ['GITHUB_RUN_ATTEMPT']):
                continue
            run = shared.gh_json(f'repos/{repo}/actions/runs/{run_id}')
            if ((run['status'] != 'completed' and not same_run) or
                    run['head_repository']['full_name'] != repo or run['head_branch'] != branch or
                    run['head_sha'] != origin.get('head_sha') or
                    run['event'] not in ('push', 'workflow_dispatch') or
                    run['path'] != '.github/workflows/static-engine-build.yml'):
                continue
            if package.exists() or (work.exists() and any(work.iterdir())):
                raise ValueError('Refusing to merge downloaded or restored workspaces')
            subprocess.run(['gh', 'run', 'download', str(run_id), '--repo', repo,
                            '--name', artifact['name'], '--dir', str(package)],
                           check=True, timeout=3600)
            result = restore(package, work, identity)
            result.update(restored_from_run=run_id, restored_from_attempt=int(match[2]))
            shutil.rmtree(package)  # owned download only; never remove source data
            return result
        if len(candidates) < 100:
            break
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['restore'])
    parser.parse_args()
    require_linux()
    work = Path(os.environ.get('CEF_STATIC_WORK') or
                (Path(os.environ['RUNNER_TEMP'])/'cef-static')).resolve()
    identity = ci_identity(work)
    diagnostics = ROOT/'static-diagnostics/linux-checkpoint'
    diagnostics.mkdir(parents=True, exist_ok=True)
    result = restore_previous(work, Path(os.environ['RUNNER_TEMP'])/'cef-linux-checkpoint-download', identity)
    (diagnostics/'restore.json').write_text(json.dumps(result or {'cache_miss': True}, indent=2)+'\n')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('artifact_name='+artifact_name(identity)+'-'+os.environ['GITHUB_RUN_ID']+'-'+
                     os.environ['GITHUB_RUN_ATTEMPT']+'\n')
    print('Linux checkpoint restored' if result else 'No Linux checkpoint; starting from sources', flush=True)


if __name__ == '__main__':
    main()
