#!/usr/bin/env python3
"""Lossless, split checkpoints for trusted native CI workspaces, NOT SDKs.

A full workspace is intentional: restoring objects without the source/generated
input mtimes makes Ninja rebuild them. Never restore across paths, recipes or
runner images; never use a checkpoint as evidence of successful engine tests.
"""
from __future__ import annotations
import argparse
import gzip
import hashlib
import io
import json
import os
import posixpath
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import subprocess
import tarfile
import tempfile

SCHEMA = 3
CHUNK = 1024 * 1024
PART_BYTES = 1024**3
ROOT = Path(__file__).resolve().parents[2]


def digest(path: Path) -> str:
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def relative(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or '\\' in value or ':' in value or '\x00' in value or
            path.is_absolute() or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError(f'Unsafe checkpoint path: {value!r}')
    return value


# Telemetry downloads these benchmark-only credentials separately from Git.
# Never read or upload their bytes. This is not an allow-list for credentials.
OMITTED_FILES = frozenset({
    'download/chromium/src/tools/perf/page_sets/data/credentials.json',
})
SECRET_NAMES = frozenset({'credentials.json', '.git-credentials', '.netrc', '.boto'})


def link_target(name: str, target: str) -> str:
    """Validate a relative target lexically; link chains are also checked later."""
    relative(name)
    if (not target or any(c in target for c in ('\x00', '\n', '\r')) or
            PureWindowsPath(target).drive or target.startswith(('/', '\\'))):
        raise ValueError('Checkpoint refuses absolute or invalid link target: ' + name)
    normalized = target.replace('\\', '/')
    destination = posixpath.normpath(posixpath.join(posixpath.dirname(name), normalized))
    if destination == '..' or destination.startswith('../') or destination.startswith('/'):
        raise ValueError('Checkpoint link escapes workspace: ' + name)
    return normalized


def workspace_entries(root: Path):
    """Never traverse links. Preserve internal symlinks separately from file data."""
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(dirs + files):
            p = Path(directory) / name
            if p.is_symlink():
                yield p
            elif ((hasattr(p, 'is_junction') and p.is_junction()) or
                  getattr(p.lstat(), 'st_file_attributes', 0) & 0x400):
                raise ValueError('Checkpoint refuses unsupported reparse points: ' + str(p))
            elif name in files:
                if not p.is_file():
                    raise ValueError('Not a regular checkpoint file: ' + str(p))
                yield p
            elif p.is_dir():
                yield p


def resolve_link(path: Path) -> Path:
    # Python 3.13+ non-strict resolve may leave symlink loops unresolved.
    # Strict resolution rejects loops; only genuinely missing targets may fall
    # back to lexical resolution (normal for optional platform tools).
    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        return path.resolve(strict=False)


def inspect_entry(path: Path, work: Path) -> tuple[str, dict | None]:
    name = relative(path.relative_to(work).as_posix())
    if name in OMITTED_FILES:
        return 'omit', None
    if path.name.casefold() in SECRET_NAMES:
        raise ValueError('Credentials must not be archived: ' + name)
    if path.is_symlink():
        target = link_target(name, os.readlink(path))
        try:
            resolved = resolve_link(path)
        except (RuntimeError, OSError) as error:
            raise ValueError('Invalid checkpoint link chain: ' + name) from error
        if not resolved.is_relative_to(work):
            raise ValueError('Checkpoint link resolves outside workspace: ' + name)
        # Do not follow credential-bearing links under innocuous filenames.
        if resolved.name.casefold() in SECRET_NAMES:
            raise ValueError('Credential link must not be archived: ' + name)
        return 'link', {'name': name, 'target': target,
                        'mtime_ns': path.lstat().st_mtime_ns,
                        'directory': bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x10)
                                     or resolved.is_dir()}
    if path.is_dir():
        return 'directory', None
    if path.name == 'config' and path.parent.name == '.git':
        config = path.read_text(encoding='utf-8', errors='replace')
        if re.search(r'https?://[^/\s]+@|extraheader\s*=', config, re.I):
            raise ValueError('Credential-bearing Git config cannot be checkpointed')
    return 'file', None


