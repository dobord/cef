#!/usr/bin/env python3
"""Pinned, fail-closed source transformations. Run AFTER upstream CEF patches.

This changes engine targets, not the binary SDK's client wrapper. Every edit
requires exactly one matching original context; source updates need review.
"""
from __future__ import annotations
import argparse
import difflib
import hashlib
import json
from pathlib import Path

EDITS: list[dict[str, str]] = []


def replace(root: Path, file: str, old: str, new: str) -> None:
    path = root / file
    text = path.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise RuntimeError(f'{file}: expected exactly one original context, found {text.count(old)}')
    changed = text.replace(old, new, 1)
    EDITS.append({'file': file, 'before_sha256': hashlib.sha256(text.encode()).hexdigest(),
                  'after_sha256': hashlib.sha256(changed.encode()).hexdigest(),
                  'diff': ''.join(difflib.unified_diff(text.splitlines(True), changed.splitlines(True),
                                                     fromfile='a/'+file, tofile='b/'+file))})
    path.write_text(changed, encoding='utf-8', newline='\n')


def patch_vulkan_disabled(root: Path) -> None:
    # Chromium 152 only defines VulkanContextProvider with ENABLE_VULKAN.
    # Match the guarded device_queue pattern in CreateSharedImageInternal.
    replace(root, 'gpu/command_buffer/service/shared_image/ozone_image_backing_factory.cc',
            """    gfx::BufferUsage usage) {
  scoped_refptr<gfx::NativePixmap> pixmap =""",
            """    gfx::BufferUsage usage) {
  VulkanDeviceQueue* device_queue = nullptr;
#if BUILDFLAG(ENABLE_VULKAN)
  if (vulkan_context_provider) {
    device_queue = vulkan_context_provider->GetDeviceQueue();
  }
#endif  // BUILDFLAG(ENABLE_VULKAN)
  scoped_refptr<gfx::NativePixmap> pixmap =""")
    replace(root, 'gpu/command_buffer/service/shared_image/ozone_image_backing_factory.cc',
            """                               vulkan_context_provider
                                   ? vulkan_context_provider->GetDeviceQueue()
                                   : nullptr,""",
            '                               device_queue,')


def patch_dawn_ozone_dependencies(root: Path) -> None:
    # Run 34565392341: Dawn/Ozone is built with use_dawn && use_ozone even
    # when Chromium's enable_vulkan=false. It still includes sync/sync.h and
    # vulkan/vulkan.h and calls sync_merge and DRM format helpers. Upstream
    # only supplies those dependencies in the separate enable_vulkan branch.
    # Keep Dawn enabled and depend on the bundled source_set, not a system
    # libsync package or a Vulkan loader shared library.
    replace(root, 'gpu/command_buffer/service/BUILD.gn',
            '''    if (use_ozone) {
      sources += [
        "shared_image/dawn_ozone_image_representation.cc",
        "shared_image/dawn_ozone_image_representation.h",
      ]
    }
''',
            '''    if (use_ozone) {
      sources += [
        "shared_image/dawn_ozone_image_representation.cc",
        "shared_image/dawn_ozone_image_representation.h",
      ]
      if ((is_linux || is_chromeos) && !enable_vulkan) {
        deps += [
          "//third_party/libsync",
          "//third_party/vulkan-headers/src:vulkan_headers",
          "//ui/gfx/linux:drm",
        ]
      }
    }
''')


def patch_windows_msvc_version(root: Path) -> None:
    # Chromium 152 hard-codes Clang's emulated MSVC version to 19.34 while
    # the reviewed Windows runner supplies MSVC STL 14.44. The mismatch changes
    # STL feature macros/constexpr availability and breaks valid C++23 code in
    # base/i18n. Match the exact reviewed native toolset; source_build.py
    # independently fails closed unless that toolset is actually installed.
    replace(root, 'build/config/win/BUILD.gn',
            '    cflags += [ "-fmsc-version=1934" ]\n',
            '    cflags += [ "-fmsc-version=1944" ]\n')


