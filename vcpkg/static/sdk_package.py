#!/usr/bin/env python3
"""Stream a ZIP64 SDK into bounded assets; verify or assemble without extraction.

Python 3.11+, standard library only. Packaging is NOT a CEF runtime test.
Release verification additionally requires current-commit native SDK receipts.
"""
from __future__ import annotations
import argparse
import bisect
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import zipfile

PART_BYTES = 1024**3
ASSET_LIMIT = 2 * 1024**3  # GitHub requires each release asset to be BELOW 2 GiB.
BLOCK = 4 * 1024**2
MAX_PARTS = 128
VERSION = '152.0.6+g708dc14+chromium-152.0.7977.83'
CEF = '708dc140cbc3286826a8abef89dc23a44ff9ea72'
CHROMIUM = '79460ebecaa5625e57a5fb679a735659e73dc687'
VCPKG = '3723ec118c8354290925feb58d021a9205a3e772'
TRIPLETS = {'linux': 'x64-linux', 'windows': 'x64-windows-static'}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def integer(value, minimum=0, maximum=MAX_PARTS * PART_BYTES):
    require(type(value) is int and minimum <= value <= maximum, 'Invalid integer or size')
    return value


def safe_name(value: str) -> str:
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,180}', value)
            and '..' not in value and not value.endswith('.'), 'Unsafe asset name')
    return value


def hash_file(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True)+'\n').encode('utf-8')


def read_json(path: Path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 32*1024**2,
            'Missing, redirected or oversized JSON')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)


def file_record(path: Path) -> dict:
    return {'name': path.name, 'size': path.stat().st_size, 'sha256': hash_file(path)}


def check_record(folder: Path, item: dict, expected: str) -> Path:
    require(set(item) == {'name', 'size', 'sha256'}, 'Invalid asset record')
    require(item['name'] == safe_name(expected), 'Unexpected asset name or order')
    integer(item['size'], 1, ASSET_LIMIT-1)
    require(isinstance(item['sha256'], str) and re.fullmatch('[0-9a-f]{64}', item['sha256']),
            'Invalid SHA-256')
    path = folder/expected
    require(not path.is_symlink() and path.is_file(), 'Missing or redirected asset: '+expected)
    require(path.stat().st_size == item['size'], 'Asset size mismatch: '+expected)
    require(hash_file(path) == item['sha256'], 'Asset digest mismatch: '+expected)
    return path


class SplitWriter(io.RawIOBase):
    """Unseekable ZIP sink: only one part and bounded caller buffers in memory."""
    def __init__(self, folder: Path, name: str, limit: int):
        super().__init__()
        self.folder, self.name = folder, safe_name(name)
        self.limit = integer(limit, 1, PART_BYTES)
        self.total = 0
        self.digest = hashlib.sha256()
        self.parts = []
        self.stream = None
        self.part_digest = None
        self.part_size = 0
        self.finished = False

    def writable(self):
        return True

    def tell(self):
        return self.total

    def _end_part(self):
        if self.stream is not None:
            self.stream.close()
            self.parts.append({'name': Path(self.stream.name).name, 'size': self.part_size,
                               'sha256': self.part_digest.hexdigest()})
            self.stream = None

    def write(self, data):
        require(not self.finished, 'Archive writer already finished')
        view = memoryview(data)
        size = len(view)
        while view:
            if self.stream is None:
                require(len(self.parts) < MAX_PARTS, 'Too many SDK parts')
                filename = f'{self.name}.zip.part{len(self.parts)+1:03d}'
                self.stream = (self.folder/filename).open('xb')
                self.part_digest, self.part_size = hashlib.sha256(), 0
            count = min(len(view), self.limit-self.part_size)
            block = view[:count]
            written = self.stream.write(block)
            require(written == count, 'Short SDK write')
            self.part_digest.update(block)
            self.digest.update(block)
            self.part_size += count
            self.total += count
            view = view[count:]
            if self.part_size == self.limit:
                self._end_part()
        return size

    def finish(self) -> dict:
        require(not self.finished, 'Archive already finalized')
        self._end_part()
        self.finished = True
        require(self.total > 0, 'Empty archive')
        if len(self.parts) == 1:
            item = self.parts[0]
            (self.folder/item['name']).rename(self.folder/(self.name+'.zip'))
            item['name'] = self.name+'.zip'
        return {'name': self.name+'.zip', 'size': self.total, 'sha256': self.digest.hexdigest()}

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        super().close()


