"""Pinned Skia regression, one-step recipe migration, and failed-edge preservation.

The native fixture compiles the real helper with an Ozone stub, NOT Chromium.
CEF_PINNED_SKIA enables exact full-file checks on both CI contract hosts.
"""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT/'vcpkg/static'))
import resume_checkpoint as resume
import linux_slice

spec = importlib.util.spec_from_file_location('skia_patches', ROOT/'vcpkg/ports/cef-static/patch_source.py')
patches = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patches)
build = linux_slice.build
LEDGER = json.loads((ROOT/'vcpkg/static/checkpoint-migrations.json').read_text())
PINNED = os.environ.get('CEF_PINNED_SKIA')
CXX = shutil.which('clang++') or shutil.which('g++')
FLAGS = ('ENABLE_VULKAN', 'SKIA_USE_DAWN', 'SUPPORTS_OZONE_X11', 'IS_OZONE')


def recipe(platform):
    paths = list((ROOT/'vcpkg/ports/cef-static').rglob('*')) + [
        ROOT/'vcpkg/static/ci.py', ROOT/'vcpkg/static/checkpoint.py',
        ROOT/'vcpkg/static/windows_slice.py', ROOT/('vcpkg/static/triplets/' +
        ('x64-linux.cmake' if platform == 'linux-x64' else 'x64-windows-static.cmake'))]
    if platform == 'linux-x64':
        paths += [ROOT/'vcpkg/static/linux_slice.py', ROOT/'vcpkg/static/linux_checkpoint.py']
    h = hashlib.sha256()
    for path in sorted(paths):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            data = path.read_bytes()
            if b'\0' not in data[:8000]:
                data = data.replace(b'\r\n', b'\n')
                if platform == 'windows-x64':
                    data = data.replace(b'\n', b'\r\n')
            h.update(path.relative_to(ROOT).as_posix().encode()+b'\0')
            h.update(data)
    return h.hexdigest()