def patch_windows_msvc_stl_warnings(root: Path) -> None:
    # Chromium's PartitionAlloc enables -Wctad-maybe-unsupported for all clang
    # builds. With the reviewed Windows platform STL this warns on valid
    # std::lock_guard CTAD and /WX turns it into an error. Keep the warning on
    # every non-Windows target and preserve the rest of Chromium's warning set.
    file = 'base/allocator/partition_allocator/src/partition_alloc/BUILD.gn'
    replace(root, file,
            '''      "-Wcstring-format-directive",
      "-Wctad-maybe-unsupported",
      "-Wdeprecated-copy",
''',
            '''      "-Wcstring-format-directive",
      "-Wdeprecated-copy",
''')
    replace(root, file,
            '''      "-Wunused-but-set-variable",
      "-Wunused-macros",
    ]
''',
            '''      "-Wunused-but-set-variable",
      "-Wunused-macros",
    ]
    if (is_win) {
      # Native MSVC STL uses valid CTAD patterns without deduction guides in
      # headers such as <functional>. Keep /WX, but suppress only this Clang
      # diagnostic for the reviewed Windows platform-STL target.
      cflags += [ "-Wno-ctad-maybe-unsupported" ]
    } else {
      cflags += [ "-Wctad-maybe-unsupported" ]
    }
''')


    # MSVC STL deliberately deprecates the C++20 shared_ptr atomic free
    # functions still used by pinned Perfetto. Chromium keeps deprecation
    # warnings as errors, so silence exactly this STL compatibility diagnostic
    # only when the Windows target uses the platform STL.
    compiler = 'build/config/compiler/BUILD.gn'
    replace(root, compiler,
            '      defines = [ "_HAS_EXCEPTIONS=1" ]\n',
            '''      defines = [
        "_HAS_EXCEPTIONS=1",
        "_SILENCE_CXX20_OLD_SHARED_PTR_ATOMIC_SUPPORT_DEPRECATION_WARNING",
      ]
''')
    replace(root, compiler,
            '      defines = [ "_HAS_EXCEPTIONS=0" ]\n',
            '''      defines = [
        "_HAS_EXCEPTIONS=0",
        "_SILENCE_CXX20_OLD_SHARED_PTR_ATOMIC_SUPPORT_DEPRECATION_WARNING",
      ]
''')


    # WebRTC has its own late-applied common_config and independently enables
    # -Wctad-maybe-unsupported. With the native Windows STL this warns on valid
    # std::less_equal{} CTAD in Chromium headers. Override only that diagnostic
    # in the same WebRTC config so the flag ordering is deterministic.
    webrtc = 'third_party/webrtc/BUILD.gn'
    replace(root, webrtc,
            '''  if (is_clang) {
    cflags += [
      "-Wshadow",

      # See https://reviews.llvm.org/D56731 for details about this
      # warning.
      "-Wctad-maybe-unsupported",
    ]
  }
''',
            '''  if (is_clang) {
    cflags += [ "-Wshadow" ]
    if (is_win) {
      cflags += [ "-Wno-ctad-maybe-unsupported" ]
    } else {
      cflags += [ "-Wctad-maybe-unsupported" ]
    }
  }
''')



