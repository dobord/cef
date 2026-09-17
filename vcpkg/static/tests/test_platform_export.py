"""Real small native archives through the SDK exporter; NOT a CEF runtime test."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

STATIC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STATIC))
import gn_platform
import platform_contract as contract
import platform_export
spec = importlib.util.spec_from_file_location('platform_exporter_under_test',
                                            STATIC.parent/'ports/cef-static/export_static.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


@unittest.skipUnless(sys.platform == 'linux' and all(shutil.which(t) for t in
                    ('cc', 'ar', 'ninja', 'cmake', 'pkg-config')), 'native Linux fixture tools')
class PlatformExportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='cef export ')
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.source = self.base/'source'
        self.out = self.source/'out'
        self.prefix = self.base/'target-prefix'
        self.logs = self.base/'logs'
        self.sdk = self.base/'package'
        for p in (self.out, self.logs, self.prefix/'lib/pkgconfig', self.prefix/'include',
                  self.source/'third_party/ninja', self.source/'cef/include/base/internal',
                  self.source/'cef/include/capi', self.source/'net/base'):
            p.mkdir(parents=True, exist_ok=True)
        shutil.copy2(shutil.which('ninja'), self.source/'third_party/ninja/ninja')
        for name, text in {
            'engine.c': 'int platform_value(void); int engine_value(void){return platform_value();}',
            'engine2.c': 'int engine_extra(void){return 40;}',
            'platform.c': 'int engine_extra(void); int platform_value(void){return engine_extra()+2;}',
            'smoke.c': 'int engine_value(void); int main(void){return engine_value()!=42;}',
        }.items():
            (self.out/name).write_text(text)
        for name in ('engine','engine2','platform'):
            self.run_cmd(['cc','-c',name+'.c','-o',name+'.o'], self.out)
        self.run_cmd(['ar','rcs',self.prefix/'lib/libplatform.a','platform.o'], self.out)
        self.run_cmd(['ar','rcs','libengine.a','engine.o','engine2.o'], self.out)
        (self.prefix/'include/platform.h').write_text('int platform_value(void);\n')
        (self.prefix/'lib/pkgconfig/platform.pc').write_text(
            'prefix=${pcfiledir}/../..\nlibdir=${prefix}/lib\nincludedir=${prefix}/include\n'
            'Name: platform\nDescription: fixture\nVersion: 1.0\n'
            'Libs: -L${libdir} -lplatform -pthread\nCflags: -I${includedir}\n')
        value = contract.capture(self.prefix, Path(shutil.which('pkg-config')), ['platform'])
        # Complete synthetic catalog, explicitly confined to this fixture.
        value['modules'] = {n: copy.deepcopy(value['modules']['platform']) for n in gn_platform.MODULES}
        self.manifest = self.base/'platform.json'
        self.manifest.write_bytes(contract.canonical(value))
        self.selection = {'manifest':str(self.manifest), 'prefix':str(self.prefix),
                          'sha256':contract.digest(self.manifest)}
        self.graph = {'libs':[str(self.prefix/'lib/libplatform.a'), 'm'], 'ldflags':['-pthread']}
        # Native Ninja query/link are real; the CEF runtime fields below are
        # synthetic unit-test inputs, never generated qualification evidence.
        archive = str(self.prefix/'lib/libplatform.a').replace(' ', '$ ')
        (self.out/'build.ninja').write_text(
            'rule cc\n  command = cc -c $in -o $out\n'
            'rule link\n  command = cc -o $out -Wl,--start-group $in -Wl,--end-group\n'
            'build cef_static_smoke.smoke.o: cc smoke.c\n'
            f'build cef_static_smoke: link cef_static_smoke.smoke.o libengine.a {archive}\n')
        self.run_cmd(['ninja','cef_static_smoke'], self.out)
        self.run_cmd([self.out/'cef_static_smoke'], self.out)
        proof = gn_platform.audit_graph(self.graph, self.source, self.out, self.prefix, value)
        proof['manifest_sha256'] = self.selection['sha256']
        self.receipt = {'cef_commit':exporter.CEF_COMMIT, 'chromium_commit':exporter.CHROMIUM_COMMIT,
            'source_build_verified':True, 'engine_linkage':'static',
            'executable_sha256':exporter.sha256(self.out/'cef_static_smoke'),
            'smoke':{'cef':exporter.CEF_VERSION,'engine':'static','javascript':True,'paint':True,
                     'browser_modules_clean':True,'renderer_modules_clean':True,
                     'browser_pid':1,'renderer_pid':2},
            'platform_build_inputs':self.selection, 'platform_graph':proof}
        for n in ('cef_version.h','cef_config.h','cef_api_versions.h','capi/cef_app_capi.h'):
            (self.source/'cef/include'/n).write_text('/* synthetic public header */\n')
        for n in ('LICENSE','cef/LICENSE.txt','net/base/net_error_list.h'):
            (self.source/n).write_text('/* synthetic fixture */\n')
        self.write_logs()

    def run_cmd(self, command, cwd, *, success=True):
        proc = subprocess.run(list(map(str,command)), cwd=cwd, capture_output=True, text=True, timeout=60)
        if success:
            self.assertEqual(proc.returncode, 0, proc.stdout+proc.stderr)
        else:
            self.assertNotEqual(proc.returncode, 0, proc.stdout+proc.stderr)
        return proc.stdout+proc.stderr

    def write_logs(self):
        (self.logs/'engine-build-receipt.json').write_text(json.dumps(self.receipt))
        (self.logs/'gn-graph.json').write_text(json.dumps({'//cef:cef_static_smoke':self.graph}))

    def export(self, selection=True):
        exporter.export(self.source, self.out, self.logs, self.sdk,
                        platform_inputs=self.selection if selection else None)

    def consumer(self, prefix, build, *, success=True):
        project = self.base/'consumer'
        project.mkdir(exist_ok=True)
        (project/'main.c').write_text('int engine_value(void); int main(void){return engine_value()!=42;}\n')
        (project/'CMakeLists.txt').write_text(
            'cmake_minimum_required(VERSION 3.24)\nproject(fixture C)\n'
            'find_package(cef-static CONFIG REQUIRED)\n'
            'add_executable(consumer main.c)\ntarget_link_libraries(consumer PRIVATE CEF::static)\n')
        output = self.run_cmd(['cmake','-S',project,'-B',build,'-G','Ninja',
            '-Dcef-static_DIR='+str(prefix/'share/cef-static'),
            '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF','-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF'],
            self.base, success=success)
        if success:
            self.run_cmd(['cmake','--build',build], self.base)
            self.run_cmd([build/'consumer'], self.base)
        return output

    def test_export_owns_only_engine_and_relocated_consumer_resolves_cycle(self):
        before = (self.prefix/'lib/libplatform.a').read_bytes()
        self.export()
        self.assertFalse((self.sdk/'lib/libplatform.a').exists())
        self.assertEqual(before, (self.prefix/'lib/libplatform.a').read_bytes())
        metadata = json.loads((self.sdk/'share/cef-static/static-link-inventory.json').read_text())
        self.assertEqual(metadata['archives'], 1)
        self.assertEqual(metadata['platform']['archives'][0]['path'],'lib/libplatform.a')
        self.assertFalse(metadata['platform']['runtime_verified'])
        self.assertEqual(metadata['platform']['manifest_sha256'], self.selection['sha256'])
        self.assertEqual((self.sdk/'share/cef-static/platform-build-inputs.json').read_bytes(),
                         self.manifest.read_bytes())
        text = (self.sdk/'share/cef-static/cef-static-config.cmake').read_text()
        self.assertNotIn(str(self.base), text)
        self.assertIn('$<LINK_GROUP:RESCAN,CEF::archive_0,CEF::platform_archive_0>', text)
        relocated = self.base/'relocated'
        shutil.copytree(self.sdk, relocated)
        shutil.copy2(self.prefix/'lib/libplatform.a', relocated/'lib/libplatform.a')
        self.source.rename(self.base/'source.hidden')
        self.prefix.rename(self.base/'target-prefix.hidden')
        self.sdk.rename(self.base/'package.hidden')
        self.consumer(relocated, self.base/'build-consumer')
        # Actual archives, not extension names, must match after relocation.
        with (relocated/'lib/libplatform.a').open('ab') as stream: stream.write(b'changed')
        self.assertIn('dependency changed',self.consumer(relocated,self.base/'tampered-build',success=False))

    def test_missing_dependency_fails_before_link(self):
        self.export()
        self.assertIn('Missing static CEF dependency',
                      self.consumer(self.sdk,self.base/'missing-build',success=False))

    def test_no_implicit_engine_only_fallback(self):
        with self.assertRaisesRegex(ValueError,'explicit platform export'):
            self.export(selection=False)
        self.assertFalse(self.sdk.exists())

    def test_engine_receipt_cannot_be_rebranded(self):
        del self.receipt['platform_build_inputs']; self.write_logs()
        with self.assertRaisesRegex(ValueError,'not verified'):
            self.export()
        self.assertFalse(self.sdk.exists())

    def test_changed_receipt_graph_rejected(self):
        self.receipt['platform_graph']['archives'] = []; self.write_logs()
        with self.assertRaisesRegex(ValueError,'differs from export graph'):
            self.export()
        self.assertFalse(self.sdk.exists())

    def test_changed_header_rejected_before_output(self):
        (self.prefix/'include/platform.h').write_text('changed')
        with self.assertRaisesRegex(ValueError,'Dependency size changed'):
            self.export()
        self.assertFalse(self.sdk.exists())

    def test_graph_cannot_hide_system_library(self):
        self.graph['libs'].append('glib-2.0'); self.write_logs()
        with self.assertRaisesRegex(ValueError,'Unresolved non-OS'):
            self.export()

    def test_extra_ninja_archive_is_not_copied_as_engine(self):
        extra = self.prefix/'lib/libextra.a'
        shutil.copy2(self.prefix/'lib/libplatform.a',extra)
        p = self.out/'build.ninja'
        with p.open('a') as stream:
            stream.write('build unused: phony\n')
        text = p.read_text().replace(' libengine.a ', ' libengine.a '+str(extra).replace(' ','$ ')+' ')
        p.write_text(text)
        with self.assertRaisesRegex(ValueError,'Uncaptured or unused'):
            self.export()

    def test_dependency_drift_during_export_rejected(self):
        prepared = platform_export.prepare(self.selection,self.receipt,self.graph,self.source,self.out)
        prepared.owns(self.prefix/'lib/libplatform.a')
        (self.prefix/'include/platform.h').write_text('changed')
        with self.assertRaises(ValueError): prepared.finish()

    def test_external_objects_cannot_replace_archives(self):
        prepared = platform_export.prepare(self.selection,self.receipt,self.graph,self.source,self.out)
        shutil.copy2(self.out/'engine.o',self.prefix/'lib/engine.o')
        with self.assertRaisesRegex(ValueError,'Uncaptured or unused'):
            prepared.owns(self.prefix/'lib/engine.o')

if __name__ == '__main__': unittest.main()
