#!/usr/bin/env python3
"""Stream a verified SDK ZIP into bounded assets; verify/join without extraction.

Parts are byte slices of ONE standard ZIP/ZIP64, not independent ZIP volumes.
No executable code from a manifest is evaluated. Checksums prove integrity,
not authenticity: obtain this script and the manifest from a trusted release.
"""
from __future__ import annotations
import argparse
import bisect
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile

ASSET_LIMIT = 2 * 1024**3  # GitHub: each release asset must be strictly smaller.
PART_BYTES = 1_900_000_000
BLOCK = 1024 * 1024
MAX_PARTS = 256
FORMAT = 'cef-sdk-zip-parts-v1'
HEX = re.compile(r'[0-9a-f]{64}')
BASE = re.compile(r'cef-152\.0\.6-(x64-linux|x64-windows-static)-static-engine-capi')
PINS = {'cef_commit': '708dc140cbc3286826a8abef89dc23a44ff9ea72',
        'chromium_commit': '79460ebecaa5625e57a5fb679a735659e73dc687',
        'vcpkg_commit': '3723ec118c8354290925feb58d021a9205a3e772'}
VERSION = '152.0.6+g708dc14+chromium-152.0.7977.83'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def dump(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2)+'\n', encoding='utf-8', newline='\n')


def regular(path: Path) -> None:
    require(stat.S_ISREG(path.lstat().st_mode), 'Not a regular file: '+str(path))


def record(path: Path) -> dict:
    regular(path)
    with path.open('rb') as f:
        digest = hashlib.file_digest(f, 'sha256').hexdigest()
    return {'name': path.name, 'size': path.stat().st_size, 'sha256': digest}


def validate_receipt(receipt: dict, expected_commit: str | None = None) -> str:
    require(receipt.get('schema') == 1, 'Unknown SDK receipt schema')
    commit = receipt.get('integration_commit', '')
    require(isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40}', commit), 'Invalid source commit')
    require(expected_commit is None or commit == expected_commit, 'SDK provenance mismatch')
    require(all(receipt.get(k) == v for k, v in PINS.items()), 'SDK source pins mismatch')
    triplet = receipt.get('triplet')
    require(triplet in ('x64-linux', 'x64-windows-static'), 'Invalid SDK triplet')
    require(receipt.get('engine_linkage') == 'static' and receipt.get('configuration') == 'Release',
            'Not a Release static engine SDK')
    for k in ('sdk_relocation_verified', 'application_relocation_verified', 'capi_only'):
        require(receipt.get(k) is True, 'Missing SDK verification: '+k)
    proof = receipt.get('smoke', {})
    require(proof.get('cef') == VERSION and proof.get('engine') == 'static', 'Wrong smoke engine/version')
    for k in ('javascript', 'paint', 'browser_modules_clean', 'renderer_modules_clean'):
        require(proof.get(k) is True, 'Missing smoke proof: '+k)
    pids = [proof.get(k) for k in ('browser_pid', 'renderer_pid')]
    require(all(type(p) is int and p > 0 for p in pids) and pids[0] != pids[1], 'Missing separate renderer')
    require(HEX.fullmatch(receipt.get('executable_sha256', '')), 'Missing executable digest')
    runs = receipt.get('runtime_runs', {})
    count = 3 if triplet == 'x64-windows-static' else 1
    require(runs.get('status') == 'success' and runs.get('engine_runtime_verified') is True
            and runs.get('no_retry_on_failure') is True, 'Missing repeated-runtime proof')
    require(runs.get('required_runs') == count and runs.get('passed_runs') == count,
            'Incomplete runtime repetitions')
    require(runs.get('executable_sha256') == receipt['executable_sha256'], 'Runtime executable mismatch')
    rows = runs.get('runs', [])
    require(isinstance(rows, list) and len(rows) == count, 'Incomplete runtime records')
    require(all(row.get('number') == i and row.get('status') == 'success'
                and row.get('engine_runtime_verified') is True
                and isinstance(row.get('cwd'), str) and row['cwd']
                for i, row in enumerate(rows, 1)), 'Failed runtime repetition')
    require(len({row['cwd'] for row in rows}) == count, 'Repeated runtime profile')
    return triplet