def patch_windows_msvc_consteval_language_tags(root: Path) -> None:
    # Chromium 152 validates known BCP47 tags at compile time through a parser
    # backed by std::optional/std::vector. clang-cl with the reviewed MSVC
    # 14.44 STL cannot destroy that dynamic-container graph in constant
    # evaluation. Preserve the same accepted known-tag contract on platform-STL
    # targets with a bounded allocation-free parser; libc++ keeps upstream code.
    replace(root, 'base/i18n/language_tag.h',
            '''consteval LanguageTag GetKnownLanguageTag(std::string_view tag) {
  std::optional<i18n_internal::ParsedBcp47Tag> parsed =
''',
            '''consteval LanguageTag GetKnownLanguageTag(std::string_view tag) {
#if defined(_MSVC_STL_UPDATE)
  void ERROR_TagIsMalformed();
  void ERROR_TagIsUnknown();
  void ERROR_TagIsTooLarge();

  if (tag.empty()) {
    ERROR_TagIsMalformed();
  }
  if (tag.size() > i18n_internal::ImmutableString::kSmallBufferSize) {
    ERROR_TagIsTooLarge();
  }

  std::string_view subtags[8] = {};
  size_t count = 0;
  size_t start = 0;
  while (true) {
    if (count == 8) {
      ERROR_TagIsMalformed();
    }
    size_t end = tag.find('-', start);
    subtags[count] =
        tag.substr(start, end == std::string_view::npos ? tag.size() - start
                                                        : end - start);
    if (subtags[count].empty()) {
      ERROR_TagIsMalformed();
    }
    ++count;
    if (end == std::string_view::npos) {
      break;
    }
    start = end + 1;
  }

  size_t index = 0;
  if (!i18n_internal::IsLanguageSubtag(subtags[index])) {
    ERROR_TagIsMalformed();
  }
  if (!i18n_internal::IsKnownLanguageSubtag(subtags[index])) {
    ERROR_TagIsUnknown();
  }
  ++index;

  if (index < count && i18n_internal::IsScriptSubtag(subtags[index])) {
    if (!i18n_internal::IsKnownScriptSubtag(subtags[index])) {
      ERROR_TagIsUnknown();
    }
    ++index;
  }
  if (index < count && i18n_internal::IsRegionSubtag(subtags[index])) {
    if (!i18n_internal::IsKnownRegionSubtag(subtags[index])) {
      ERROR_TagIsUnknown();
    }
    ++index;
  }
  while (index < count && i18n_internal::IsVariantSubtag(subtags[index])) {
    if (!i18n_internal::IsKnownVariantSubtag(subtags[index])) {
      ERROR_TagIsUnknown();
    }
    ++index;
  }

  unsigned long long seen_singletons = 0;
  while (index < count &&
         i18n_internal::IsExtensionSingleton(subtags[index])) {
    char singleton = base::ToLowerASCII(subtags[index].front());
    unsigned bit = base::IsAsciiDigit(singleton)
                       ? static_cast<unsigned>(singleton - '0')
                       : 10u + static_cast<unsigned>(singleton - 'a');
    unsigned long long mask = 1ull << bit;
    if (seen_singletons & mask) {
      ERROR_TagIsMalformed();
    }
    seen_singletons |= mask;
    ++index;
    size_t extension_start = index;
    while (index < count &&
           i18n_internal::IsExtensionSubtag(subtags[index])) {
      ++index;
    }
    if (index == extension_start) {
      ERROR_TagIsMalformed();
    }
  }

  if (index < count &&
      (subtags[index] == "x" || subtags[index] == "X")) {
    ++index;
    size_t private_start = index;
    while (index < count &&
           i18n_internal::IsPrivateUseSubtag(subtags[index])) {
      ++index;
    }
    if (index == private_start) {
      ERROR_TagIsMalformed();
    }
  }
  if (index != count) {
    ERROR_TagIsMalformed();
  }

  return LanguageTag(base::span<const std::string_view>({tag}));
#else
  std::optional<i18n_internal::ParsedBcp47Tag> parsed =
''')
    replace(root, 'base/i18n/language_tag.h',
            '''  return LanguageTag(base::span<const std::string_view>({tag}));
}

constexpr std::optional<LanguageTag> LanguageTag::GetParentTag() const {
''',
            '''  return LanguageTag(base::span<const std::string_view>({tag}));
#endif
}

constexpr std::optional<LanguageTag> LanguageTag::GetParentTag() const {
''')
    # MSVC std::variant remains non-trivially destructible at runtime even
    # when constant evaluation selected ImmutableString's stack alternative.
    # Avoid static storage only for this local target-STL value.
    replace(root, 'base/i18n/time_formatting.cc',
            '  static constexpr i18n::LanguageTag en_us = i18n::GetKnownLanguageTag("en-US");\n',
            '''#if defined(_MSVC_STL_UPDATE)
  constexpr i18n::LanguageTag en_us = i18n::GetKnownLanguageTag("en-US");
#else
  static constexpr i18n::LanguageTag en_us = i18n::GetKnownLanguageTag("en-US");
#endif
''')


def patch_windows_missing_string_include(root: Path) -> None:
    # Chromium's cert_util.h exposes std::string in its public declaration but
    # only includes <string_view>. Older transitive MSVC STL includes masked
    # this; the reviewed 14.44 toolset correctly requires the direct include.
    replace(root, 'net/tools/transport_security_state_generator/cert_util.h',
            '''#include <stdint.h>

#include <string_view>
''',
            '''#include <stdint.h>

#include <string>
#include <string_view>
''')


def patch_static_cefclient_pkgconfig(root: Path) -> None:
    # The static engine root does not build cefclient. GN still evaluates the
    # cefclient pkg_config target while loading //cef/BUILD.gn, so do not query
    # gtk+-unix-print-3.0 from the closed target manifest for this profile.
    # Ordinary CEF/cefclient builds retain the upstream printing dependency.
    replace(root, 'cef/BUILD.gn',
            '''    pkg_config("gtk") {
      packages = [
        "gmodule-2.0",
        "gtk+-3.0",
        "gthread-2.0",
        "gtk+-unix-print-3.0",
        "xi",
      ]
    }
''',
            '''    pkg_config("gtk") {
      packages = [
        "gmodule-2.0",
        "gtk+-3.0",
        "gthread-2.0",
        "xi",
      ]
      if (!cef_static_engine) {
        packages += [ "gtk+-unix-print-3.0" ]
      }
    }
''')