class PartsReader(io.RawIOBase):
    """Seek across contiguous ZIP parts without making a second multi-GB file."""
    def __init__(self, paths: list[Path]):
        super().__init__()
        self.paths, self.offsets, self.position = paths, [0], 0
        for path in paths:
            self.offsets.append(self.offsets[-1]+path.stat().st_size)

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        require(whence in (0, 1, 2), 'Invalid seek origin')
        position = offset + (self.position if whence == 1 else self.offsets[-1] if whence == 2 else 0)
        require(position >= 0, 'Negative archive position')
        self.position = position
        return position

    def read(self, size=-1):
        available = max(0, self.offsets[-1]-self.position)
        size = available if size < 0 else min(size, available)
        # ZIP metadata is small; fail rather than allocate attacker-sized buffers.
        require(size <= 64*1024**2, 'Oversized ZIP metadata read')
        chunks = []
        while size:
            index = bisect.bisect_right(self.offsets, self.position)-1
            count = min(size, self.offsets[index+1]-self.position)
            with self.paths[index].open('rb') as stream:
                stream.seek(self.position-self.offsets[index])
                data = stream.read(count)
            require(len(data) == count, 'Truncated archive part')
            chunks.append(data)
            self.position += count
            size -= count
        return b''.join(chunks)


def validate_entry(path: str, name: str) -> None:
    require(isinstance(path, str) and not any(ord(c) < 32 for c in path)
            and '\\' not in path and ':' not in path, 'Unsafe ZIP path')
    p = PurePosixPath(path)
    require(not p.is_absolute() and len(p.parts) >= 2 and p.parts[0] == name
            and all(s not in ('', '.', '..') for s in path.split('/')), 'ZIP path escapes SDK')


