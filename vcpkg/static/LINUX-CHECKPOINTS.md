# Ubuntu build iterations

The Linux leg of run `34681970100` stopped at the 18000-second compilation
budget after 28507/57915 Ninja actions. Its ccache was saved, but no complete
Linux workspace survived. A ccache hit is not a substitute for generated files,
Rust objects, source timestamps and Ninja dependency state.

## Operation

Hosted Ubuntu now restores a matching `cef-linux-checkpoint-<identity>-<run>-<attempt>`
artifact **before** source preparation. It validates the pinned source/recipe,
checks the static graph and the archive policy, then runs native Ninja for up to
10800 seconds. `CEF_LINUX_SLICE_SECONDS` accepts 1..10800; changing this scheduling
budget does not change the compile recipe. The job retains its 350-minute limit,
leaving room for preparation, restore, checkpoint compression/upload, and final
validation. A hard runner timeout cannot guarantee that a checkpoint is saved.

Ninja receives SIGINT and must exit cleanly, removing incomplete edge outputs.
A compiler error, forced kill or failed archive is still a failure and cannot
publish a valid checkpoint or SDK. A clean scheduled interruption creates a
checkpoint, **not an engine success**. Re-run all jobs at the same commit for the
next iteration on both platforms; no unbounded automatic rerun chain is created.
Self-hosted jobs keep the existing persistent-workspace path and full build.

The full dedicated source/depot_tools/Ninja workspace is saved in streaming gzip
parts of at most 1 GiB. File modes, integer-nanosecond timestamps, empty directories
and internal relative symbolic links are retained. POSIX restore respects case:
`Case/file` and `case/file` are distinct. Hardlinked files are stored as independent
regular bytes, so restoration never needs an unsafe TAR hardlink entry. The Linux
sysroot's relative library links and executable host tools are retained.

All parts must pass size/SHA-256 checks before extraction. Extraction stages
regular data first and links last; escape paths, cycles, unsupported filesystem
entries and credential-bearing files/configs are rejected. The existing narrow
Telemetry benchmark-credentials omission is retained, never read or uploaded.
Artifact discovery only accepts the matching repository/branch, producer workflow,
source SHA and identity, including earlier attempts of the same run. Up to five
pages of repository artifacts are searched. A mismatch starts fresh; a corrupt
selected checkpoint fails rather than silently merging workspaces.

The previous `cef-windows-checkpoint-*` is not invalidated by this change. The
Windows archive code, driver, port files and all inputs hashed by Windows
`ci_identity()` remain byte-for-byte unchanged. New Linux tests deliberately live
under `vcpkg/static/tests`, outside the port tree hashed by that Windows identity.
Linux and Windows artifact namespaces/identities cannot be interchanged.

## SDK publication

Only when native Ninja finishes does the Linux package step invoke the existing
`ci.py` vcpkg install/export, browser/renderer execution and relocated external
consumer checks. Only a successful package step may upload
`cef-static-x64-linux-sdk-<attempt>`. Checkpoint-only iterations skip SDK upload.
The existing two-platform SDK readiness and release proof/hash gates are retained.
This CI change does not prove that the final engine links/runs, and does not
change the port's existing C-API/Release profile or system-library linkage policy.

Checkpoints use three-day retention, SDK artifacts seven days. All checkpoint
parts are needed for restoration. Repository quotas/billing settings are not
changed; full-source checkpoints consume substantial artifact storage. A cache
miss, expired checkpoint or changed runner image can still force a fresh build.

## Validation

The main workflow calls a separate Linux producer/consumer preflight before any
engine job. It compiles a real C library, transfers its checkpoint to another
Ubuntu runner, finishes the link while verifying object reuse, changes a header
to check dependency invalidation, and checks executable modes, hardlinks,
case-sensitive names and symlink clocks. It uses the same Linux archive adapter
as production. This small fixture is not a CEF engine build.

Primary references:
- Timeout and existing Windows checkpoint: https://github.com/dobord/cef/actions/runs/34681970100
- Hosted-job limits: https://docs.github.com/en/actions/reference/limits
- Artifact semantics: https://github.com/actions/upload-artifact
- Pinned sysroot relative-link cleanup: https://github.com/chromium/chromium/blob/79460ebecaa5625e57a5fb679a735659e73dc687/build/linux/sysroot_scripts/sysroot_creator.py