def patch_gpu_init_filter_set(root: Path) -> None:
    # filter_set is read by either Vulkan or the ChromeOS Dawn filter. A
    # Vulkan-disabled non-ChromeOS Ozone build has neither reader. Keep both
    # original users and their mutual-exclusion check; do not suppress -Werror.
    replace(root, 'gpu/ipc/service/gpu_init.cc',
            '  bool filter_set = false;\n#if BUILDFLAG(ENABLE_VULKAN)\n',
            '#if BUILDFLAG(ENABLE_VULKAN) || '
            '(BUILDFLAG(SKIA_USE_DAWN) && BUILDFLAG(IS_CHROMEOS))\n'
            '  bool filter_set = false;\n'
            '#endif\n'
            '#if BUILDFLAG(ENABLE_VULKAN)\n')


# The pinned file is not modified by the upstream CEF patch set.
X11_FILE = 'components/viz/service/display_embedder/skia_output_surface_impl_on_gpu.cc'
X11_ORIGINAL_BLOB = 'd8485e68ef15f944a4d45facf2b6447a53412944'
X11_OLD = '#if BUILDFLAG(ENABLE_VULKAN)\n// Returns whether SkiaOutputDeviceX11 can be instantiated on this platform.\nbool MayFallBackToSkiaOutputDeviceX11() {\n#if BUILDFLAG(IS_OZONE)\n  return ui::OzonePlatform::GetInstance()\n      ->GetPlatformProperties()\n      .skia_can_fall_back_to_x11;\n#else\n  return false;\n#endif  // BUILDFLAG(IS_OZONE)\n}\n#endif  // BUILDFLAG(ENABLE_VULKAN)\n'
X11_NEW = '#if BUILDFLAG(ENABLE_VULKAN) || \\\n    (BUILDFLAG(SKIA_USE_DAWN) && BUILDFLAG(SUPPORTS_OZONE_X11))\n// Returns whether SkiaOutputDeviceX11 can be instantiated on this platform.\nbool MayFallBackToSkiaOutputDeviceX11() {\n#if BUILDFLAG(IS_OZONE)\n  return ui::OzonePlatform::GetInstance()\n      ->GetPlatformProperties()\n      .skia_can_fall_back_to_x11;\n#else\n  return false;\n#endif  // BUILDFLAG(IS_OZONE)\n}\n#endif  // Vulkan or Dawn/X11 fallback\n'


def patch_skia_x11_fallback(root: Path) -> None:
    """Match the union of the two callers, without enabling Vulkan or disabling Dawn.

    Idempotence is limited to this exact pinned transformation so an interrupted
    marker upgrade can be retried without reapplying unrelated source patches.
    """
    path = root / X11_FILE
    text = path.read_text(encoding='utf-8')
    already = text.count(X11_NEW) == 1 and X11_OLD not in text
    original = text.replace(X11_NEW, X11_OLD, 1) if already else text
    data = original.encode('utf-8')
    blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    if blob != X11_ORIGINAL_BLOB or original.count(X11_OLD) != 1:
        raise RuntimeError('Pinned Skia X11 fallback source/context mismatch')
    if not already:
        replace(root, X11_FILE, X11_OLD, X11_NEW)