class GuardTests(unittest.TestCase):
    def test_union_of_callers_not_unconditional_dawn(self):
        self.assertIn('(BUILDFLAG(SKIA_USE_DAWN) && BUILDFLAG(SUPPORTS_OZONE_X11))', patches.X11_NEW)
        self.assertEqual(patches.X11_OLD.split('// Returns', 1)[1].split('\n#endif  // BUILDFLAG(ENABLE_VULKAN)')[0],
                         patches.X11_NEW.split('// Returns', 1)[1].split('\n#endif  // Vulkan')[0])

    def test_pinned_source_rejects_unreviewed_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = root/patches.X11_FILE; p.parent.mkdir(parents=True)
            p.write_text(patches.X11_OLD)
            with self.assertRaisesRegex(RuntimeError, 'source/context mismatch'):
                patches.patch_skia_x11_fallback(root)
            self.assertEqual(p.read_text(), patches.X11_OLD)

    @unittest.skipUnless(PINNED, 'full pinned Chromium file supplied by CI')
    def test_full_pinned_source_patch_is_idempotent_and_minimal(self):
        data = Path(PINNED).read_bytes()
        self.assertEqual(hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest(), patches.X11_ORIGINAL_BLOB)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); p=root/patches.X11_FILE; p.parent.mkdir(parents=True);p.write_bytes(data)
            patches.patch_skia_x11_fallback(root)
            first=p.read_bytes();stamp=p.stat().st_mtime_ns
            self.assertEqual(first.decode(), data.decode().replace(patches.X11_OLD,patches.X11_NEW,1))
            patches.patch_skia_x11_fallback(root)
            self.assertEqual(first,p.read_bytes());self.assertEqual(stamp,p.stat().st_mtime_ns)

    @unittest.skipUnless(CXX, 'native C++ compiler required')
    def test_native_helper_all_16_flag_combinations(self):
        rows=[]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); cpp=root/'probe.cc'; exe=root/('probe.exe' if os.name=='nt' else 'probe')
            for flags in itertools.product((0,1), repeat=4):
                v,d,x,o=flags
                head='#define BUILDFLAG(x) (x)\n'+''.join(f'#define {key} {value}\n' for key,value in zip(FLAGS,flags))
                stub='''namespace ui {
struct Properties { bool skia_can_fall_back_to_x11 = false; };
class OzonePlatform { public:
 static OzonePlatform* GetInstance() { static OzonePlatform p; return &p; }
 Properties& GetPlatformProperties() { return props; }
 private: Properties props;
}; }
'''
                callers='''bool VulkanCaller() {
#if BUILDFLAG(ENABLE_VULKAN)
 return MayFallBackToSkiaOutputDeviceX11();
#else
 return false;
#endif
}
bool DawnCaller() {
#if BUILDFLAG(SKIA_USE_DAWN) && BUILDFLAG(SUPPORTS_OZONE_X11)
 return MayFallBackToSkiaOutputDeviceX11();
#else
 return false;
#endif
}
int main() {
 if (VulkanCaller() || DawnCaller()) return 1;
 ui::OzonePlatform::GetInstance()->GetPlatformProperties().skia_can_fall_back_to_x11 = true;
 return VulkanCaller() != bool(ENABLE_VULKAN && IS_OZONE) ||
        DawnCaller() != bool(SKIA_USE_DAWN && SUPPORTS_OZONE_X11 && IS_OZONE);
}
'''
                cmd=[CXX,'-std=c++17','-Wall','-Wextra','-Werror',str(cpp),'-o',str(exe)]
                cpp.write_text(head+stub+'namespace {\n'+patches.X11_OLD+'}\n'+callers)
                before=subprocess.run(cmd, capture_output=True,text=True)
                self.assertEqual(before.returncode != 0, bool(not v and d and x), before.stderr)
                cpp.write_text(head+stub+'namespace {\n'+patches.X11_NEW+'}\n'+callers)
                after=subprocess.run(cmd,capture_output=True,text=True)
                self.assertEqual(after.returncode,0,after.stderr)
                subprocess.run([exe],check=True,timeout=10)
                rows.append({'flags':dict(zip(FLAGS,flags)),'original_failed':before.returncode!=0,'patched_compile_and_run':True})
        proof=ROOT/'static-diagnostics/skia-x11-regression.json';proof.parent.mkdir(exist_ok=True)
        proof.write_text(json.dumps({'fixture_not_cef':True,'compiler':CXX,'matrix':rows,'full_pinned_file_supplied':bool(PINNED)},indent=2)+'\n')

    @unittest.skipUnless(PINNED and CXX, 'full pinned file and preprocessor supplied by CI')
    def test_full_file_preprocessor_definition_and_call_sites(self):
        original=Path(PINNED).read_text()
        modified=original.replace(patches.X11_OLD,patches.X11_NEW,1)
        for flags in itertools.product((0,1),repeat=4):
            v,d,x,o=flags
            header='#define BUILDFLAG(x) (x)\n'+''.join(f'#define {k} {n}\n' for k,n in zip(FLAGS,flags))
            for text,fixed in [(original,False),(modified,True)]:
                no_includes=re.sub(r'^\s*#include[^\n]*', '',text,flags=re.M)
                result=subprocess.run([CXX,'-E','-P','-x','c++','-'],input=header+no_includes,capture_output=True,text=True,check=True)
                definitions=int(bool(v or (fixed and d and x)))
                self.assertEqual(result.stdout.count('MayFallBackToSkiaOutputDeviceX11()'),definitions+v+int(bool(d and x)))


