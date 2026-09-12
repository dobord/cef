# Windows build iterations

Run `34565392341`, job `103156443144`, stopped after 18000 seconds at Ninja action
20968/57819. The diagnostic status is `timed_out`, not a compiler failure. No
Windows object cache existed; only logs survived the hosted runner shutdown.

The hosted Windows leg now performs a bounded Ninja iteration (default 10800
seconds, maximum 10800), then saves a **build checkpoint, not an SDK**. The
checkpoint contains the dedicated CEF source/depot_tools/Ninja workspace,
including generated files, objects, `.ninja_log`, `.ninja_deps` and exact integer
file mtimes. A matching future iteration restores it before source preparation.
Only compression output parts and their manifest are uploaded: compression is
streamed, so an additional uncompressed TAR is not required.

Ninja receives CTRL_BREAK in a new process group and must finish its cleanup.
A forced termination, compiler error or unverifiable interruption does **not**
create a checkpoint. In particular a timeout is not reported as engine success.

## Continuation

Re-run the workflow at the same source revision. Re-run-all and a later attempt
of the same run can restore a previous attempt's artifact; names include run and
attempt IDs to avoid immutable-artifact name conflicts. The most recent matching
checkpoint amongst the last 100 repository artifacts is considered. Misses
(including expired artifacts, changed recipes or changed runner images) cause a
fresh build. A corrupt or incompatible selected checkpoint fails closed.

Identity includes the absolute workspace, repository/ref, Windows runner image
version and port/driver recipe bytes. Restores never overwrite a nonempty
workspace. Pull-request workflows cannot supply production checkpoints; producer
repository, branch, event and workflow path are checked. No tokens are included;
credential-bearing Git configs, escaping links and unsupported junctions are rejected.
Relative symlinks inside the workspace are recorded separately and recreated
only after regular files have been extracted; they are never followed while
archiving. Empty directories are retained. Schema 2 rejects older checkpoints.
The Telemetry benchmark-only credentials.json path is omitted without reading
its contents; arbitrary credential files remain errors.

Snapshots are large and consume artifact storage. Existing repository retention,
quota and billing settings are **not** changed. Default retention is three days;
archive size and part hashes appear in `checkpoint.json`. All parts are required.
Do not treat a checkpoint as a redistributable vcpkg package or deploy it.

## Completion and publication

If Ninja completes, the unchanged vcpkg build/export and native plus relocated
external-consumer runtime checks run. Only that successful `package` step may
publish `cef-static-x64-windows-static-sdk-<attempt>`. A successful checkpoint iteration alone
cannot publish an SDK or attach a release asset. Release publication additionally
requires both platform SDKs from the same run and the original proof/hash gates.
The latest valid artifact for each platform from any attempt of that same run
is selected; the integration SHA must still match. Diagnostics and test artifacts
also carry the attempt number, so reruns do not overwrite immutable artifacts.
A native link, runtime or exporter error remains a build failure.

The Windows change leaves the Linux build path, its ccache and the Dawn/Ozone
fix intact. Self-hosted Windows continues using its existing persistent
`CEF_STATIC_WORK`; hosted snapshots are not automatically enabled there.

`CEF_WINDOWS_SLICE_SECONDS` is a repository variable (1..10800). Changing it does
not change the compile recipe. Keep sufficient job time for restore, compression,
upload, and final validation. Raising the old timeout beyond the hosted job
limit does not fix the loss of progress.

## Verification scope

`windows-checkpoint-regression.yml` compiles a small native C library, transfers
its workspace to a **different hosted runner**, finishes the link without
recompiling the existing object, and then changes a header to prove Ninja's
restored dependency tracking still rebuilds correctly. It separately tests real
Ninja interruption and removal of unfinished output. Windows regression tests
invoke the Visual Studio native `ninja.exe` directly instead of a possible
Chocolatey launcher shim; production invokes the pinned Chromium Ninja directly. These tests are **not CEF
engine builds**; the real Chromium-size checkpoint transfer, full source build,
link/run and vcpkg packaging must still finish before claiming a static CEF SDK.

## Primary references

- Failure: https://github.com/dobord/cef/actions/runs/34565392341/job/103156443144
- Hosted execution limits: https://docs.github.com/en/actions/reference/limits
- Artifact handoff: https://github.com/actions/upload-artifact
- Ninja cleanup: https://github.com/ninja-build/ninja/blob/master/src/build.cc
- Ninja Windows interrupts: https://github.com/ninja-build/ninja/blob/master/src/subprocess-win32.cc

## September 12 repair and corrected diagnosis

The exact job log for run 34593997225/job 103329207292 shows the snapshot
failed on the relative depot_tools/cros_sdk symlink. The earlier summary that
attributed this particular failure to credentials.json was incorrect. The
schema-2 round-trip regression now includes the cros_sdk -> cros layout and a
symlinked C header used by the native compiler. Escaping/cyclic links and
junctions remain rejected. A preflight scan runs before the Windows Ninja slice,
so unsupported workspace entries are detected before hours of compilation.
Full Chromium-size checkpoint transfer and static SDK success still require the
main CI run; small native checkpoint regressions do not certify an engine build.