def patch(root: Path) -> None:
    patch_vulkan_disabled(root)
    patch_dawn_ozone_dependencies(root)
    patch_windows_msvc_version(root)
    patch_windows_msvc_stl_warnings(root)
    patch_windows_msvc_consteval_language_tags(root)
    patch_windows_missing_string_include(root)
    patch_static_cefclient_pkgconfig(root)
    patch_gpu_init_filter_set(root)
    patch_skia_x11_fallback(root)
    replace(root, 'cef/libcef/features/features.gni', '  enable_cef = true\n',
            '  enable_cef = true\n\n  # Build an engine archive, without the DLL-wrapper ABI boundary.\n  cef_static_engine = false\n')
    replace(root, 'cef/include/internal/cef_export.h', '#if defined(COMPILER_MSVC)\n',
            '#if defined(CEF_STATIC)\n\n#define CEF_EXPORT\n\n#elif defined(COMPILER_MSVC)\n')
    replace(root, 'cef/libcef/features/BUILD.gn', '    "USING_CHROMIUM_INCLUDES",\n  ]\n',
            '    "USING_CHROMIUM_INCLUDES",\n  ]\n  if (cef_static_engine) {\n    defines += [ "CEF_STATIC" ]\n  }\n')
    # These switch/crash-key objects also occur in the browser closure.
    marker = '  static_library("chrome_elf_set") {\n'
    file = 'cef/BUILD.gn'
    text = (root/file).read_text()
    begin = text.index(marker)
    end = text.index('    configs += [', begin)
    old = text[begin:end]
    new = old + '''    if (cef_static_engine) {
      sources -= [
        "//chrome/common/crash_keys.cc",
        "//chrome/common/chrome_switches.cc",
        "//components/webui/flags/flags_ui_switches.cc",
        "//content/public/common/content_switches.cc",
      ]
    }

'''
    replace(root, file, old, new)
    text = (root/file).read_text()
    replace(root, file, text[-100:], text[-100:] + '''

# Explicit static-engine targets. Existing DLL targets are not used here.
if (cef_static_engine) {
  assert(is_win || is_linux, "Static engine supports Windows/Linux only")
  assert(target_cpu == "x64", "Static engine supports x64 only")
  assert(!is_component_build, "A component build is not a static engine")
  assert(use_static_angle, "ANGLE must be linked into the engine")
  assert(!enable_swiftshader, "Do not package a shared SwiftShader runtime")

  config("cef_static_consumer") {
    include_dirs = [ ".", "$root_gen_dir/cef" ]
    defines = [ "CEF_STATIC", "CEF_API_VERSION=15200" ]
  }

  static_library("cef_engine") {
    testonly = true
    # Preserve GN archive/source_set semantics; export the complete closure.
    output_name = "cef_engine"
    sources = libcef_sources_common
    deps = libcef_deps_common + [ "//build/config:executable_deps" ]
    configs += [ ":libcef_autogen_config", ":libcef_includes_config" ]
    public_configs = [ ":cef_static_consumer" ]
    assert_no_deps = [ ":libcef", ":libcef_dll_wrapper" ]
    if (is_win) {
      sources += includes_win
      libs = [ "comctl32.lib", "dxguid.lib" ]
    }
  }

  if (is_win) {
    source_set("cef_static_resources") {
      sources = [ "libcef_dll/libcef_dll.rc" ]
      configs += [ ":libcef_includes_config" ]
      deps = [ ":cef_make_headers", "//ui/resources:ui_unscaled_resources_grd" ]
    }
  }

  executable("cef_static_smoke") {
    testonly = true
    sources = [ "static/smoke.c" ]
    deps = [ ":cef_engine" ]
    use_libcxx_modules = false
    assert_no_deps = [ ":libcef", ":libcef_dll_wrapper" ]
    if (is_win) {
      deps += [ ":cef_static_resources" ]
      ldflags = [ "/STACK:0x800000" ]
    }
  }
}
''')
    replace(root, 'chrome/chrome_elf/BUILD.gn', 'shared_library("chrome_elf") {', '''_cef_elf_target_type = "shared_library"
if (cef_static_engine) {
  _cef_elf_target_type = "source_set"
}
target(_cef_elf_target_type, "chrome_elf") {''')
    replace(root, 'chrome/chrome_elf/BUILD.gn', '  configs += [ "//build/config/win:windowed" ]', '''  if (cef_static_engine) {
    sources -= [ "chrome_elf.def" ]
    deps -= [ ":chrome_elf_manifest", ":chrome_elf_resources" ]
    configs += [ "//cef/libcef/features:config" ]
  }
  configs += [ "//build/config/win:windowed" ]''')
    # source_set has no default console config, unlike DLL/executable targets.
    replace(root, 'chrome/chrome_elf/BUILD.gn',
            '  configs -= [ "//build/config/win:console" ]',
            '  if (!cef_static_engine) {\n    configs -= [ "//build/config/win:console" ]\n  }')
    replace(root, 'chrome/chrome_elf/chrome_elf_main.h', 'void SignalChromeElf();', '''void SignalChromeElf();
#if defined(CEF_STATIC)
void CefInitializeChromeElfForStatic();
#endif''')
    replace(root, 'chrome/chrome_elf/chrome_elf_main.cc', '#include <assert.h>',
            '#include <assert.h>\n#if defined(CEF_STATIC)\n#include <stdlib.h>\n#endif')
    replace(root, 'chrome/chrome_elf/chrome_elf_main.cc',
            'BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID reserved) {', '''#if defined(CEF_STATIC)
static BOOL APIENTRY ChromeElfStaticProcessEvent(HMODULE module, DWORD reason,
                                                 LPVOID reserved) {
#else
BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID reserved) {
#endif''')
    replace(root, 'chrome/chrome_elf/chrome_elf_main.cc', 'void DumpProcessWithoutCrash() {', '''#if defined(CEF_STATIC)
namespace {
void ShutdownStaticChromeElf() {
  ChromeElfStaticProcessEvent(GetModuleHandle(nullptr), DLL_PROCESS_DETACH, nullptr);
}
BOOL CALLBACK InitializeStaticChromeElf(PINIT_ONCE, PVOID, PVOID*) {
  if (!ChromeElfStaticProcessEvent(GetModuleHandle(nullptr), DLL_PROCESS_ATTACH, nullptr))
    return FALSE;
  return atexit(ShutdownStaticChromeElf) == 0;
}
}  // namespace
void CefInitializeChromeElfForStatic() {
  static INIT_ONCE once = INIT_ONCE_STATIC_INIT;
  if (!InitOnceExecuteOnce(&once, InitializeStaticChromeElf, nullptr, nullptr))
    RaiseFailFastException(nullptr, nullptr, 0);
}
#endif

void DumpProcessWithoutCrash() {''')
    replace(root, 'chrome/install_static/BUILD.gn', 'import("//testing/test.gni")',
            'import("//testing/test.gni")\nimport("//cef/libcef/features/features.gni")')
    replace(root, 'chrome/install_static/BUILD.gn', '  source_set("secondary_module") {', '''  source_set("secondary_module") {
    if (cef_static_engine) {
      configs += [ "//cef/libcef/features:config" ]
    }''')
    replace(root, 'chrome/install_static/initialize_from_primary_module.cc',
            'extern "C" const install_static::InstallDetails::Payload __declspec(dllimport) *\n    GetInstallDetailsPayload();', '''#if defined(CEF_STATIC)
extern "C" void CefInitializeChromeElfForStatic();
#else
extern "C" const install_static::InstallDetails::Payload __declspec(dllimport) *
    GetInstallDetailsPayload();
#endif''')
    replace(root, 'chrome/install_static/initialize_from_primary_module.cc',
            '  InstallDetails::InitializeFromPayload(GetInstallDetailsPayload());', '''#if defined(CEF_STATIC)
  CefInitializeChromeElfForStatic();
#else
  InstallDetails::InitializeFromPayload(GetInstallDetailsPayload());
#endif''')
    replace(root, 'cef/libcef/common/crash_reporting.cc', 'namespace crash_reporting {', '''#if BUILDFLAG(IS_WIN) && defined(CEF_STATIC)
extern "C" int __cdecl SetCrashKeyValueImpl(const char*, size_t, const char*, size_t);
extern "C" int __cdecl IsCrashReportingEnabledImpl();
#endif

namespace crash_reporting {''')
    replace(root, 'cef/libcef/common/crash_reporting.cc',
            'const base::FilePath::CharType kChromeElfDllName[] =', '''#if defined(CEF_STATIC)
bool SetCrashKeyValueTrampoline(const std::string_view& key,
                                const std::string_view& value) {
  return !!SetCrashKeyValueImpl(key.data(), key.size(), value.data(), value.size());
}
bool IsCrashReportingEnabledTrampoline() {
  return !!IsCrashReportingEnabledImpl();
}
#else
const base::FilePath::CharType kChromeElfDllName[] =''')
    replace(root, 'cef/libcef/common/crash_reporting.cc',
            '#endif  // BUILDFLAG(IS_WIN)\n\nbool g_crash_reporting_enabled',
            '#endif  // CEF_STATIC\n#endif  // BUILDFLAG(IS_WIN)\n\nbool g_crash_reporting_enabled')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--upgrade-x11-fallback', action='store_true')
    args = parser.parse_args()
    if args.upgrade_x11_fallback:
        patch_skia_x11_fallback(args.source.resolve())
    else:
        patch(args.source.resolve())
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps({'schema': 1, 'edits': EDITS}, indent=2)+'\n')
    print(f'Applied {len(EDITS)} checked static-engine source edits')

if __name__ == '__main__':
    main()
