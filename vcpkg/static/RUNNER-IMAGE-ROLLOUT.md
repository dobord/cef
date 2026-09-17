# Windows checkpoint image rollout

## Observed failure

Run `35175681883`, commit `af332e11f498937f570d03c5da486566a3ac34a1`,
failed in `checkpoint-preflight / consume (windows-2022)` before the engine
jobs. The fixture producer used `20260907.297.1`; its fresh consumer received
`20260913.307.1`. The exact-image archive check correctly refused that input.
The read-only SDK cleanup and transport preflights both passed.

The ordinary `windows-2022` label is not an immutable image version.
The official update at
https://github.com/actions/runner-images/releases/tag/win22%2F20260913.307
lists OS and other software updates. Unchanged version labels alone are not
sufficient evidence of unchanged compiler or SDK files.

## Measured baseline and conditional legacy bridge

`runner_fingerprint.py` inventories host compiler/SDK/tool files without
changing them. Isolated inventory run `35177873930` on commit
`dc1119a6a2c9ed43a70ae3b2b82017bea625669c` produced identical old-image
inventories in samples 1, 2 and 4: 20 components, 45,404 file entries,
22,440,525,841 bytes. The preserved full baseline is
`runner-image-baseline.json`. Inventory hashing excludes Python bytecode
cache directories and site-packages; this is a host build-input inventory,
not an assertion that the entire OS is byte-identical or a CEF runtime proof.

Baseline inventory SHA-256:
`e7b4113d5e19720b6a2adf0cf01850691d9e7403e1ffeea381d9ae69505f6dd7`.
The inventory covers all installed MSVC toolsets, auxiliary selection/build
files, MSBuild, native Ninja, Windows SDK include/lib/bin trees, .NET Framework
SDK, host CMake, Git entry points/core, and the host Python runtime/stdlib.
Source-pinned Clang/GN/Ninja remain in the existing checked source workspace.

The production bridge is deliberately narrower than a general image alias:

- Only producer run `35132153154`, attempt 1, SHA
  `c4e8c1e24f0483c42c5439bd95df293d05405a83`, and its already reviewed
  recipe transition are eligible.
- Only old image `20260907.297.1` or prospective consumer `20260913.307.1`
  may be considered. An unknown image is rejected before downloading 15 GB.
- Every selected Windows host must recompute the full inventory and match
  the recorded baseline. The collector's normalized source SHA-256 is also
  pinned. An unreadable file, modified tool, missing component or different
  path fails closed. Approval of the prospective image is conditional on
  this check; there is no unconditional claim that its build inputs match.
- Only the restore INPUT identity refers to the old image. `ImageVersion` is
  never overwritten. New checkpoints retain the real consumer image, and
  `static-diagnostics/resume/runner-image-transition.json` records both.

The archive unpacker, recipe hashes, source objects, producer provenance,
checksums, symlink/timestamp handling, no-cold-fallback rule and all external
SDK runtime/publication gates remain unchanged. Sequence 17 still selects
both complete engine checkpoints from `35132153154`, not the failed
preflight run. No checkpoint or release is deleted or overwritten.

## Native fixture contract

The small Windows archive fixture now keys compatibility to its measured
host toolchain content, work path, OS and fixture recipe instead of the
whole image release label. Producer and consumer independently compute the
inventory. Actual image versions remain separate provenance in the fixture
and uploaded host reports. Linux retains its existing image-bound policy.
The native object must still be reused unchanged on a fresh runner, and a
changed header must still cause a recompilation and changed runtime output.
Image mismatch is never swallowed and a fresh build is not reported as reuse.

Producer and consumer host reports are uploaded even on failure. Fingerprint
work has a bounded 30-minute fixture job budget; it is not a skip or a retry.
The audit inventory is intentionally conservative and adds disk-read cost.

## Validation and limits

`test_runner_image_migration.py` reproduces the original failure with a real
archive and checks strict identity, producer and changed-content rejection.
Its native Windows and Ubuntu steps passed in run `35178511255`. Local
Linux: 12 tests passed, one Windows-only skip. A local CMake/Ninja roundtrip
also preserved the object and recompiled after a header edit; that local
roundtrip is not fresh-runner evidence.

The two-runner fixture and the new full SDK workflow are required gates.
A content inventory, a green unit suite, or a small native fixture alone does
not establish a successful CEF build, runtime validation or SDK upload.
