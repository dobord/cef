#!/usr/bin/env python3
"""Keep BRP tracer bookkeeping out of allocator hooks without disabling tracing.

Pinned source only. Applied before GN on both fresh and restored workspaces.
The public vector is allocated after unlocking; internal containers use PA's
existing no-hooks allocator. This is not a runtime success certificate.
"""
from __future__ import annotations
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import tempfile

FILE = 'base/allocator/partition_allocator/src/partition_alloc/pointers/instance_tracer.cc'
BLOB = '46bad4bd7d3438981ae04ccaaf1760d4c0b02aca'
EDITS = [
    ('#include <atomic>\n', '#include <atomic>\n#include <functional>\n'),
    ('#include <mutex>\n', '#include <mutex>\n#include <utility>\n'),
    ('#include "partition_alloc/partition_alloc_base/check.h"',
     '#include "partition_alloc/internal_allocator.h"\n'
     '#include "partition_alloc/partition_alloc_base/check.h"'),
    ('''auto& GetStorage() {
  static partition_alloc::internal::base::NoDestructor<std::map<uint64_t, Info>>
      storage;
  return *storage;
}''', '''// Ordinary allocation/free can re-enter raw_ptr tracing via allocator shims
// (including GWP-ASan). Bookkeeping under the non-recursive storage mutex must
// use the internal partition, whose allocator bypasses those hooks.
using Storage = std::map<
    uint64_t, Info, std::less<uint64_t>,
    partition_alloc::internal::InternalAllocator<std::pair<const uint64_t, Info>>>;
using StackTrace = std::array<const void*, 32>;
using Snapshot = std::vector<
    StackTrace, partition_alloc::internal::InternalAllocator<StackTrace>>;

auto& GetStorage() {
  static partition_alloc::internal::base::NoDestructor<Storage> storage;
  return *storage;
}'''),
    ('''  const std::lock_guard guard(GetStorageMutex());
  GetStorage().try_emplace(owner_id, slot_count, may_dangle);''',
     '''  // Stack unwinding may initialize platform state. Do it before locking.
  Info info(slot_count, may_dangle);
  const std::lock_guard guard(GetStorageMutex());
  GetStorage().try_emplace(owner_id, std::move(info));'''),
    ('''  std::vector<std::array<const void*, 32>> result;
  const std::lock_guard guard(GetStorageMutex());
  for (const auto& [id, info] : GetStorage()) {
    if (info.slot_count == allocation && !info.may_dangle) {
      result.push_back(info.stack_trace);
    }
  }
  return result;''', '''  Snapshot snapshot;
  {
    const std::lock_guard guard(GetStorageMutex());
    for (const auto& [id, info] : GetStorage()) {
      if (info.slot_count == allocation && !info.may_dangle) {
        snapshot.push_back(info.stack_trace);
      }
    }
  }
  // Preserve the public return type. Its ordinary allocator may invoke raw_ptr
  // callbacks, so construct the result only after releasing the storage mutex.
  return {snapshot.begin(), snapshot.end()};'''),
]


def blob(text: str) -> str:
    data = text.encode('utf-8')
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


def transform(text: str) -> str:
    original = text
    for old, new in reversed(EDITS):
        if original.count(new) == 1:
            original = original.replace(new, old, 1)
    if blob(original) != BLOB:
        raise RuntimeError('Unreviewed complete InstanceTracer source')
    result = original
    for old, new in EDITS:
        if result.count(old) != 1:
            raise RuntimeError('Ambiguous InstanceTracer patch context')
        result = result.replace(old, new, 1)
    if text not in (original, result):
        raise RuntimeError('Partially patched InstanceTracer source')
    return result


def apply(source: Path, receipt: Path) -> None:
    source = source.resolve()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.unlink(missing_ok=True)
    path = source/FILE
    if path.is_symlink() or not path.resolve().is_relative_to(source):
        raise RuntimeError('Redirected InstanceTracer source')
    text = path.read_text(encoding='utf-8')
    result = transform(text)
    if text != result:
        fd, name = tempfile.mkstemp(prefix='.cef-tracer-', dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(result)
            temporary.chmod(path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    receipt.write_text(json.dumps({'schema': 1, 'repair': 'instance-tracer-no-hooks-v1',
        'file': FILE, 'reviewed_input_blob': BLOB, 'output_blob': blob(result),
        'status': 'already-applied' if text == result else 'applied',
        'engine_runtime_verified': False,
        'diff': ''.join(difflib.unified_diff(text.splitlines(True), result.splitlines(True),
                                            fromfile='a/'+FILE, tofile='b/'+FILE))},
        indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    apply(args.source, args.receipt)
