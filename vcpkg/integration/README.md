# Static SDK integration adapter

This adapter leaves the tested source recipe, Ninja graph and checkpoint codecs
unchanged. `port.py` supports explicitly locked `source` and `release` acquisition.
There is no automatic fallback. Native source slices are handled by `iteration.py`;
only a completed, tested package may be installed by vcpkg.

`release_import.py` verifies all pinned assets, the canonical SDK manifest, every
ZIP member and the original producer receipt. Downloaded Python helpers are data,
never executable input. Only the CEF-owned include/lib/share payload is imported;
old vcpkg status, toolchains and triplet names are not copied into the new install.
Original receipts remain unchanged and a separate acquisition receipt records the
conversion. Neither an import nor a checkpoint certifies the new consumer runtime.

The `static-third-party` Linux profile replaces external system-library names with
regular static archives resolved only in the vcpkg target prefix. Private pkg-config
dependencies are retained; unknown flags, external directories, absent archives,
shared fallback and thin archives fail closed. The generated CMake interface is
relocatable and uses archive groups to preserve cyclic dependencies. External
archives remain owned by their vcpkg packages; they are not duplicated inside CEF.

A source build still uses the original Chromium build environment for its native
reference test. The final consumer is separately linked against the selected
vcpkg dependency closure. This preserves existing compilation checkpoints; it does
not assert ABI compatibility before the final consumer has linked and run.

The strict profile permits the documented OS interface, not arbitrary system
GLib/NSS/X11/ALSA libraries. It is not a claim of a glibc-free Linux executable,
certified sandbox, hardware GPU support, or successful testing of every plugin.
The builder must run its package, ELF/PE and runtime-module gates on the actual
combined SDK and only then issue a publication receipt.

Tests use synthetic transport data and a small native C static-library consumer.
Run `python -m unittest discover -s vcpkg/integration/tests -v` from repository root.
These tests are not a substitute for full Chromium/SDK runtime validation.