def verify_bundle(manifest: Path, *, strict=True) -> dict:
    data = read_json(manifest)
    require(set(data) == {'schema', 'format', 'name', 'archive', 'part_bytes', 'parts',
                          'receipt', 'helpers', 'entries'}, 'Invalid bundle manifest')
    require(data['schema'] == 1 and data['format'] == 'cef-sdk-zip-parts-v1', 'Unknown bundle format')
    name = safe_name(data['name'])
    require(manifest.name == name+'.manifest.json', 'Manifest name mismatch')
    folder = manifest.parent
    archive = data['archive']
    require(set(archive) == {'name', 'size', 'sha256'} and archive['name'] == name+'.zip',
            'Invalid logical ZIP name')
    total = integer(archive['size'], 1)
    limit = integer(data['part_bytes'], 1, PART_BYTES)
    count = (total+limit-1)//limit
    require(isinstance(data['parts'], list) and len(data['parts']) == count <= MAX_PARTS,
            'Incomplete SDK parts')
    paths, digest = [], hashlib.sha256()
    for index, item in enumerate(data['parts']):
        expected = name+'.zip' if count == 1 else f'{name}.zip.part{index+1:03d}'
        path = check_record(folder, item, expected)
        require(item['size'] == min(limit, total-index*limit), 'Non-contiguous part sizes')
        paths.append(path)
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(BLOCK), b''):
                digest.update(block)
    require(digest.hexdigest() == archive['sha256'], 'Reassembled ZIP digest mismatch')
    receipt = check_record(folder, data['receipt'], name+'.json')
    require(receipt.stat().st_size <= 2*1024**2, 'Oversized SDK receipt')
    require(isinstance(data['helpers'], list) and len(data['helpers']) == 2, 'Missing restore instructions')
    helpers = [check_record(folder, data['helpers'][i], name+suffix)
               for i, suffix in enumerate(('-assemble.py', '-README.txt'))]
    expected_files = {manifest.name, receipt.name, *(p.name for p in paths+helpers)}
    if strict:
        require({p.name for p in folder.iterdir()} == expected_files, 'Unexpected files in SDK artifact')
    entries = data['entries']
    require(isinstance(entries, list) and 0 < len(entries) <= 100000, 'Invalid SDK inventory')
    expected_entries = {}
    folded = set()
    for entry in entries:
        require(set(entry) == {'path', 'size', 'sha256', 'mode'}, 'Invalid ZIP inventory entry')
        path = entry['path']
        validate_entry(path, name)
        require(path.casefold() not in folded, 'Duplicate or case-colliding ZIP entry')
        folded.add(path.casefold())
        integer(entry['size'])
        integer(entry['mode'], 0, 0o777)
        expected_entries[path] = entry
    with PartsReader(paths) as stream, zipfile.ZipFile(stream) as bundle:
        infos = bundle.infolist()
        require(len(infos) == len(entries) and {i.filename for i in infos} == set(expected_entries),
                'ZIP inventory mismatch')
        for info in infos:
            require(not info.is_dir() and not (info.flag_bits & 1)
                    and stat.S_IFMT(info.external_attr >> 16) in (0, stat.S_IFREG),
                    'Encrypted or special ZIP entry')
            entry = expected_entries[info.filename]
            require(info.file_size == entry['size'] and
                    ((info.external_attr >> 16) & 0o777) == entry['mode'], 'ZIP metadata mismatch')
            payload = hashlib.sha256()
            with bundle.open(info) as source:
                for block in iter(lambda: source.read(BLOCK), b''):
                    payload.update(block)  # zipfile also checks CRC on reaching EOF.
            require(payload.hexdigest() == entry['sha256'], 'SDK payload digest mismatch')
        inside = name+'/static-sdk-receipt.json'
        require(inside in expected_entries and expected_entries[inside]['size'] <= 2*1024**2,
                'Missing internal SDK receipt')
        require(bundle.read(inside) == receipt.read_bytes(), 'Inner and outer SDK receipts differ')
    return {'manifest': data, 'receipt': read_json(receipt),
            'files': [folder/n for n in sorted(expected_files)]}