class MigrationTests(unittest.TestCase):
    def test_current_recipes_equal_ledger_destinations(self):
        for m in LEDGER['migrations']:
            self.assertEqual(recipe(m['platform']),m['to_recipe'])

    def test_only_exact_producer_recipe_sha_attempt_migrates(self):
        for m in LEDGER['migrations']:
            identity={'platform':m['platform'],'recipe':m['to_recipe'],'work':'unchanged','image':'unchanged','schema':3}
            run={'id':m['producer_run'],'head_sha':m['producer_sha'],'run_attempt':m['producer_attempt']}
            adapted,selected=resume.checkpoint_input_identity(identity,run)
            self.assertEqual(selected,m)
            self.assertEqual(adapted,dict(identity,recipe=m['from_recipe']))
            for field,value in [('id',1),('head_sha','0'*40),('run_attempt',2)]:
                bad=dict(run,**{field:value}); self.assertEqual(resume.checkpoint_input_identity(identity,bad),(identity,None))
            bad=dict(identity,recipe='f'*64); self.assertEqual(resume.checkpoint_input_identity(bad,run),(bad,None))

    def test_source_recipe_pairs_match_current_patch_and_smoke(self):
        a=(ROOT/'vcpkg/ports/cef-static/patch_source.py').read_bytes().replace(b'\r\n',b'\n')
        b=(ROOT/'vcpkg/ports/cef-static/smoke.c').read_bytes().replace(b'\r\n',b'\n')
        values={hashlib.sha256(a+b).hexdigest(),hashlib.sha256(a.replace(b'\n',b'\r\n')+b.replace(b'\n',b'\r\n')).hexdigest()}
        self.assertEqual(set(build.X11_RECIPE_UPGRADES.values()),values)

    @unittest.skipUnless(PINNED, 'full pinned Chromium file supplied by CI')
    def test_real_source_recipe_migration_keeps_other_objects(self):
        a=(ROOT/'vcpkg/ports/cef-static/patch_source.py').read_bytes()
        b=(ROOT/'vcpkg/ports/cef-static/smoke.c').read_bytes()
        target=hashlib.sha256(a+b).hexdigest()
        previous=next(k for k,v in build.X11_RECIPE_UPGRADES.items() if v==target)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'src';logs=root/'logs';logs.mkdir()
            p=source/patches.X11_FILE;p.parent.mkdir(parents=True);p.write_bytes(Path(PINNED).read_bytes())
            marker=source/'cef-static-patched.json';marker.write_text(json.dumps({'recipe':previous}))
            obj=source/'unrelated.o';obj.write_bytes(b'unchanged fixture');stamp=obj.stat().st_mtime_ns
            build.upgrade_x11_recipe(source,logs,previous,target)
            self.assertEqual(json.loads(marker.read_text())['recipe'],target)
            self.assertIn(patches.X11_NEW,p.read_text())
            self.assertEqual(obj.read_bytes(),b'unchanged fixture');self.assertEqual(obj.stat().st_mtime_ns,stamp)

    def test_unknown_source_recipe_cannot_rewrite_marker(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(build,'run') as run:
            root=Path(tmp);marker=root/'cef-static-patched.json';marker.write_text('original')
            with self.assertRaisesRegex(RuntimeError,'no reviewed migration'):
                build.upgrade_x11_recipe(root,root,'0'*64,'f'*64)
            run.assert_not_called();self.assertEqual(marker.read_text(),'original')

    def test_marker_updated_only_after_successful_patch(self):
        before,after=next(iter(build.X11_RECIPE_UPGRADES.items()))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);marker=root/'cef-static-patched.json';marker.write_text('original')
            with patch.object(build,'run',side_effect=RuntimeError('failed')):
                with self.assertRaises(RuntimeError):build.upgrade_x11_recipe(root,root,before,after)
            self.assertEqual(marker.read_text(),'original')
            with patch.object(build,'run',return_value=''):
                build.upgrade_x11_recipe(root,root,before,after)
            self.assertEqual(json.loads(marker.read_text())['recipe'],after)

    def test_split_platform_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'vcpkg/static/iteration-request.json';p.parent.mkdir(parents=True)
            request={'schema':1,'sequence':1,'mode':'resume','checkpoint_run':'',
                     'checkpoint_runs':{'windows':'100','linux':'90'}}
            p.write_text(json.dumps(request))
            with patch.object(resume,'ROOT',root),patch.dict(os.environ,GITHUB_EVENT_NAME='push'):
                mode,run,fresh=resume.push_request('resume','')
                self.assertEqual(run,'100' if os.name=='nt' else '90')
                self.assertEqual(mode,'resume');self.assertFalse(fresh)
                self.assertEqual(resume.push_request('resume','120')[1],'120')
                for updates in [{'checkpoint_run':'80'}, {'mode':'fresh'},
                        {'checkpoint_runs':{'linux':'90'}}, {'checkpoint_runs':{'windows':'../1','linux':'90'}}]:
                    p.write_text(json.dumps(dict(request,**updates)))
                    with self.assertRaises(ValueError):resume.push_request('resume','')


