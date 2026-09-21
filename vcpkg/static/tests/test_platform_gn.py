"""GN binding and graph boundaries; full native template is tested separately."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import gn_platform as gn


class GNTests(unittest.TestCase):
    def test_scoped_template_transformation(self):
        source='''declare_args() {
}
template("pkg_config") {
      pkgresult = exec_script(pkg_config_script, _script_args, "json")
      lib_dirs = pkgresult[3]
}
'''
        result=gn.patch_template(source)
        self.assertIn('current_toolchain == default_toolchain',result)
        self.assertNotIn('assert(use_lld',result)
        self.assertIs(gn.gn_args(Path('/manifest'), Path('/prefix'), 'a'*64)['use_lld'], True)
        self.assertIn('_enable_cache = false',result)
        self.assertIn('ldflags = pkgresult[4]',result)
        self.assertIn('[ "inputs" ] + _cef_identity_args',result)
        with self.assertRaises(ValueError): gn.patch_template(source+source)
        with self.assertRaises(ValueError): gn.patch_template('unreviewed')

    def test_static_platform_disables_dynamic_gpu_driver_paths(self):
        dri = '''import("//build/config/linux/pkg_config.gni")

pkg_config("dri") {
  packages = [ "dri" ]
}
'''
        patched = gn.patch_direct('build/config/linux/dri/BUILD.gn', dri)
        self.assertIn('config("dri") {}', patched)
        self.assertIn('pkg_config("dri")', patched)
        args = gn.gn_args(Path('/manifest'), Path('/prefix'), 'a'*64)
        self.assertIs(args['use_vaapi'], False)
        self.assertIs(args['use_v4l2_codec'], False)
        self.assertIs(args['rtc_use_pipewire'], False)
        self.assertIs(args['enable_remoting'], False)

    def test_only_reviewed_cups_and_alsa_call_sites(self):
        cups=gn.patch_direct('printing/BUILD.gn','  if (is_chromeos_device) {\n')
        self.assertIn('is_linux && cef_static_platform_manifest != ""',cups)
        for path in ('media/audio/BUILD.gn','media/midi/BUILD.gn'):
            source='import("//media/media_options.gni")\n    libs += [ "asound" ]\n'
            result=gn.patch_direct(path,source)
            self.assertIn('packages = [ "alsa" ]',result)
            self.assertIn('configs += [ ":cef_alsa_static" ]',result)
            self.assertIn('} else {\n      libs += [ "asound" ]',result)

    def test_incomplete_catalog_is_not_promoted(self):
        with patch.object(gn.contract,'load',return_value={'modules':{'nss':{}}}):
            with self.assertRaisesRegex(ValueError,'Missing static CEF modules'):
                gn.inputs(Path('/manifest'),Path('/prefix'),'a'*64)
        self.assertIn('gbm',gn.MODULES)
        self.assertIn('xshmfence',gn.MODULES)
        self.assertIn('cups',gn.MODULES)
        self.assertIn('gtk+-3.0',gn.MODULES)

    def test_graph_rejects_system_search_and_uncaptured_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';out=source/'out';prefix=root/'prefix'
            value={'archive_objects':{'lib/libtarget.a':2}}
            atomic=prefix/'lib/libatomic.a';atomic.write_bytes(b'b')
            value['archive_objects']['lib/libatomic.a']=1
            base={'libs':[str(prefix/'lib/libtarget.a'),'atomic','m'], 'ldflags':['-pthread']}
            result=gn.audit_graph(base,source,out,prefix,value)
            self.assertFalse(result['runtime_verified'])
            self.assertIn('lib/libatomic.a', result['archives'])
            for library in ('uncaptured','/usr/lib/libtarget.a',str(prefix/'lib/other.a'),str(out/'libstub.so')):
                bad=dict(base,libs=base['libs']+[library])
                with self.subTest(library=library),self.assertRaises(ValueError):
                    gn.audit_graph(bad,source,out,prefix,value)
            for field,entry in [('lib_dirs',['/usr/lib']),('ldflags',['-L/usr/lib']),('ldflags',['-Wl,-levil'])]:
                bad=dict(base,**{field:entry})
                with self.subTest(field=field),self.assertRaises(ValueError):
                    gn.audit_graph(bad,source,out,prefix,value)

    def test_graph_accepts_only_pinned_clang_builtins_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';out=source/'out';prefix=root/'prefix'
            platform=prefix/'lib/libtarget.a';platform.parent.mkdir(parents=True);platform.write_bytes(b'a')
            runtime=source/next(iter(gn.CLANG_RUNTIME_ARCHIVES))
            runtime.parent.mkdir(parents=True);runtime.write_bytes(b'b')
            stamp=source/gn.CLANG_STAMP
            stamp.write_text(gn.CLANG_PACKAGE_VERSION+'\n')
            value={'archive_objects':{'lib/libtarget.a':1}}
            graph={'libs':[str(platform),'//'+runtime.relative_to(source).as_posix(),'m'],
                   'ldflags':['-pthread']}
            result=gn.audit_graph(graph,source,out,prefix,value)
            self.assertEqual(result['toolchain_archives'],[runtime.relative_to(source).as_posix()])
            stamp.write_text('different-clang\n')
            with self.assertRaisesRegex(ValueError,'Clang package revision changed'):
                gn.audit_graph(graph,source,out,prefix,value)
            stamp.write_text(gn.CLANG_PACKAGE_VERSION+'\n')
            other=source/'third_party/llvm-build/Release+Asserts/lib/libunexpected.a'
            other.parent.mkdir(parents=True,exist_ok=True);other.write_bytes(b'c')
            with self.assertRaisesRegex(ValueError,'outside target prefix'):
                gn.audit_graph(dict(graph,libs=graph['libs']+[str(other)]),
                               source,out,prefix,value)

    def test_binding_marker_never_silently_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            gn.guard(root,None)
            (root/gn.MARKER).write_text('{}')
            with self.assertRaisesRegex(ValueError,'explicit pinned contract'): gn.guard(root,None)

if __name__=='__main__': unittest.main()