def package_sdk(source: Path, output: Path, name: str, *, part_bytes=PART_BYTES,
                compression=zipfile.ZIP_DEFLATED) -> Path:
    """Stage, fully verify, then expose a complete bundle. Never trim SDK contents."""
    safe_name(name)
    integer(part_bytes, 1, PART_BYTES)
    require(compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED), 'Unsupported ZIP compression')
    source, output = source.resolve(), output.absolute()
    require(source.is_dir() and not output.is_relative_to(source), 'Invalid package directories')
    require(not output.exists() or (output.is_dir() and not output.is_symlink()
                                   and not any(output.iterdir())), 'Output must be absent or empty')
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt = source/'static-sdk-receipt.json'
    read_json(receipt)
    with tempfile.TemporaryDirectory(prefix='.cef-sdk-', dir=output.parent) as temp:
        staged = Path(temp)/'bundle'
        staged.mkdir()
        entries = []
        with SplitWriter(staged, name, part_bytes) as writer:
            with zipfile.ZipFile(writer, 'w', compression=compression, compresslevel=6,
                                 allowZip64=True) as bundle:
                for path in sorted(source.rglob('*')):
                    require(not path.is_symlink(), 'SDK symlink must not be followed')
                    if path.is_dir():
                        continue
                    require(path.is_file(), 'SDK special file rejected')
                    arcname = name+'/'+path.relative_to(source).as_posix()
                    validate_entry(arcname, name)
                    before = path.stat()
                    info = zipfile.ZipInfo.from_file(path, arcname)
                    info.compress_type = compression
                    info._compresslevel = 6
                    digest, size = hashlib.sha256(), 0
                    with path.open('rb') as src, bundle.open(info, 'w', force_zip64=True) as dest:
                        for block in iter(lambda: src.read(BLOCK), b''):
                            dest.write(block)
                            digest.update(block)
                            size += len(block)
                    after = path.stat()
                    require((before.st_size, before.st_mtime_ns, before.st_ino) ==
                            (after.st_size, after.st_mtime_ns, after.st_ino) and size == before.st_size,
                            'SDK changed while being archived')
                    entries.append({'path': arcname, 'size': size, 'sha256': digest.hexdigest(),
                                    'mode': (info.external_attr >> 16) & 0o777})
            archive = writer.finish()
            parts = writer.parts
        shutil.copyfile(receipt, staged/(name+'.json'))
        helper = staged/(name+'-assemble.py')
        shutil.copyfile(Path(__file__), helper)
        instructions = staged/(name+'-README.txt')
        instructions.write_text(
            'Download ALL files for this SDK into one directory. Python 3.11+ required.\n'
            'Verify every part, every ZIP member, and the complete archive SHA-256:\n'
            f'  python {helper.name} verify {name}.manifest.json\n'
            'For a multipart SDK, reconstruct the ordinary ZIP64 archive:\n'
            f'  python {helper.name} assemble {name}.manifest.json\n'
            'Then extract the resulting .zip with a ZIP64-capable extractor.\n'
            'A single-part SDK already contains this .zip. Parts are byte slices,\n'
            'not independent ZIP files. No downloads, code execution or extraction\n'
            'are performed by the verifier. Existing output files are not overwritten.\n'
            'SHA-256 detects corruption, not a maliciously replaced manifest; obtain\n'
            'the files from the trusted GitHub run/release. Runtime scope and system\n'
            'dependencies are recorded in static-sdk-receipt.json inside the SDK.\n', encoding='utf-8')
        manifest = staged/(name+'.manifest.json')
        manifest.write_bytes(json_bytes({'schema': 1, 'format': 'cef-sdk-zip-parts-v1',
            'name': name, 'archive': archive, 'part_bytes': part_bytes, 'parts': parts,
            'receipt': file_record(staged/(name+'.json')), 'helpers': [file_record(helper), file_record(instructions)],
            'entries': entries}))
        verify_bundle(manifest)
        if output.exists():
            output.rmdir()  # Only an empty directory; never erase earlier artifacts.
        staged.rename(output)
    return output/manifest.name


def assemble(manifest: Path, output: Path | None = None) -> Path:
    result = verify_bundle(manifest, strict=False)
    data = result['manifest']
    output = output or manifest.parent/data['archive']['name']
    paths = [manifest.parent/p['name'] for p in data['parts']]
    if len(paths) == 1 and output.absolute() == paths[0].absolute():
        return output  # Already a verified regular ZIP.
    require(not output.exists() and not output.is_symlink(), 'Refusing to overwrite assembled ZIP')
    with tempfile.TemporaryDirectory(prefix='.cef-assemble-', dir=output.parent) as temp:
        staged = Path(temp)/'archive.zip'
        digest, size = hashlib.sha256(), 0
        with staged.open('xb') as dest:
            for path in paths:
                with path.open('rb') as src:
                    for block in iter(lambda: src.read(BLOCK), b''):
                        dest.write(block)
                        digest.update(block)
                        size += len(block)
        require(size == data['archive']['size'] and digest.hexdigest() == data['archive']['sha256'],
                'Parts changed during reconstruction')
        # Atomic no-clobber installation on NTFS/ext4; fail on unsupported filesystems.
        os.link(staged, output)
    return output


