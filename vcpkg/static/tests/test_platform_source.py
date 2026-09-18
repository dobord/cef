"""Canonical source configuration routing. No Chromium compile claim."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
PORT = ROOT/'vcpkg/ports/cef-static'
spec = importlib.util.spec_from_file_location('platform_source_build', PORT/'source_build.py')
build = importlib.util.module_from_spec(spec); spec.loader.exec_module(build)
sys.path.insert(0, str(ROOT/'vcpkg/static'))
import gn_platform as gn

class SourcePlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'source';self.logs = self.root/'logs';self.logs.mkdir()
        tools = self.source/'cef/tools';tools.mkdir(parents=True)
        (tools/'gn_args.py').write_text('import json\n'
            'def GetMergedArgs(args): return args\n'
            'def GetConfigArgs(args, *unused): return args\n'
            'def GetConfigFileContents(args): return json.dumps(args,sort_keys=True)\n')
        recipe = hashlib.sha256((PORT/'patch_source.py').read_bytes()+(PORT/'smoke.c').read_bytes()).hexdigest()
        (self.source/'cef-static-patched.json').write_text(json.dumps({'recipe':recipe}))
        self.selection = {'manifest':str(self.root/'inputs.json'), 'prefix':str(self.root/'target'), 'sha256':'a'*64}
        self.names=[]
        self.args=gn.gn_args(Path(self.selection['manifest']),Path(self.selection['prefix']),self.selection['sha256'])

    def run_stub(self, command, cwd, logs, name, **kwargs):
        self.names.append(name)
        if name == 'bind-static-platform': return json.dumps(self.args)
        if name == 'gn-graph': return json.dumps({'//cef:cef_static_smoke':{'libs':['m'],'ldflags':['-pthread']}})
        if name == 'viz-x11-graph': return '{}'
        if name == 'native-link-edge': return 'cef_static_smoke:\n  input: link\n    obj/cef_static_smoke.smoke.o\n    obj/libengine.a\n  outputs:\n'
        if name == 'audit-static-platform':
            return json.dumps({'schema':1,'status':'static-platform-graph-verified','manifest_sha256':'a'*64,'runtime_verified':False})
        return ''

    def configure(self, selection=None):
        with mock.patch.object(build,'WINDOWS',False), mock.patch.object(build,'run',side_effect=self.run_stub), \
             mock.patch.object(build,'find_binary',return_value=Path('/native-tool')), \
             mock.patch.object(build.shutil,'which',return_value=None):
            return build.configuration(self.source,self.logs,platform_inputs=selection)

    def test_explicit_graph_merges_into_canonical_source_profile(self):
        out=self.configure(self.selection)
        self.assertEqual(out.name,'CEF_Static_Platform_Release_x64')
        args=json.loads((out/'args.gn').read_text())
        for key,value in self.args.items(): self.assertEqual(args[key],value)
        self.assertFalse(args['is_component_build'])
        self.assertFalse(args['enable_vulkan'])
        self.assertTrue(args['cef_static_engine'])
        self.assertLess(self.names.index('validate-static-platform'),self.names.index('native-startup-patches'))
        self.assertLess(self.names.index('native-startup-patches'),self.names.index('bind-static-platform'))
        self.assertLess(self.names.index('bind-static-platform'),self.names.index('gn-gen'))
        self.assertLess(self.names.index('gn-graph'),self.names.index('audit-static-platform'))
        self.assertFalse(json.loads((self.logs/'platform-graph-receipt.json').read_text())['runtime_verified'])
        clock=(out/'args.gn').stat().st_mtime_ns
        self.configure(self.selection)
        self.assertEqual(clock,(out/'args.gn').stat().st_mtime_ns)

    def test_windows_target_uses_platform_msvc_stl_without_changing_host_tools(self):
        merged = {'use_custom_libcxx': True, 'use_custom_libcxx_for_host': True}
        value = build.static_profile(merged, True)
        self.assertIs(value['use_custom_libcxx'], False)
        self.assertIs(value['use_custom_libcxx_for_host'], True)
        self.assertIs(value['dawn_use_built_dxc'], False)
        self.assertIs(value['dawn_force_system_component_load'], True)
        self.assertIs(value['dawn_use_agility_sdk'], False)
        # Linux keeps the platform profile's stdlib decision unchanged.
        linux = build.static_profile(merged, False)
        self.assertIs(linux['use_custom_libcxx'], True)
        self.assertIs(linux['use_custom_libcxx_for_host'], True)

    def test_default_engine_profile_and_directory_remain_unchanged(self):
        out=self.configure()
        self.assertEqual(out.name,'CEF_Static_Release_x64')
        self.assertTrue(json.loads((out/'args.gn').read_text())['use_sysroot'])
        self.assertNotIn('bind-static-platform',self.names)

    def test_engine_only_workspace_cannot_be_migrated_implicitly(self):
        self.configure()
        self.names.clear()
        with self.assertRaisesRegex(ValueError,'fresh source workspace'): self.configure(self.selection)
        self.assertEqual(self.names,[])

    def test_bound_workspace_cannot_silently_drop_its_platform(self):
        (self.source/gn.MARKER).write_text('{}')
        with self.assertRaisesRegex(ValueError,'explicit platform inputs'): self.configure()
        self.assertEqual(self.names,[])

    def test_all_inputs_required_and_windows_refused(self):
        with mock.patch.object(build,'WINDOWS',False):
            self.assertIsNone(build.platform_selection(None,None,None))
            for values in [(self.root,None,'a'*64),(None,self.root,'a'*64),
                           (self.root,self.root,None),(self.root,self.root,'bad')]:
                with self.assertRaises(ValueError): build.platform_selection(*values)
        with mock.patch.object(build,'WINDOWS',True),self.assertRaises(ValueError):
            build.platform_selection(self.root,self.root,'a'*64)

    def test_graph_audit_failure_cannot_leave_success_evidence(self):
        self.configure(self.selection)
        original=self.run_stub
        def fail(*a,**k):
            if a[3]=='audit-static-platform': raise RuntimeError('uncaptured library')
            return original(*a,**k)
        self.run_stub=fail
        with self.assertRaisesRegex(RuntimeError,'uncaptured library'): self.configure(self.selection)
        self.assertFalse((self.logs/'platform-graph-receipt.json').exists())

if __name__=='__main__': unittest.main()
