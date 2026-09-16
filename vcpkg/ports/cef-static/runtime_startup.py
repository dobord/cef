#!/usr/bin/env python3
"""Reviewed native startup repairs, applied before GN on fresh/resumed sources.

No allocator checks, API validation or native SDK tests are disabled. Every edit
accepts only a pinned complete file or its exact idempotent transformation.
"""
from __future__ import annotations
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import tempfile

ASSET_FILE = 'cef/libcef/common/resource_util.cc'
ASSET_BLOB = '22e667262a5d1a335f799437fde3d0a9787ed626'
ASSET_OLD = '''void OverrideAssetPath() {
  Dl_info dl_info;
  if (dladdr(reinterpret_cast<const void*>(&OverrideAssetPath), &dl_info)) {
    base::FilePath path = base::FilePath(dl_info.dli_fname).DirName();
    base::PathService::Override(base::DIR_ASSETS, path);
  }
}'''
ASSET_NEW = '''void OverrideAssetPath() {
#if defined(CEF_STATIC)
  // Chromium launches helpers through /proc/self/exe. For a statically linked
  // engine dladdr() then names /proc/self/exe, not the real asset directory.
  // DIR_EXE resolves the executable itself and also survives a changed cwd.
  base::FilePath path;
  CHECK(base::PathService::Get(base::DIR_EXE, &path));
  CHECK(base::PathService::Override(base::DIR_ASSETS, path));
#else
  Dl_info dl_info;
  if (dladdr(reinterpret_cast<const void*>(&OverrideAssetPath), &dl_info)) {
    base::FilePath path = base::FilePath(dl_info.dli_fname).DirName();
    base::PathService::Override(base::DIR_ASSETS, path);
  }
#endif
}'''
ASSET_INCLUDE_OLD = '#include "base/base_paths.h"\n'
ASSET_INCLUDE_NEW = ASSET_INCLUDE_OLD + '#include "base/check.h"\n'


RAND_FILE = 'base/rand_util.cc'
# Pinned Chromium after CEF's base_rand_util_lazy_seed.patch (not stock Chromium).
RAND_BLOB = '5580637cc84be708c8fe65b535fae219555515c3'
RAND_OLD = '''    do {
      a_ = base::RandUint64();
      b_ = base::RandUint64();
    } while (a_ == 0 && b_ == 0);'''
RAND_NEW = '''    // The first sample can itself run within an allocator hook. Lazy
    // construction alone is insufficient: RandUint64 may call RAND_bytes,
    // whose per-thread state allocates. Use the existing non-allocating OS
    // entropy generator for the seed; keep XorShift and crypto APIs unchanged.
    base::NonAllocatingRandomBitGenerator seed;
    do {
      a_ = seed();
      b_ = seed();
    } while (a_ == 0 && b_ == 0);'''


def blob(text: str) -> str:
    data = text.encode('utf-8')
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


def transform_asset(text: str) -> tuple[str, str]:
    edits = [(ASSET_INCLUDE_OLD, ASSET_INCLUDE_NEW), (ASSET_OLD, ASSET_NEW)]
    original = text
    for old, new in reversed(edits):
        if original.count(new) == 1:
            original = original.replace(new, old, 1)
    if blob(original) != ASSET_BLOB:
        raise RuntimeError('Unreviewed CEF asset source; refusing startup patch')
    result = original
    for old, new in edits:
        if result.count(old) != 1:
            raise RuntimeError('Ambiguous CEF asset patch context')
        result = result.replace(old, new, 1)
    # Reject mixed/partial states instead of blessing an interrupted patch.
    if text not in (original, result):
        raise RuntimeError('Partially patched CEF asset source')
    return result, ASSET_BLOB


def transform_rand(text: str) -> tuple[str, str]:
    already = text.count(RAND_NEW) == 1 and RAND_OLD not in text
    original = text.replace(RAND_NEW, RAND_OLD, 1) if already else text
    if blob(original) != RAND_BLOB or original.count(RAND_OLD) != 1:
        raise RuntimeError('Unreviewed allocator-safe random source; refusing startup patch')
    return original.replace(RAND_OLD, RAND_NEW, 1), RAND_BLOB


def apply(source: Path, receipt: Path) -> None:
    source = source.resolve()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.unlink(missing_ok=True)
    planned = []
    for name, transform in [(ASSET_FILE, transform_asset), (RAND_FILE, transform_rand)]:
        path = source/name
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise RuntimeError('Redirected runtime source: '+name)
        text = path.read_text(encoding='utf-8')
        changed, original_blob = transform(text)
        planned.append((name, path, text, changed, original_blob))
    reports = []
    # Validate all complete input files before writing any individual edit.
    for name, path, text, changed, original_blob in planned:
        if text != changed:
            fd, tmp = tempfile.mkstemp(prefix='.cef-runtime-', dir=path.parent)
            temporary = Path(tmp)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                    stream.write(changed)
                temporary.chmod(path.stat().st_mode & 0o777)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        reports.append({'file': name, 'reviewed_input_blob': original_blob,
            'status': 'already-applied' if text == changed else 'applied',
            'before_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'after_sha256': hashlib.sha256(changed.encode()).hexdigest(),
            'diff': ''.join(difflib.unified_diff(text.splitlines(True), changed.splitlines(True),
                                               fromfile='a/'+name, tofile='b/'+name))})
    receipt.write_text(json.dumps({'schema': 1, 'repair': 'native-startup-v1',
        'edits': reports, 'engine_runtime_verified': False}, indent=2)+'\n', encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    # The launcher inserts only this script's directory for sibling imports.
    import instance_tracer_patch
    instance_tracer_patch.apply(args.source,
                               args.receipt.with_name('instance-tracer-patch.json'))
    apply(args.source, args.receipt)


if __name__ == '__main__':
    main()