def set_link_mtime(path: Path, timestamp: int) -> None:
    """Restore the link's clock, never its target (Windows Ninja may stat links)."""
    if type(timestamp) is not int or not 0 <= timestamp < 2**63 or not path.is_symlink():
        raise ValueError('Invalid checkpoint symlink timestamp')
    if os.name == 'nt':
        # CPython 3.12 os.utime cannot open a Windows link without following it.
        # OPEN_REPARSE_POINT selects the link; BACKUP_SEMANTICS supports dirs.
        import ctypes
        from ctypes import wintypes
        if timestamp % 100:
            raise ValueError('NTFS cannot preserve sub-100ns link timestamps')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        set_time = kernel.SetFileTime
        set_time.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
        set_time.restype = wintypes.BOOL
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]; close.restype = wintypes.BOOL
        handle = create(str(path), 0x100, 7, None, 3, 0x02200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ticks = timestamp // 100 + 116444736000000000
            value = wintypes.FILETIME(ticks & 0xffffffff, ticks >> 32)
            if not set_time(handle, None, None, ctypes.byref(value)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            close(handle)
    else:
        os.utime(path, ns=(timestamp, timestamp), follow_symlinks=False)
    if path.lstat().st_mtime_ns != timestamp:
        raise ValueError('Filesystem cannot preserve checkpoint symlink timestamp')


def preflight(work: Path) -> dict:
    """Catch unsupported entries before spending hours compiling; save rechecks."""
    work = work.resolve()
    counts = {'files': 0, 'links': 0, 'directories': 0, 'omitted_files': []}
    for path in workspace_entries(work):
        name = path.relative_to(work).as_posix()
        if name.split('/')[0].startswith('cef-sdk-test-'):
            continue
        kind, _ = inspect_entry(path, work)
        if kind == 'omit':
            counts['omitted_files'].append(name)
        else:
            counts['directories' if kind == 'directory' else kind + 's'] += 1
    return counts


class SplitWriter:
    """Stream compression directly to bounded parts; no second huge TAR copy."""
    def __init__(self, directory: Path, limit: int):
        if limit < 1:
            raise ValueError('Positive part limit required')
        self.directory, self.limit = directory, limit
        self.parts, self.stream, self.size, self.hash = [], None, 0, None

    def write(self, data: bytes) -> int:
        count = len(data)
        while data:
            if self.stream is None:
                self.name = f'workspace.tar.gz.part{len(self.parts):04d}'
                self.stream = (self.directory/self.name).open('wb')
                self.hash, self.size = hashlib.sha256(), 0
            piece = data[:self.limit-self.size]
            self.stream.write(piece); self.hash.update(piece)
            self.size += len(piece); data = data[len(piece):]
            if self.size == self.limit:
                self.flush_part()
        return count

    def flush_part(self):
        if self.stream is not None:
            self.stream.close()
            self.parts.append({'name': self.name, 'bytes': self.size,
                               'sha256': self.hash.hexdigest()})
            self.stream = None

    def flush(self):
        if self.stream is not None:
            self.stream.flush()


class PartsReader(io.RawIOBase):
    def __init__(self, directory: Path, parts: list[dict]):
        super().__init__()
        self.paths = iter(directory/item['name'] for item in parts)
        self.stream = None

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            if self.stream is None:
                try:
                    self.stream = next(self.paths).open('rb')
                except StopIteration:
                    return 0
            count = self.stream.readinto(buffer)
            if count:
                return count
            self.stream.close(); self.stream = None

    def close(self):
        if self.stream is not None:
            self.stream.close()
        super().close()


def save(work: Path, destination: Path, identity: dict, *, limit: int = PART_BYTES) -> dict:
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
            with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT) as archive:
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
            len(set(n.casefold() for n in directories)) != len(directories)):
        raise ValueError('Invalid checkpoint directories')
    for name in directories:
        relative(name)
    directory_names = {n.casefold() for n in directories}
    link_names = set()
    for link in links:
        if (not isinstance(link, dict) or not isinstance(link.get('name'), str) or
                not isinstance(link.get('target'), str) or type(link.get('directory')) is not bool or
                type(link.get('mtime_ns')) is not int or not 0 <= link['mtime_ns'] < 2**63):
            raise ValueError('Invalid checkpoint link metadata')
        name = relative(link['name'])
        if (name.casefold() in link_names or name in OMITTED_FILES or
                name.casefold() in directory_names):
            raise ValueError('Duplicate or excluded checkpoint link')
        if PurePosixPath(name).name.casefold() in SECRET_NAMES:
            raise ValueError('Credentials must not be restored')
        link_names.add(name.casefold())
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
                    if (not member.isfile() or name.casefold() in seen or
                            name.casefold() in link_names):
                        raise ValueError('Links, special files or duplicate paths in checkpoint')
                    if name in OMITTED_FILES or PurePosixPath(name).name.casefold() in SECRET_NAMES:
                        raise ValueError('Credentials must not be restored')
                    seen.add(name.casefold()); total += member.size
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
            # Win32 symlink targets must use native separators; the portable
            # manifest intentionally stores forward slashes.
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
    image = os.environ.get('ImageVersion')
    if not image:
        # Fail closed rather than reuse objects with a different MSVC/SDK.
        raise ValueError('ImageVersion is required for hosted Windows checkpoints')
    recipe = hashlib.sha256()
    paths = list((ROOT/'vcpkg/ports/cef-static').rglob('*')) + [
        ROOT/'vcpkg/static/ci.py', Path(__file__), ROOT/'vcpkg/static/windows_slice.py',
        ROOT/'vcpkg/static/triplets/x64-windows-static.cmake']
    for path in sorted(paths):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            recipe.update(path.relative_to(ROOT).as_posix().encode()+b'\0')
            recipe.update(path.read_bytes())
    return {'recipe': recipe.hexdigest(), 'work': str(work.resolve()),
            'repository': os.environ['GITHUB_REPOSITORY'], 'ref': os.environ['GITHUB_REF'],
            'platform': 'windows-x64', 'image': image, 'schema': SCHEMA}


