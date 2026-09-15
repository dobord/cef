# Skia X11 recovery and checkpoint migration

Run 34885160067 failed on Ubuntu at `skia_output_surface_impl_on_gpu.cc:2170`.
The helper `MayFallBackToSkiaOutputDeviceX11` was defined only for Vulkan, but
also called by Dawn with Ozone X11 when Vulkan was disabled. The new guard is
`ENABLE_VULKAN || (SKIA_USE_DAWN && SUPPORTS_OZONE_X11)`; the function body and
both call sites are unchanged. Dawn and X11 remain enabled; Vulkan stays off.

The canonical source patch verifies the complete normalized upstream file blob
`d8485e68ef15f944a4d45facf2b6447a53412944`, not just a permissive substring.
Existing workspaces receive only this exact source transformation. The source
recipe marker is atomically updated only after that patch succeeds. Unknown
recipe transitions or changed source contents fail rather than reset a build.

## Resume this recovery

The committed request pins Windows to run 34885160067 and Linux to 34844924690.
The failed Ubuntu job did not upload a new full checkpoint, so its latest
26 minutes of work cannot be recovered from a full workspace archive. The
separately saved compiler cache may avoid some repeat compilation, but is not
proof of recovered objects. Windows uses its newer successful checkpoint.

`checkpoint-migrations.json` authorizes only these two producer run IDs, attempt
numbers, source SHAs, platforms, and exact old/new recipe digests. Archive
validation still checks the old identity, including runner image, absolute
work path, repository, branch, file metadata, links and part checksums. After
configuration upgrades the source, subsequent archives use the new identity.
No generic old-recipe fallback, source reset, or deletion of good objects is used.

For the iteration AFTER this recovery, inspect its artifacts first. Increment
`sequence` and set `checkpoint_run` to the new completed producer, removing
`checkpoint_runs` when both platforms use the same producer. If they need
different producers, leave `checkpoint_run` empty and explicitly update BOTH
keys in `checkpoint_runs`. Merely increasing `sequence` without updating the
producer pins will repeat work. Never re-run a checkpoint producer in place.

## Preserve a failed Ubuntu compile without claiming success

Only a normal Ninja subcommand failure with explicit single `.o` output paths
under `obj/` can be checkpointed. Failed outputs are removed; successfully
completed objects and dependency logs remain. Symlinks, traversal, multi-output
edges, linker failures, timeouts and unsafe/signalled stops are not guessed.
The archive is uploaded with a failure-aware condition, but the compilation
step still raises an error and the overall job stays failed. SDK validation and
publication remain gated on a successful native build and consumer tests.

## Regression scope

The tests compile, link and run the original and patched helper across sixteen
flag combinations, preprocess the pinned full file to count real declarations
and call sites, test exact recipe migrations and marker atomicity, and exercise
a real failing Ninja build through archive/restore and incremental repair.
The small native fixtures are not CEF. Full Chromium compilation, CEF runtime,
relocated vcpkg consumption and complete static dependency coverage still need
their production build stages to finish successfully.
