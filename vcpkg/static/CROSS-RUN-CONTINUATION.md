# Continue a build with a new run, not a retry

## Incident: run 34746930913, attempt 2

Both jobs were green because their timed Ninja stops and checkpoint uploads
succeeded. They did not finish a CEF SDK. The downloaded diagnostic ZIPs show:

| Platform | Attempt 1 | Attempt 2 |
| --- | --- | --- |
| Windows | restored run 34709030909; 3283/32560 actions | restored the same old run; 3251/32560 actions |
| Linux | no full checkpoint; 24069/57915 actions | another cache miss; 23197/57915 actions |

Every distinct action description from attempt 2 was already present in attempt 1.
This is a text-log comparison, not a claim of verified object hashes or percentage
of CPU time. After the retry, the run-artifacts API lists only attempt-2 artifacts;
the exact attempt-1 Linux artifact-name query returns an empty list. The disappearance
is consistent with the upstream report below; this audit does not prove who deleted
or hid an individual artifact inside GitHub.

The old selector silently accepted an older Windows checkpoint or no Linux
checkpoint. Merely adding attempt numbers to artifact names did not protect us.
The recommendation to use **Re-run all jobs** was wrong for this continuation path.

## New operation

The checked-in workflow restores through `resume_checkpoint.py` on both hosted
platforms. The serializers, compilation drivers and complete recipe input sets
are unchanged, so the existing attempt-2 checkpoints remain reusable if the
runner image and absolute work path still match.

Each iteration is a **new workflow run**, with run_attempt=1. `resume` first
selects one producer (explicit run ID, or the latest relevant branch run). It
requires that producer to be completed and obtains the exact platform artifact
from its latest attempt. It does not fall through to older runs, earlier attempts,
or a fresh build when anything is missing. API errors are errors, not cache misses.
Source-run status/attempt/SHA are checked again around download. The unchanged
unpacker verifies the archive identity, part hashes and filesystem safety.

### Starting another iteration while main remains original upstream

The repository currently has default branch `main`; this workflow lives in
`static-engine`. GitHub documents that workflow_dispatch requires the workflow
on the default branch. Do not change the default branch or add CI to original
main merely to obtain a Run workflow button.

Instead edit **`vcpkg/static/iteration-request.json` on `static-engine`**, increment
`sequence`, leave `mode` as `resume` and commit/push normally. This ordinary push
starts a fresh run; the marker does not participate in either build recipe hash.
Leave `checkpoint_run` empty to require the latest relevant producer, or set it to
one explicitly reviewed completed producer ID. Selecting an older run explicitly
can roll back progress; the receipt records exactly what was selected.

For the first iteration after this fix, the available producer is **34746930913,
attempt 2**, whose artifacts are Linux **10321400869** and Windows **10321951438**.
Do not retry or delete that producer during the download/restore handoff.

When workflow_dispatch is available, use a **new** run with branch `static-engine`,
`checkpoint_mode=resume` and optional `checkpoint_run=<producer>`. This is not
Re-run all jobs. With empty `checkpoint_run`, an active newest producer or a
completed newest producer lacking the expected artifact causes a clear failure.
Review the diagnostics and explicitly choose the correct saved producer rather
than silently compiling from an older one.

An intentional cold build requires `mode=fresh` in a committed request or an
explicit manual dispatch. Clear `checkpoint_run` and switch mode back to `resume`
for the next iteration. A published new release starts fresh as before. This
change does not add automatic follow-up runs, change billing/retention, disable
identity checks or write back to Git from CI.

## Progress and publication gates

Before each slice, CI records the existing output set in `.ninja_log`. After the
checkpoint is safely uploaded it writes `static-diagnostics/resume/progress.json`:
unique previous/new output counts, newly completed compiled outputs, and a small
sample of names. A non-completed engine with **zero additional completed outputs**
fails instead of being reported as progress. The saved checkpoint is retained.
This can indicate stalled work or an individual link action longer than the slice
budget; another identical iteration should not be treated as a solution.

`static-diagnostics/resume/restore.json` records producer run, attempt, SHA,
artifact ID/name/digest and exact identity. Existing platform diagnostic paths
also receive that receipt. `engine_runtime_verified` remains false. Only the
existing native CEF execution and relocated vcpkg SDK consumer checks may enable
SDK publication. No compiler warning, linker error or missing dependency is suppressed.

## Tests and sources

`test_resume_checkpoint.py` covers missing/expired latest-attempt artifacts,
rejection of silent fallback/cold starts, foreign producers, pagination, producer
retry races, preservation of existing workspaces, real POSIX archive restoration,
and a real small C/Ninja no-op/progress test on Linux. Passing fixtures are not a
full CEF build or proof of a multi-gigabyte transfer on a new runner.

Primary references:
- CI under audit: https://github.com/dobord/cef/actions/runs/34746930913
- Upstream reproduction of retry artifact loss: https://github.com/actions/upload-artifact/issues/585
- GitHub artifact API: https://docs.github.com/en/rest/actions/artifacts
- Manual workflow/default-branch requirement: https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow
