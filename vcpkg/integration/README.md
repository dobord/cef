# External vcpkg/builder integration

This directory adds reusable acquisition and iteration entry points without
changing the existing source recipe, checkpoint formats or released SDKs.

`sdk_import.py` verifies a hash-locked release manifest, all transport parts,
every ZIP member and the canonical upstream runtime receipt. It installs only
CEF-owned headers, regular archives, CMake files, resources and licenses.
The old SDK's vcpkg status database, triplets and toolchain are not imported.
Its original receipt is preserved and a separate acquisition record explicitly
requires a NEW consumer qualification. Downloaded assembly helpers are verified
as bytes and NEVER executed. Existing package destinations are not overwritten.

`driver.py` exposes identity/restore/slice/verify-consumer operations for a trusted
caller. The worker has no API credentials, transport keys or dispatch logic.
It uses the existing source builder, graceful Ninja stop and platform-specific
checkpoint codecs. Interrupted compiler outputs are not blindly cached. Progress
includes rebuilt outputs, not only new filenames. Every checkpoint has an exact
semantic contract, recipe, working path, runner image and orchestration scope.
Cross-repository or changed-image restoration is deliberately rejected; a future
migration must be separately reviewed rather than weakening those checks.

`install.cmake` is consumed by a thin, hash-tracked external port. The caller
materializes `cef-build.json` in the port BEFORE ABI calculation. Run/attempt,
cache location, slice budget and resume selection are operational state, not
package ABI. The compiler recipe, linkage profile and release hashes are ABI
inputs. Native source installation still runs the original native tests and
exporter; incomplete slices never return a successful port installation.

## Scope and outstanding qualification

The implemented profile is **engine-static**, C API, x64, Release. The original
Linux release still links platform libraries dynamically. Windows uses /MT and
OS DLLs. Sandbox, GPU and arbitrary web content are not certified by the fixed
local smoke fixture. `static-third-party` is intentionally rejected, not silently
downgraded. Completing that profile still requires static NSS/D-Bus and the full
Linux platform closure, GN/pkg-config wiring and runtime module qualification.
No existing release is renamed or promoted to a stronger capability claim.

`release.lock.json` records the manifest hashes of the explicitly selected
`cef-152.0.6-static-capi-r35179388662-a1` release. It is a pin, not a latest-release
lookup. A moved/replaced asset with different bytes fails validation.

## Tests

Run `python -m unittest discover -s vcpkg/integration/tests -v` from the repository
root. Tests use synthetic SDK bytes and receipts; their success is NOT native
Chromium execution. Both Windows and Linux CI run this contract suite.