class PartWriter:
    """Append-only ZIP sink. Never holds a full asset or a duplicate ZIP in RAM/disk."""
    def __init__(self, folder: Path, archive: str, capacity: int):
        require(type(capacity) is int and 0 < capacity < ASSET_LIMIT, 'Invalid part capacity')
        self.folder, self.archive, self.capacity = folder, archive, capacity
        self.parts, self.total, self.used = [], 0, 0
        self.digest, self.part_digest, self.file = hashlib.sha256(), None, None

    def tell(self):
        return self.total

    def flush(self):
        if self.file is not None:
            self.file.flush()

    def close(self):
        if self.file is not None:
            self.file.close()
            self.parts.append({'name': Path(self.file.name).name, 'size': self.used,
                               'sha256': self.part_digest.hexdigest()})
            self.file = None

    def write(self, data):
        view = memoryview(data)
        length = len(view)
        while view:
            if self.file is None:
                require(len(self.parts) < MAX_PARTS, 'Too many SDK parts')
                self.file = (self.folder/(self.archive+f'.part{len(self.parts)+1:03d}')).open('xb')
                self.used, self.part_digest = 0, hashlib.sha256()
            chunk = view[:min(len(view), self.capacity-self.used, BLOCK)]
            require(self.file.write(chunk) == len(chunk), 'Short SDK asset write')
            self.part_digest.update(chunk); self.digest.update(chunk)
            self.used += len(chunk); self.total += len(chunk)
            view = view[len(chunk):]
            if self.used == self.capacity:
                self.close()
        return length


class PartReader(io.RawIOBase):
    """Seekable concatenation for ZIP64/CRC validation, without disk reassembly."""
    def __init__(self, folder: Path, parts: list[dict]):
        super().__init__()
        self.folder, self.parts, self.position = folder, parts, 0
        self.ends = []
        for part in parts:
            self.ends.append((self.ends[-1] if self.ends else 0)+part['size'])
        self.handle, self.index = None, -1

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.position

    def seek(self, offset, whence=0):
        require(whence in (0, 1, 2), 'Invalid seek mode')
        position = offset + (self.position if whence == 1 else self.ends[-1] if whence == 2 else 0)
        require(position >= 0, 'Negative seek')
        self.position = position
        return position

    def read(self, size=-1):
        remaining = max(0, self.ends[-1]-self.position)
        if size < 0: size = remaining
        size = min(size, remaining)
        output = bytearray()
        while size:
            index = bisect.bisect_right(self.ends, self.position)
            start = self.ends[index-1] if index else 0
            if self.index != index:
                if self.handle is not None: self.handle.close()
                self.handle = (self.folder/self.parts[index]['name']).open('rb')
                self.index = index
            self.handle.seek(self.position-start)
            chunk = self.handle.read(min(size, self.ends[index]-self.position, BLOCK))
            require(chunk, 'Truncated SDK part during ZIP read')
            output.extend(chunk); self.position += len(chunk); size -= len(chunk)
        return bytes(output)

    def close(self):
        if self.handle is not None: self.handle.close()
        super().close()


def safe_member(name: str, root: str) -> None:
    require(name.startswith(root+'/') and '\\' not in name and ':' not in name
            and '\0' not in name and not name.endswith('/'), 'Unsafe SDK ZIP member: '+name)
    require(all(p not in ('', '.', '..') for p in name.split('/')), 'Unsafe SDK ZIP path')


