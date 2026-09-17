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
helper and emits the six explicit GN arguments. Merge these arguments into a
separate native CEF source configuration; do not reuse an engine-only checkpoint
or overwrite another prefix binding. A missing module is an error, not a request
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

## Not yet qualified

This does not enable the builder's `static-third-party` production profile.
The installed full dependency prefix, actual Chromium GN generation and final
link/runtime modules must still be qualified together. The existing release
receipt is engine-static and cannot certify these new build inputs. Source
worker/checkpoint orchestration and relocatable external-archive export still
need integration before this can become a source-built SDK profile.

GBM/DRI drivers, NSS external PKCS11 modules, GLib/GIO modules, X11 locale modules
and ALSA plugins need runtime policy and tests beyond archive validation. The
catalog deliberately continues to require GBM rather than replacing it with an
empty stub or disabling graphics to make a check pass. CUPS TLS must remain real;
using OpenSSL together with Chromium's BoringSSL requires symbol/ABI review.
