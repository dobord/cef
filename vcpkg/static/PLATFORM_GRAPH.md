# Static platform graph (experimental, Linux x64)

`platform_contract.py capture` resolves an explicit set of pkg-config modules
with `--static` and with search limited to the selected installed prefix. It
records actual archives, public headers, pkg-config metadata, module versions
and tool provenance. Every archive member must be a native x64 ELF relocatable
object; thin/shared/nested/bitcode inputs are refused. This is build-input
evidence, not a runtime certificate.

`gn_platform.py --source SRC --manifest FILE --prefix PREFIX --sha256 HASH`
requires the complete reference module catalog before changing any source.
It verifies exact Chromium GN file hashes, installs the credential-free query
helper and emits the six explicit GN arguments. The canonical source builder
accepts the same explicit inputs (see below); do not reuse an engine-only
checkpoint or overwrite another prefix binding. A missing module is an error, not a request
to link a system shared library.

The modified pkg_config template uses the manifest only in the default target
toolchain. Separate host tools keep their original pkg-config behavior. Absolute
archive paths stay in GN `libs`; compile/link flags stay in their proper fields.
LLD is required for cyclic archive resolution without hiding libraries in flags.
CUPS and both ALSA call sites, which otherwise bypass pkg-config, use their
captured target modules in the explicit static mode. The post-generation
`audit_graph` helper rejects uncaptured archives and bare non-OS libraries.

The native regression evaluates the exact patched upstream pkg_config template
inside a small GN build, links cyclic real archives, executes target and host
programs and checks rejection of a changed header. Its catalog aliases refer to
fixture libraries, NOT to built Chromium dependencies. The other Chromium GN
files are hash-checked and transformed, not compiled as complete components by
that fixture.

## Canonical source build and iterations

For a complete real dependency prefix, capture its contract using the modules
in `gn_platform.MODULES`, then use the exact recorded SHA-256:

```sh
python vcpkg/ports/cef-static/source_build.py check \
  --work /absolute/work --logs /absolute/logs \
  --platform-manifest /absolute/work/platform.json \
  --platform-prefix /absolute/work/target-prefix \
  --platform-sha256 "$PLATFORM_SHA256"
```

`check`, `regressions` and `build` validate the complete frozen prefix before
source preparation. The ordinary CEF patch/profile pipeline still runs. Only
then are the reviewed platform GN edits applied and merged into the canonical
arguments. The explicit profile uses `out/CEF_Static_Platform_Release_x64` and
checks the actual `gn desc` root after generation. Neither a missing contract
nor a bound workspace can silently fall back to the engine-only configuration.
Unchanged `args.gn` contents retain their file clock.

`vcpkg/integration/driver.py` accepts the same three flags for `identity`,
`restore` and `slice`. The manifest and dependency prefix must both reside
inside `--work`, with the manifest outside the prefix. Thus the original
headers, archives and timestamps travel in the source checkpoint rather than
being re-extracted on every runner. The worker identity binds the manifest,
prefix paths and both GN/query helper digests. After restoring the checkpoint,
the worker verifies the prefix bytes before recording restoration success.
Existing engine-only worker schema and Windows behavior are unchanged; adopting
this profile changes checkpoint identity and requires explicit fresh inputs.

The generic signed-plan builder and package installer have not yet been wired
to select this source-worker profile. These flags expose a tested native worker
interface, not a new production release profile. Native reference receipts keep
`system_libraries_static: false` and `platform_runtime_qualified: false` until
all runtime module and combined-application checks have been completed.

## Not yet qualified

This does not enable the builder's `static-third-party` production profile.
The installed full dependency prefix, actual Chromium GN generation and final
link/runtime modules must still be qualified together. The existing release
receipt is engine-static and cannot certify these new build inputs. The signed
builder plan, package installer and relocatable external-archive export still
need integration before this can become a source-built SDK profile.

GBM/DRI drivers, NSS external PKCS11 modules, GLib/GIO modules, X11 locale modules
and ALSA plugins need runtime policy and tests beyond archive validation. The
catalog deliberately continues to require GBM rather than replacing it with an
empty stub or disabling graphics to make a check pass. CUPS TLS must remain real;
using OpenSSL together with Chromium's BoringSSL requires symbol/ABI review.