def verify_bundle(manifest_path: Path, expected_commit: str | None = None) -> dict:
    regular(manifest_path)
    require(manifest_path.stat().st_size < 1024**2, 'Manifest too large')
    data = json.loads(manifest_path.read_text(encoding='utf-8'))
    require(data.get('schema') == 1 and data.get('format') == FORMAT, 'Unknown distribution format')
    name = data.get('name', '')
    require(isinstance(name, str) and BASE.fullmatch(name), 'Invalid SDK distribution name')
    require(manifest_path.name == name+'.manifest.json', 'Manifest name mismatch')
    archive, parts, metadata = data['archive'], data['parts'], data['metadata']
    require(archive.get('name') == name+'.zip' and HEX.fullmatch(archive.get('sha256', '')),
            'Invalid logical ZIP record')
    limit = data['part_bytes']
    require(type(limit) is int and 0 < limit < ASSET_LIMIT, 'Invalid part capacity')
    require(isinstance(parts, list) and 1 <= len(parts) <= MAX_PARTS, 'Invalid part count')
    for i, part in enumerate(parts, 1):
        expected = name+'.zip'+(f'.part{i:03d}' if len(parts) > 1 else '')
        require(part.get('name') == expected, 'Missing, reordered or unsafe SDK part name')
        require(type(part.get('size')) is int and 0 < part['size'] <= limit, 'Invalid part size')
        require(i == len(parts) or part['size'] == limit, 'Nonfinal short SDK part')
    require(type(archive.get('size')) is int and sum(p['size'] for p in parts) == archive['size'],
            'Logical ZIP length mismatch')
    expected_metadata = {'receipt': name+'.json', 'restore': name+'-restore.py',
                         'readme': name+'-README.txt', 'checksum': name+'.zip.sha256'}
    require(set(metadata) == set(expected_metadata), 'Missing distribution metadata')
    for role, expected in expected_metadata.items():
        require(metadata[role].get('name') == expected, 'Unsafe metadata name')
    folder = manifest_path.parent
    full_hash = hashlib.sha256()
    for entry in parts+list(metadata.values()):
        require(type(entry.get('size')) is int and 0 < entry['size'] < ASSET_LIMIT
                and HEX.fullmatch(entry.get('sha256', '')), 'Invalid asset metadata')
        file = folder/entry['name']; regular(file)
        require(file.stat().st_size == entry['size'], 'SDK asset size mismatch: '+file.name)
        h = hashlib.sha256()
        with file.open('rb') as f:
            while block := f.read(BLOCK):
                h.update(block)
                if entry in parts: full_hash.update(block)
        require(h.hexdigest() == entry['sha256'], 'SDK asset digest mismatch: '+file.name)
    require(full_hash.hexdigest() == archive['sha256'], 'Logical ZIP digest mismatch')
    require((folder/(name+'.zip.sha256')).read_text(encoding='utf-8') ==
            archive['sha256']+'  '+archive['name']+'\n', 'Logical ZIP checksum mismatch')
    require({p.name for p in folder.glob(name+'.zip.part*')} ==
            ({p['name'] for p in parts} if len(parts) > 1 else set()), 'Extra or mixed SDK parts')
    outer = (folder/(name+'.json')).read_bytes()
    require(len(outer) < 1024**2, 'SDK receipt too large')
    receipt = json.loads(outer)
    triplet = validate_receipt(receipt, expected_commit)
    require(BASE.fullmatch(name).group(1) == triplet, 'SDK platform mismatch')
    # Reading every member checks its CRC. Binding the internal receipt prevents
    # a valid unrelated ZIP being substituted beside a valid external receipt.
    with PartReader(folder, parts) as stream, zipfile.ZipFile(stream) as bundle:
        seen = set()
        for entry in bundle.infolist():
            safe_member(entry.filename, name)
            key = entry.filename.casefold() if triplet == 'x64-windows-static' else entry.filename
            require(key not in seen, 'Duplicate SDK ZIP member'); seen.add(key)
            kind = stat.S_IFMT(entry.external_attr >> 16)
            require(kind in (0, stat.S_IFREG), 'Nonregular SDK ZIP member')
            require(not (entry.flag_bits & 1), 'Encrypted SDK member')
            with bundle.open(entry) as f:
                while f.read(BLOCK): pass
        internal = bundle.getinfo(name+'/static-sdk-receipt.json')
        require(internal.file_size == len(outer) and bundle.read(internal) == outer,
                'Internal and external SDK receipts differ')
    return {'name': name, 'triplet': triplet, 'archive': archive, 'part_count': len(parts),
            'files': [manifest_path.name]+[p['name'] for p in parts]+[p['name'] for p in metadata.values()],
            'integrity_verified': True, 'zip_crc_verified': True,
            'integration_commit': receipt['integration_commit']}


