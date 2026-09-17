# Promote verified static CEF SDKs without rebuilding

## Why `publish` was skipped in build run 35179388662

The build was triggered by **push**, completed successfully on both platforms,
and uploaded both SDKs. The existing build workflow's `publish` job explicitly
requires `github.event_name == 'release'` and a complete SDK pair. This is a
release-event policy, not a compilation failure. A green branch build does not
create a GitHub Release. The old hybrid validation draft is unrelated and must
not be published or overwritten.

The new **Publish verified CEF SDKs (no engine rebuild)** workflow supplies the
missing explicit promotion path. It does not turn every engine-build push into
a stable release, and does not require adding workflows to the upstream `main`.
The historical build job remains `skipped`; promotion is a separate workflow.

## Request publication

Edit `vcpkg/release/request.json` on `static-engine` and push it. Set the exact
successful producer run, attempt, source commit and branch. The tag must be:

```
cef-152.0.6-static-capi-r<RUN_ID>-a<ATTEMPT>
```

`prerelease` must be true. Increment `sequence` for a new request. This file is
outside `vcpkg/static/**`, so its push does **not** restart Chromium compilation.
A manual dispatch of this workflow also uses the committed request. PR events
run policy tests only; they cannot publish. The checked-in initial request
promotes run **35179388662 / attempt 1 / bd0f30183448eca4c3be183af644baeb94116bce**.

## Verification and publication order

1. Run publication-policy regression tests with read-only repository permissions.
2. Require the explicitly selected build to be complete and successful, from the
   owning repository's trusted branch and exact build workflow. Check the native
   Windows/Linux consumer and SDK-upload steps for the selected attempt.
3. Select exactly one unexpired SDK artifact for each platform. Validate origin,
   attempt-specific name, size and SHA-256. Download by artifact ID, check the
   entire outer ZIP digest, and extract only bounded, flat transport files.
4. Use the existing `sdk_package.release_files` validator to check both complete
   bundles, part ordering, every payload member's SHA-256 and CRC, inner/outer
   receipt agreement, source commit and required runtime repetitions. Downloaded
   assembly helpers and native binaries are **not executed** by the publisher.
5. Recheck producer stability. Create a unique tag pointing to the **tested build
   commit**, not the newer publication implementation, then create a draft.
6. Upload only the verified files without overwrite. Require server-reported
   size/SHA-256 for every uploaded release asset. Recheck the producer and tag.
   Only then publish the draft as a prerelease, explicitly not `latest`.

The release `contents: write` permission is confined to the trusted publication
job. No secret is stored in files. The publisher never downloads checkpoint
archives or starts a build. GitHub-token release writes do not recursively
start the old release build workflow.

## Safe retry and diagnostics

An upload failure leaves a **draft**, never a partially filled published release.
A retry revalidates everything and uploads missing files only; matching files are
retained. Foreign drafts, wrong tags, unexpected assets or differing checksums
cause an error. No existing tag, asset or release is deleted, retargeted or
clobbered. A fully published, identical release is verified and treated as a
no-op. Do not rerun the producer build merely to retry release publication.

`cef-sdk-publication-proof-<RUN>-<ATTEMPT>` contains source-run metadata, selected
artifact identities, the verified file list, publication result or failure.
The workflow summary links directly to the completed prerelease.

## Honest scope

The SDK has a **static CEF engine, C API, Release, x64**. Windows uses static CRT,
but requires system DLLs. Linux still uses dynamic system dependencies. Sandbox
and GPU functionality are not certified by the smoke page. The initial release
is therefore a prerelease, not an assertion of fully static system libraries or
production security readiness. Source-test settings disabling sandbox must not
be copied to applications handling untrusted content.

Publication verifies the producer's recorded runtime evidence; it does not claim
a new native CEF execution on the publishing Ubuntu runner.