def verify_receipt(receipt: dict, expected_commit: str, triplet: str) -> None:
    require(re.fullmatch('[0-9a-f]{40}', expected_commit) is not None, 'Invalid expected commit')
    require(triplet in TRIPLETS.values(), 'Unsupported native triplet')
    fixed = {'schema': 1, 'integration_commit': expected_commit, 'triplet': triplet,
             'cef_commit': CEF, 'chromium_commit': CHROMIUM, 'vcpkg_commit': VCPKG,
             'configuration': 'Release', 'engine_linkage': 'static'}
    require(all(receipt.get(k) == v for k, v in fixed.items()), 'SDK provenance mismatch')
    require(not receipt.get('fixture_only') and all(receipt.get(k) is True for k in
            ('capi_only', 'sdk_relocation_verified', 'application_relocation_verified')), 'Unverified SDK')
    require(all(type(receipt.get(k)) is bool for k in ('sandbox_verified', 'system_libraries_static')),
            'Missing SDK capability scope')
    proof = receipt.get('smoke', {})
    require(proof.get('cef') == VERSION and proof.get('engine') == 'static'
            and proof.get('interface') == 'capi', 'Wrong runtime engine')
    require(all(proof.get(k) is True for k in ('javascript', 'paint', 'browser_modules_clean',
                                            'renderer_modules_clean')), 'Missing runtime checks')
    pids = [proof.get(k) for k in ('browser_pid', 'renderer_pid')]
    require(all(type(p) is int and p > 0 for p in pids) and pids[0] != pids[1], 'No separate renderer')
    runs = receipt.get('smoke_runs', {})
    count = 3 if triplet == 'x64-windows-static' else 1
    require(runs.get('status') == 'success' and runs.get('engine_runtime_verified') is True
            and runs.get('no_retry_on_failure') is True and runs.get('required_runs') == count
            and runs.get('passed_runs') == count and len(runs.get('runs', [])) == count,
            'Incomplete required runtime repetitions')
    integer(runs['required_runs'], count, count)
    integer(runs['passed_runs'], count, count)
    require(len({r.get('cwd') for r in runs['runs']}) == count
            and all(isinstance(r.get('cwd'), str) and r['cwd'] for r in runs['runs']),
            'Runtime repetitions reused a profile')
    for index, run in enumerate(runs['runs'], 1):
        require(run.get('number') == index and run.get('status') == 'success'
                and run.get('engine_runtime_verified') is True, 'Failed runtime repetition')
    require(isinstance(receipt.get('executable_sha256'), str) and
            re.fullmatch('[0-9a-f]{64}', receipt['executable_sha256']) and
            runs.get('executable_sha256') == receipt['executable_sha256'], 'Executable proof mismatch')


def release_files(root: Path, expected_commit: str) -> list[Path]:
    require({p.name for p in root.iterdir()} == set(TRIPLETS), 'Expected exactly two native SDK artifacts')
    result = []
    for platform, triplet in TRIPLETS.items():
        folder = root/platform
        require(not folder.is_symlink() and folder.is_dir(), 'Invalid SDK artifact directory')
        name = f'cef-152.0.6-{triplet}-static-engine-capi'
        proof = verify_bundle(folder/(name+'.manifest.json'))
        verify_receipt(proof['receipt'], expected_commit, triplet)
        result += proof['files']
    require(len(result) < 1000 and len({p.name for p in result}) == len(result), 'Release asset collision')
    require(all(p.stat().st_size < ASSET_LIMIT for p in result), 'Oversized release asset')
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    for operation in ('verify', 'assemble'):
        p = sub.add_parser(operation)
        p.add_argument('manifest', type=Path)
        if operation == 'assemble': p.add_argument('--output', type=Path)
    p = sub.add_parser('release-list')
    p.add_argument('root', type=Path)
    p.add_argument('--commit', required=True)
    p.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.operation == 'assemble':
        print(assemble(args.manifest, args.output))
    elif args.operation == 'verify':
        result = verify_bundle(args.manifest, strict=False)
        print(json.dumps({'status': 'verified', 'archive': result['manifest']['archive']}))
    else:
        paths = release_files(args.root, args.commit)
        with args.output.open('xb') as dest:
            dest.write(json_bytes({'commit': args.commit, 'files': [str(p.resolve()) for p in paths]}))


if __name__ == '__main__':
    main()