def package_sdk(sdk: Path, destination: Path, name: str, receipt: dict,
                diagnostics: Path, *, part_bytes: int = PART_BYTES,
                compression: int = zipfile.ZIP_DEFLATED) -> dict:
    """Publish an all-or-nothing directory after complete integrity validation."""
    require(BASE.fullmatch(name), 'Invalid SDK name')
    validate_receipt(receipt)
    require(sdk.is_dir() and not sdk.is_symlink(), 'Invalid SDK directory')
    require(not destination.is_symlink() and
            (not destination.exists() or (destination.is_dir() and not any(destination.iterdir()))),
            'Refusing to mix or overwrite SDK distributions')
    destination.parent.mkdir(parents=True, exist_ok=True)
    diagnostics.parent.mkdir(parents=True, exist_ok=True)
    status = {'schema': 1, 'status': 'packing', 'name': name, 'part_bytes': part_bytes}
    dump(diagnostics, status)
    try:
        dump(sdk/'static-sdk-receipt.json', receipt)
        inventory = []
        for file in sorted(sdk.rglob('*')):
            require(not file.is_symlink(), 'SDK link cannot be silently omitted or followed: '+str(file))
            if file.is_dir(): continue
            regular(file)
            arcname = name+'/'+file.relative_to(sdk).as_posix()
            safe_member(arcname, name)
            inventory.append((file, arcname, file.stat().st_size, file.stat().st_mtime_ns))
        with tempfile.TemporaryDirectory(prefix='.cef-sdk-package-', dir=destination.parent) as temp:
            stage = Path(temp)
            writer = PartWriter(stage, name+'.zip', part_bytes)
            try:
                with zipfile.ZipFile(writer, 'w', compression, allowZip64=True,
                                     compresslevel=6 if compression == zipfile.ZIP_DEFLATED else None) as bundle:
                    for file, arcname, size, timestamp in inventory:
                        bundle.write(file, arcname)
                        require(file.stat().st_size == size and file.stat().st_mtime_ns == timestamp,
                                'SDK source changed during packaging')
            finally:
                writer.close()
            require(writer.parts, 'Empty SDK ZIP')
            if len(writer.parts) == 1:
                (stage/writer.parts[0]['name']).rename(stage/(name+'.zip'))
                writer.parts[0]['name'] = name+'.zip'
            archive = {'name': name+'.zip', 'size': writer.total, 'sha256': writer.digest.hexdigest()}
            status.update(archive_bytes=writer.total, part_count=len(writer.parts), status='verifying')
            dump(diagnostics, status)
            dump(stage/(name+'.json'), receipt)
            shutil.copyfile(Path(__file__), stage/(name+'-restore.py'))
            (stage/(name+'.zip.sha256')).write_text(archive['sha256']+'  '+archive['name']+'\n', encoding='utf-8')
            (stage/(name+'-README.txt')).write_text(
                'Static CEF SDK distribution. Download ALL assets with this SDK prefix.\n'
                'The .zip.partNNN files are byte slices, not independent ZIP files.\n'
                f'Verify: python "{name}-restore.py" verify "{name}.manifest.json"\n'
                f'Join parts: python "{name}-restore.py" join "{name}.manifest.json" --output "{name}.zip"\n'
                'A single .zip is already usable; do not join it onto itself.\n'
                'Requires Python 3.11 or newer. Then extract with a ZIP64-capable tool.\n'
                'Verification requires no duplicate archive on disk; joining needs the logical ZIP size free.\n'
                'Hashes check integrity, not publisher authenticity. Trust the release source.\n'
                'This Release C-API SDK has a static CEF engine, not fully static OS libraries.\n', encoding='utf-8')
            manifest = {'schema': 1, 'format': FORMAT, 'name': name, 'archive': archive,
                        'part_bytes': part_bytes, 'parts': writer.parts,
                        'metadata': {role: record(stage/(name+suffix)) for role, suffix in
                                     [('receipt', '.json'), ('restore', '-restore.py'),
                                      ('readme', '-README.txt'), ('checksum', '.zip.sha256')]}}
            manifest_path = stage/(name+'.manifest.json'); dump(manifest_path, manifest)
            result = verify_bundle(manifest_path, receipt['integration_commit'])
            # Directory rename on the same filesystem; failed verification never
            # leaves uploadable output. Never merge with another run's directory.
            if destination.exists(): destination.rmdir()
            stage.rename(destination)
        status.update(result, status='success'); dump(diagnostics, status)
        print(json.dumps(status), flush=True)
        return result
    except Exception as error:
        status.update(status='failed', error=f'{type(error).__name__}: {error}')
        dump(diagnostics, status)
        raise


