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


def patch(root: Path) -> None:
    patch_vulkan_disabled(root)
    patch_dawn_ozone_dependencies(root)
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
    args = parser.parse_args()
    patch(args.source.resolve())
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps({'schema': 1, 'edits': EDITS}, indent=2)+'\n')
    print(f'Applied {len(EDITS)} checked static-engine source edits')

if __name__ == '__main__':
    main()
