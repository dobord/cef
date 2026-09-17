# Read-only Windows SDK session cleanup

## Observed failure, not an archive-size failure

Run `35132153154`, attempt 1, commit
`c4e8c1e24f0483c42c5439bd95df293d05405a83` passed the Windows external
consumer link (exit 0), three fresh CEF runtime checks and SDK transport
verification. `sdk-packaging-status.json` records a **1,034,098,229-byte** ZIP,
6,802 members and `transport_verified: true`. The manifest has 131 read-only
members, including the Apache `manual/LICENSE` (mode 0444).

The source job failed afterwards in `shutil.rmtree(session)`, on that copied
LICENSE, with `PermissionError: [WinError 5] Access is denied`. This made the
package step fail and correctly prevented the success-gated SDK upload.
The success marker had been printed too early, before final cleanup.

The earlier diagnosis that run `35112919208` failed at the 2-GiB ZIP guard
was incorrect: its Windows job log also ends in read-only session cleanup.
Multipart transport remains independently tested, but was not the fix for
this failure. No SDK files or licenses should be removed to work around it.

## Narrow repair

`session_cleanup.remove_session` runs only after successful runtime and
packaging, and after copying diagnostic logs. It removes only the direct
`cef-sdk-test-*` directory created by this invocation. The final CI success
marker is now printed after cleanup, not before it. `sdk-cleanup-status.json`
records cleanup independently of runtime and packaging receipts.

The callback retries exactly once, only on native Windows unlink WinError 5
for a confirmed read-only regular file. It clears that temporary file's
read-only attribute and retries the original operation. Writable-file access
denials, sharing violations, directory errors and I/O failures still raise.
On retry failure it attempts to restore the original metadata and retains
the failure. There is no blanket ignore-errors or best-effort success.

Root symlinks/junctions and redirected chmod paths are rejected. Enumeration
never follows reparse points. Before deleting anything on Windows, read-only
hard links must be exclusively inside the invocation: NTFS attributes belong
to the shared file, not one name. `os.stat(..., follow_symlinks=False)` is used
because `DirEntry.stat` does not provide usable Windows inode/link counts.
Writable hard links require no attribute change. This is private, trusted CI
session cleanup after subprocess exit, not a race-proof hostile-filesystem API.

## Evidence and continuation

Native fixture run `35175234576` passed on Windows Server 2022/Python 3.12.10
and Ubuntu/Python 3.12.3. Windows reproduced the legacy WinError 5, then removed
both the read-only license and a real Git object. Source contents, mode and
mtime, the existing archive and a sibling invocation stayed unchanged.
Native tests also cover owned/external hard links, junctions and a real
handle opened without FILE_SHARE_DELETE. All 15 cleanup tests run on Windows;
Linux runs 12 and skips the three Windows-only cases. Existing port contracts
passed on both hosts. The unchanged transport suite passed all 16 local tests,
including an actual ZIP larger than 2 GiB.

The cleanup suite is now required by the existing reusable SDK-package
preflight before either source job. The standalone cleanup workflow produces
only small fixture evidence and no CEF SDK.

Only `ci.py` changes the existing checkpoint recipe inputs. Exact native
baseline/candidate recipes were measured by the real checkpoint adapters,
including Windows checkout line endings. Sequence 16 resumes both platforms
from run `35132153154`, attempt 1. The reconstructed baseline artifact keys
match its Windows and Linux checkpoint names exactly. Recipe migration does
not bypass repository, ref, image, workspace, producer SHA/attempt or archive
integrity checks. No source upgrade or change to Ninja inputs is needed.

Windows checkpoint: `10464543521`, 15,136,283,698 bytes, expires
2026-09-19 19:18:05 UTC. Linux checkpoint: `10463287852`, 16,792,541,403 bytes,
expires 2026-09-19 18:51:29 UTC. They remain checkpoints, not runtime receipts.

Full CEF SDK validation with the repair must be established by the new main
workflow; fixture success alone does not certify an SDK or publish a release.
The existing C API/Release/static-engine scope is unchanged. Sandbox, GPU and
fully static operating-system dependencies are not certified by this repair.

## Primary platform references

- Python 3.12 shutil.rmtree/onexc and the read-only cleanup example:
  https://docs.python.org/3.12/library/shutil.html#rmtree-example
- Windows DeleteFileW read-only and sharing semantics:
  https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-deletefilew
- NTFS hard-link attribute propagation:
  https://learn.microsoft.com/en-us/windows/win32/fileio/hard-links-and-junctions