@unittest.skipUnless(sys.platform=='linux','Linux failure checkpoint policy')
class FailedCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.out=self.root/'out';self.out.mkdir()
        self.diag=self.root/'diag';self.diag.mkdir()
        self.status={'status':'failed','unsafe_stop':False,'timed_out':False,'exit_code':1}
        self.write()
    def tearDown(self):self.tmp.cleanup()
    def write(self,failed='obj/bad.o'):
        (self.diag/'ninja-slice.json').write_text(json.dumps(self.status))
        (self.diag/'ninja-slice.log').write_text('FAILED: '+failed+' \nbad.cc:1: error: invalid\nninja: build stopped: subcommand failed.\n')
    def test_delete_failed_object_only(self):
        (self.out/'obj').mkdir();(self.out/'obj/bad.o').write_text('partial');(self.out/'obj/good.o').write_text('good')
        result=linux_slice.clean_failed_objects(self.out,self.diag)
        self.assertFalse((self.out/'obj/bad.o').exists());self.assertEqual((self.out/'obj/good.o').read_text(),'good')
        self.assertEqual(result['status'],'compile-failed-checkpoint');self.assertFalse(result['engine_runtime_verified'])
    def test_reject_unsafe_signalled_and_timed_stops(self):
        for field,value in [('unsafe_stop',True),('timed_out',True),('exit_code',-9),('status','running')]:
            with self.subTest(field=field):
                status=copy.deepcopy(self.status);self.status[field]=value;self.write()
                with self.assertRaises(RuntimeError):linux_slice.clean_failed_objects(self.out,self.diag)
                self.status=status
    def test_reject_linker_multi_output_and_traversal(self):
        for name in ['cef_static_smoke','obj/a.o obj/b.o','obj/../bad.o','obj//bad.o','/tmp/bad.o','obj/.hidden/../bad.o']:
            self.write(name)
            with self.subTest(name=name),self.assertRaises(RuntimeError):linux_slice.clean_failed_objects(self.out,self.diag)
    def test_reject_symlink(self):
        (self.out/'obj').mkdir();(self.out/'obj/bad.o').symlink_to(self.root/'outside')
        with self.assertRaises(RuntimeError):linux_slice.clean_failed_objects(self.out,self.diag)
    @unittest.skipUnless(CXX and shutil.which('ninja'), 'native compiler and Ninja required')
    def test_real_ninja_failure_checkpoint_and_incremental_repair(self):
        import linux_checkpoint as cp
        work=self.root/'native';work.mkdir();out=work/'out';out.mkdir()
        (work/'good.cc').write_text('int good() {return 7;}\n')
        (work/'bad.cc').write_text('this is not valid C++\n')
        (work/'main.cc').write_text('int good(); int bad(); int main(){return good()+bad()!=10;}\n')
        (out/'build.ninja').write_text(
            'rule compile\n  command = "'+CXX+'" -MMD -MF $out.d -c $in -o $out\n'
            '  depfile = $out.d\n  deps = gcc\n'
            'rule link\n  command = "'+CXX+'" $in -o $out\n'
            'build obj/good.o: compile ../good.cc\n'
            'build obj/bad.o: compile ../bad.cc\n'
            'build obj/main.o: compile ../main.cc\n'
            'build probe: link obj/good.o obj/bad.o obj/main.o\n')
        ninja=shutil.which('ninja')
        subprocess.run([ninja,'-C',out,'obj/good.o'],check=True,capture_output=True)
        good=out/'obj/good.o';before=good.read_bytes();stamp=good.stat().st_mtime_ns
        with self.assertRaises(RuntimeError):
            linux_slice.run_ninja([ninja,'-C',out,'-j','1','probe'],work,self.diag/'ninja-slice.log',30)
        # Model a compiler that left a partial failed output. It must not survive.
        (out/'obj/bad.o').write_bytes(b'partial object')
        result=linux_slice.clean_failed_objects(out,self.diag)
        self.assertEqual(result['status'],'compile-failed-checkpoint')
        identity={'platform':'linux-x64','recipe':'native-failure-test','schema':3}
        package=self.root/'package';cp.save(work,package,identity,limit=4096)
        shutil.rmtree(work);cp.restore(package,work,identity)
        self.assertFalse((out/'obj/bad.o').exists())
        self.assertEqual(good.read_bytes(),before);self.assertEqual(good.stat().st_mtime_ns,stamp)
        (work/'bad.cc').write_text('int bad() {return 3;}\n')
        subprocess.run([ninja,'-C',out,'probe'],check=True,capture_output=True)
        subprocess.run([out/'probe'],check=True,timeout=10)
        self.assertEqual(good.read_bytes(),before);self.assertEqual(good.stat().st_mtime_ns,stamp)

    def test_upload_does_not_require_a_successful_compile(self):
        workflow=(ROOT/'.github/workflows/static-engine-build.yml').read_text()
        self.assertIn("!cancelled() && steps.linux_iteration.outputs.checkpoint_ready == 'true'",workflow)
        self.assertIn("if: success() && steps.package.outcome == 'success'",workflow)


if __name__=='__main__':unittest.main()
