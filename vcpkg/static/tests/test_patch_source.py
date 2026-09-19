"""Regression for the Windows platform-STL PartitionAlloc warning transform."""
from pathlib import Path
import importlib.util
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
PATCH = ROOT / "vcpkg/ports/cef-static/patch_source.py"
spec = importlib.util.spec_from_file_location("cef_patch_source", PATCH)
patch_source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch_source)


class PatchSourceTests(unittest.TestCase):
    def test_windows_msvc_emulation_matches_reviewed_native_stl(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "build/config/win/BUILD.gn"
            path.parent.mkdir(parents=True)
            path.write_text(
                '''config("compiler") {
  if (is_clang) {
    # Tell clang which version of MSVC to emulate.
    cflags += [ "-fmsc-version=1934" ]
  }
}
''',
                encoding="utf-8",
            )
            patch_source.EDITS.clear()
            patch_source.patch_windows_msvc_version(root)
            changed = path.read_text()
            self.assertNotIn('-fmsc-version=1934', changed)
            self.assertEqual(changed.count('-fmsc-version=1944'), 1)
            self.assertEqual(len(patch_source.EDITS), 1)

    def test_ctad_warning_remains_enabled_off_windows_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "base/allocator/partition_allocator/src/partition_alloc/BUILD.gn"
            path.parent.mkdir(parents=True)
            path.write_text(
                """config("dependants_extra_warnings") {
  if (build_with_chromium && is_clang && !is_fuchsia) {
    cflags = [
      "-Wc++11-narrowing",
      "-Wconditional-uninitialized",
      "-Wcstring-format-directive",
      "-Wctad-maybe-unsupported",
      "-Wdeprecated-copy",
      "-Wunused-but-set-variable",
      "-Wunused-macros",
    ]
  }
}
""",
                encoding="utf-8",
            )
            webrtc = root / "third_party/webrtc/BUILD.gn"
            webrtc.parent.mkdir(parents=True)
            webrtc.write_text(
                """config("common_config") {
  cflags = []
  if (is_clang) {
    cflags += [
      "-Wshadow",

      # See https://reviews.llvm.org/D56731 for details about this
      # warning.
      "-Wctad-maybe-unsupported",
    ]
  }
}
""",
                encoding="utf-8",
            )
            compiler = root / "build/config/compiler/BUILD.gn"
            compiler.parent.mkdir(parents=True)
            compiler.write_text(
                """config("exceptions") {
  if (is_win) {
    if (!use_custom_libcxx) {
      defines = [ "_HAS_EXCEPTIONS=1" ]
    }
  }
}
config("no_exceptions") {
  if (is_win) {
    if (!use_custom_libcxx) {
      defines = [ "_HAS_EXCEPTIONS=0" ]
    }
  }
}
""",
                encoding="utf-8",
            )
            patch_source.EDITS.clear()
            patch_source.patch_windows_msvc_stl_warnings(root)
            changed = path.read_text()
            self.assertEqual(changed.count('cflags += [ "-Wctad-maybe-unsupported" ]'), 1)
            self.assertEqual(changed.count('cflags += [ "-Wno-ctad-maybe-unsupported" ]'), 1)
            self.assertIn('if (is_win) {', changed)
            webrtc_changed = webrtc.read_text()
            self.assertIn('cflags += [ "-Wno-ctad-maybe-unsupported" ]', webrtc_changed)
            self.assertIn('cflags += [ "-Wctad-maybe-unsupported" ]', webrtc_changed)
            self.assertNotIn('cflags += [\n      "-Wshadow",', webrtc_changed)
            compiler_changed = compiler.read_text()
            self.assertEqual(
                compiler_changed.count(
                    "_SILENCE_CXX20_OLD_SHARED_PTR_ATOMIC_SUPPORT_DEPRECATION_WARNING"
                ),
                2,
            )
            self.assertIn('"_HAS_EXCEPTIONS=1",', compiler_changed)
            self.assertIn('"_HAS_EXCEPTIONS=0",', compiler_changed)
            self.assertEqual(len(patch_source.EDITS), 5)


    def test_platform_stl_language_tags_avoid_constexpr_dynamic_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            language = root / "base/i18n/language_tag.h"
            language.parent.mkdir(parents=True)
            language.write_text(
                """consteval LanguageTag GetKnownLanguageTag(std::string_view tag) {
  std::optional<i18n_internal::ParsedBcp47Tag> parsed =
      i18n_internal::ParseBcp47Tag(tag);
  if (!parsed) {
    void ERROR_TagIsMalformed();
    ERROR_TagIsMalformed();
  }

  if (!i18n_internal::AreSubtagsKnown(*parsed)) {
    void ERROR_TagIsUnknown();
    ERROR_TagIsUnknown();
  }

  // It is only possible to construct LanguageTags at compile-time if they
  // are small.
  if (tag.size() > i18n_internal::ImmutableString::kSmallBufferSize) {
    void ERROR_TagIsTooLarge();
    ERROR_TagIsTooLarge();
  }

  return LanguageTag(base::span<const std::string_view>({tag}));
}

constexpr std::optional<LanguageTag> LanguageTag::GetParentTag() const {
""",
                encoding="utf-8",
            )
            time_formatting = root / "base/i18n/time_formatting.cc"
            time_formatting.parent.mkdir(parents=True)
            time_formatting.write_text(
                '  static constexpr i18n::LanguageTag en_us = i18n::GetKnownLanguageTag("en-US");\n',
                encoding="utf-8",
            )
            patch_source.EDITS.clear()
            patch_source.patch_windows_msvc_consteval_language_tags(root)
            changed = language.read_text()
            self.assertIn("#if defined(_MSVC_STL_UPDATE)", changed)
            self.assertIn("std::string_view subtags[8]", changed)
            self.assertIn("IsKnownLanguageSubtag", changed)
            self.assertIn("IsKnownScriptSubtag", changed)
            self.assertIn("IsKnownRegionSubtag", changed)
            self.assertIn("IsKnownVariantSubtag", changed)
            self.assertIn("seen_singletons", changed)
            self.assertIn("IsExtensionSubtag", changed)
            self.assertIn("IsPrivateUseSubtag", changed)
            self.assertIn("#else", changed)
            self.assertEqual(changed.count("i18n_internal::ParseBcp47Tag(tag)"), 1)
            time_changed = time_formatting.read_text()
            self.assertIn("#if defined(_MSVC_STL_UPDATE)", time_changed)
            self.assertIn(
                'constexpr i18n::LanguageTag en_us = i18n::GetKnownLanguageTag("en-US");',
                time_changed,
            )
            self.assertIn("static constexpr i18n::LanguageTag en_us", time_changed)
            self.assertEqual(len(patch_source.EDITS), 3)


if __name__ == "__main__":
    unittest.main()