def join_bundle(manifest_path: Path, output: Path) -> None:
    result = verify_bundle(manifest_path)
    require(not output.exists() and not output.is_symlink(), 'Refusing to overwrite joined ZIP')
    data = json.loads(manifest_path.read_text(encoding='utf-8'))
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.cef-sdk-join-', dir=output.parent)
    try:
        h, size = hashlib.sha256(), 0
        with os.fdopen(fd, 'wb') as out:
            for part in data['parts']:
                with (manifest_path.parent/part['name']).open('rb') as f:
                    while block := f.read(BLOCK):
                        out.write(block); h.update(block); size += len(block)
        require(size == result['archive']['size'] and h.hexdigest() == result['archive']['sha256'],
                'Parts changed during reconstruction')
        # Atomic no-replace publication (NTFS/ext4); do not clobber a racing file.
        os.link(temp, output)
    finally:
        Path(temp).unlink(missing_ok=True)


def verify_release(root: Path, expected_commit: str) -> list[str]:
    files = []
    for platform, triplet in [('linux', 'x64-linux'), ('windows', 'x64-windows-static')]:
        folder = root/platform
        require(folder.is_dir() and not folder.is_symlink(), 'Missing native SDK directory')
        manifests = list(folder.glob('*.manifest.json'))
        require(len(manifests) == 1, 'Expected one manifest per native SDK')
        result = verify_bundle(manifests[0], expected_commit)
        require(result['triplet'] == triplet, 'Wrong release platform')
        require({p.name for p in folder.iterdir()} == set(result['files']), 'Unexpected release assets')
        files += [str(folder/n) for n in result['files']]
    require(len(files) <= 1000 and len({Path(f).name for f in files}) == len(files), 'Ambiguous asset set')
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('verify'); p.add_argument('manifest', type=Path)
    p = sub.add_parser('join'); p.add_argument('manifest', type=Path); p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('release'); p.add_argument('root', type=Path); p.add_argument('--commit', required=True)
    p.add_argument('--file-list', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'verify': print(json.dumps(verify_bundle(args.manifest), indent=2))
    elif args.command == 'join': join_bundle(args.manifest, args.output)
    else:
        # A stale allow-list can never survive a failed verification.
        args.file_list.unlink(missing_ok=True)
        files = verify_release(args.root, args.commit)
        args.file_list.write_text(json.dumps(files, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__': main()