def artifact_name(identity: dict) -> str:
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return 'cef-windows-checkpoint-'+key


def gh_json(*args) -> dict:
    result = subprocess.run(['gh', 'api', *args], capture_output=True, text=True,
                            encoding='utf-8', check=True, timeout=120)
    return json.loads(result.stdout)


def restore_previous(work: Path, package: Path, identity: dict) -> dict | None:
    name = artifact_name(identity)
    repo = identity['repository']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Invalid repository')
    listing = gh_json(f'repos/{repo}/actions/artifacts?per_page=100')
    candidates = sorted(listing['artifacts'], key=lambda a: a['id'], reverse=True)
    branch = identity['ref'].removeprefix('refs/heads/')
    for artifact in candidates:
        run_info = artifact.get('workflow_run', {})
        run_id = run_info.get('id')
        if (artifact.get('expired') or
                not artifact['name'].startswith(name+'-') or run_info.get('head_branch') != branch):
            continue
        same_run = str(run_id) == os.environ['GITHUB_RUN_ID']
        if same_run:
            try:
                if int(artifact['name'].rsplit('-', 1)[-1]) >= int(os.environ['GITHUB_RUN_ATTEMPT']):
                    continue
            except ValueError:
                continue
        run = gh_json(f'repos/{repo}/actions/runs/{run_id}')
        if ((run['status'] != 'completed' and not same_run) or run['head_repository']['full_name'] != repo or
                run['event'] not in ('push', 'workflow_dispatch') or
                run['path'] != '.github/workflows/static-engine-build.yml'):
            continue
        subprocess.run(['gh', 'run', 'download', str(run_id), '--repo', repo,
                        '--name', artifact['name'], '--dir', str(package)], check=True, timeout=3600)
        result = restore(package, work, identity)
        result['restored_from_run'] = run_id
        shutil.rmtree(package)  # owned download only, free space before compilation
        return result
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['restore'])
    args = parser.parse_args()
    if os.name != 'nt':
        raise ValueError('Production checkpoint path is Windows-only')
    work = Path(os.environ.get('CEF_STATIC_WORK') or (Path(os.environ['RUNNER_TEMP'])/'cef-static')).resolve()
    identity = ci_identity(work)
    diagnostics = ROOT/'static-diagnostics/checkpoint'; diagnostics.mkdir(parents=True, exist_ok=True)
    result = restore_previous(work, Path(os.environ['RUNNER_TEMP'])/'cef-checkpoint-download', identity)
    (diagnostics/'restore.json').write_text(json.dumps(result or {'cache_miss': True}, indent=2)+'\n')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('artifact_name='+artifact_name(identity)+'-'+os.environ['GITHUB_RUN_ID']+'-'+os.environ['GITHUB_RUN_ATTEMPT']+'\n')
    print('Checkpoint restored' if result else 'No compatible checkpoint; starting from sources', flush=True)


if __name__ == '__main__':
    main()
