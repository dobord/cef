"""Native startup regressions: small real executables, not a CEF SDK certificate."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('runtime_startup_fix', ROOT/'vcpkg/ports/cef-static/runtime_startup.py')
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)


class AssetSourceTests(unittest.TestCase):
    def setUp(self):
        self.random_text = '// isolated file fixture\n'+fix.RAND_OLD+'\n'
        self.patcher = patch.object(fix, 'RAND_BLOB', fix.blob(self.random_text))
        self.patcher.start(); self.addCleanup(self.patcher.stop)

    def add_random(self, root):
        target = root/fix.RAND_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.random_text)

    def original(self):
        return (ROOT/'libcef/common/resource_util.cc').read_text(encoding='utf-8')

    def test_exact_original_file_minimal_and_idempotent(self):
        original = self.original()
        self.assertEqual(fix.blob(original), fix.ASSET_BLOB)
        changed, pin = fix.transform_asset(original)
        self.assertEqual(changed, original.replace(fix.ASSET_INCLUDE_OLD, fix.ASSET_INCLUDE_NEW, 1)
                                         .replace(fix.ASSET_OLD, fix.ASSET_NEW, 1))
        self.assertEqual(fix.transform_asset(changed), (changed, pin))
        self.assertIn('#else\n'+fix.ASSET_OLD.split('\n',1)[1][:-1]+'#endif', fix.ASSET_NEW)

    def test_drift_partial_and_duplicate_states_rejected(self):
        original = self.original(); changed, _ = fix.transform_asset(original)
        cases = [original+'// drift\n', changed+'// drift\n', fix.ASSET_OLD,
                 original.replace(fix.ASSET_OLD, fix.ASSET_NEW, 1),
                 original.replace(fix.ASSET_INCLUDE_OLD, fix.ASSET_INCLUDE_NEW, 1),
                 original+'\n'+fix.ASSET_OLD]
        for text in cases:
            with self.subTest(text=text[-50:]), self.assertRaises(RuntimeError):
                fix.transform_asset(text)

    def test_reapplication_preserves_clocks_and_other_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); target=root/fix.ASSET_FILE
            target.parent.mkdir(parents=True); target.write_text(self.original())
            sentinel=root/'keep.o';sentinel.write_bytes(b'compiled fixture')
            clock=sentinel.stat().st_mtime_ns
            receipt=root/'logs/repair.json'
            self.add_random(root)
            fix.apply(root,receipt); changed_clock=target.stat().st_mtime_ns
            fix.apply(root,receipt)
            self.assertEqual(target.stat().st_mtime_ns,changed_clock)
            self.assertEqual(sentinel.stat().st_mtime_ns,clock)
            self.assertEqual(sentinel.read_bytes(),b'compiled fixture')
            result=json.loads(receipt.read_text())
            self.assertFalse(result['engine_runtime_verified'])
            self.assertEqual(result['edits'][0]['status'],'already-applied')

    def test_unknown_source_removes_old_receipt_without_rewriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);target=root/fix.ASSET_FILE
            target.parent.mkdir(parents=True);target.write_text('unknown')
            receipt=root/'proof.json';receipt.write_text('stale')
            with self.assertRaises(RuntimeError):fix.apply(root,receipt)
            self.assertEqual(target.read_text(),'unknown'); self.assertFalse(receipt.exists())


@unittest.skipUnless(sys.platform == 'linux', 'native Linux proc/self/exe behavior')
class NativeAssetTests(unittest.TestCase):
    def test_proc_self_exe_and_foreign_cwd_with_exact_function_body(self):
        compiler=shutil.which('clang++') or shutil.which('g++')
        self.assertIsNotNone(compiler,'Native C++ compiler is mandatory')
        fixture=r'''#include <dlfcn.h>
#include <unistd.h>
#include <limits.h>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#define CHECK(c) do { if (!(c)) std::abort(); } while (0)
namespace base {
enum {DIR_EXE,DIR_ASSETS};
class FilePath {
 public:
  std::filesystem::path path;
  FilePath()=default;
  explicit FilePath(const char* v):path(v){}
  FilePath DirName()const {FilePath f;f.path=path.parent_path();return f;}
};
static FilePath assets;
struct PathService {
 static bool Get(int key,FilePath* value) {
   if(key!=DIR_EXE)return false;
   char path[PATH_MAX];auto count=readlink("/proc/self/exe",path,sizeof(path)-1);
   if(count<0)return false;
   path[count]=0;*value=FilePath(path).DirName();return true;
 }
 static bool Override(int key,const FilePath& value){if(key!=DIR_ASSETS)return false;assets=value;return true;}
};
}
FUNCTION
int main(int argc,char**) {
 if(argc==1) {execl("/proc/self/exe","/proc/self/exe","child",(char*)nullptr);return 2;}
 OverrideAssetPath();
 auto asset=base::assets.path/"icudtl.dat";
 std::printf("asset=%s\n",asset.c_str());
 std::ifstream stream(asset);std::string text;stream>>text;
 return text=="fixture-marker"?0:9;
}
'''
        with tempfile.TemporaryDirectory(prefix='cef assets with spaces ') as tmp:
            root=Path(tmp);deploy=root/'relocated';deploy.mkdir();cwd=root/'foreign';cwd.mkdir()
            (deploy/'icudtl.dat').write_text('fixture-marker')
            for name,body,defines,expected in [('original',fix.ASSET_OLD,['-DCEF_STATIC'],9),
                    ('static-fixed',fix.ASSET_NEW,['-DCEF_STATIC'],0),
                    ('shared-behavior',fix.ASSET_NEW,[],9)]:
                source=root/(name+'.cc');source.write_text(fixture.replace('FUNCTION',body))
                exe=deploy/name
                result=subprocess.run([compiler,'-std=c++17','-Wall','-Wextra','-Werror','-fPIE','-pie',
                    *defines,str(source),'-ldl','-o',str(exe)],capture_output=True,text=True,timeout=60)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                result=subprocess.run([exe],cwd=cwd,capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
                if expected: self.assertIn('/proc/self/icudtl.dat',result.stdout)
                else:self.assertIn(str(deploy/'icudtl.dat'),result.stdout)
            (deploy/'icudtl.dat').unlink()
            result=subprocess.run([deploy/'static-fixed'],cwd=cwd,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,9,'Missing data must not be reported as success')



PINNED = os.environ.get('CEF_PINNED_RUNTIME')


class RandomSourceTests(unittest.TestCase):
    def test_exact_loop_keeps_zero_retry_and_only_changes_seed_provider(self):
        self.assertIn('while (a_ == 0 && b_ == 0)',fix.RAND_NEW)
        self.assertIn('NonAllocatingRandomBitGenerator seed;',fix.RAND_NEW)
        self.assertNotIn('ReseedForTesting',fix.RAND_NEW)
        self.assertNotIn('DCHECK',fix.RAND_NEW)
        self.assertNotIn('disable',fix.RAND_NEW)

    def test_drift_duplicate_and_reapplication(self):
        original='// complete test fixture\n'+fix.RAND_OLD
        with patch.object(fix,'RAND_BLOB',fix.blob(original)):
            changed,pin=fix.transform_rand(original)
            self.assertEqual(fix.transform_rand(changed),(changed,pin))
            for text in [original+'// drift',changed+'// drift',original*2,changed*2]:
                with self.subTest(text=text[-20:]),self.assertRaises(RuntimeError):fix.transform_rand(text)

    def test_all_inputs_verified_before_any_source_is_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);asset=root/fix.ASSET_FILE;asset.parent.mkdir(parents=True)
            original=(ROOT/'libcef/common/resource_util.cc').read_text();asset.write_text(original)
            random=root/fix.RAND_FILE;random.parent.mkdir(parents=True);random.write_text('unreviewed')
            receipt=root/'proof.json';receipt.write_text('stale')
            with self.assertRaises(RuntimeError):fix.apply(root,receipt)
            self.assertEqual(asset.read_text(),original)
            self.assertFalse(receipt.exists())

    def test_redirected_source_rejected_without_replacing_target(self):
        if os.name=='nt':self.skipTest('POSIX symlink fixture; Windows archive guards tested separately')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);src=root/'src';asset=src/fix.ASSET_FILE;asset.parent.mkdir(parents=True)
            outside=root/'outside';outside.write_text('untouched');asset.symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError,'Redirected'):fix.apply(src,root/'proof.json')
            self.assertEqual(outside.read_text(),'untouched')

    @unittest.skipUnless(PINNED,'full pinned Chromium/CEF-patched RNG supplied in CI')
    def test_full_pinned_random_file_only_two_seed_assignments_changed(self):
        original=(Path(PINNED)/'base/rand_util.cc').read_text(encoding='utf-8')
        self.assertEqual(fix.blob(original),fix.RAND_BLOB)
        changed,pin=fix.transform_rand(original)
        self.assertEqual(changed,original.replace(fix.RAND_OLD,fix.RAND_NEW,1))
        self.assertEqual(fix.transform_rand(changed),(changed,pin))
        self.assertIn('InsecureRandomGenerator::InsecureRandomGenerator() = default;',changed)
        self.assertIn('CHECK_NE(seed, 0u);',changed)
        header=(Path(PINNED)/'base/rand_util.h').read_text()
        self.assertIn('class NonAllocatingRandomBitGenerator',header)
        self.assertIn('RAND_get_system_entropy_for_custom_prng',header)


class NativeRandomTests(unittest.TestCase):
    def test_allocator_critical_first_sample_and_reseed_preserve_xorshift(self):
        compiler=shutil.which('clang++') or shutil.which('g++')
        self.assertIsNotNone(compiler,'Native C++ compiler is mandatory')
        # The exact generator body is checked against the full file in CI; the
        # two entropy backends below deliberately model allocating/nonallocating
        # providers. This is a reentrancy regression, NOT a BoringSSL RNG test.
        tail='''  uint64_t t = a_;
  const uint64_t s = b_;
  a_ = s;
  t ^= t << 23;
  t ^= t >> 17;
  t ^= s ^ (s >> 26);
  b_ = t;
  return t + s;
'''
        prefix='''uint64_t InsecureRandomGenerator::RandUint64() const {
  if (a_ == 0 && b_ == 0) [[unlikely]] {
'''
        header='''class NonAllocatingRandomBitGenerator {
 public:
  using result_type = uint64_t;
  static constexpr result_type min() { return 0; }
  static constexpr result_type max() { return UINT64_MAX; }
  result_type operator()() const {
    uint64_t result;
    RAND_get_system_entropy_for_custom_prng(reinterpret_cast<uint8_t*>(&result),
                                            sizeof(result));
    return result;
  }
  NonAllocatingRandomBitGenerator() = default;
  ~NonAllocatingRandomBitGenerator() = default;
};'''
        if PINNED:
            original=(Path(PINNED)/'base/rand_util.cc').read_text()
            start=original.index('uint64_t InsecureRandomGenerator::RandUint64() const {')
            end=original.index('\nuint32_t InsecureRandomGenerator::RandUint32()',start)
            actual=original[start:end].strip()
            self.assertIn(''.join(tail.split()),''.join(actual.split()))
            self.assertIn(fix.RAND_OLD,actual)
            full_header=(Path(PINNED)/'base/rand_util.h').read_text()
            self.assertEqual(''.join(header.split()), ''.join(full_header[full_header.index('class NonAllocatingRandomBitGenerator'):full_header.index('// Shuffles')].split()))
        else:
            actual=prefix+fix.RAND_OLD+'\n  }\n'+tail+'}\n'
        harness=r'''#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
static bool in_allocator=false;
static unsigned crypto_calls=0, entropy_calls=0;
static constexpr uint64_t seeds[]={0,0,0xfedcba9876543210ULL,0x0123456789abcdefULL};
void RAND_get_system_entropy_for_custom_prng(uint8_t* output,size_t size) {
 if(size!=sizeof(uint64_t))std::exit(80);
 auto value=seeds[entropy_calls++ % 4];std::memcpy(output,&value,size);
}
namespace base {
uint64_t RandUint64(){++crypto_calls;if(in_allocator)std::exit(73);return 42;}
NONALLOC
class InsecureRandomGenerator {
 public:
  uint64_t RandUint64() const;
  mutable uint64_t a_=0,b_=0;
};
GENERATOR
}
uint64_t reference(uint64_t& a,uint64_t& b){uint64_t t=a,s=b;a=s;t^=t<<23;t^=t>>17;t^=s^(s>>26);b=t;return t+s;}
int main(){
 for(int cycle=0;cycle<2;++cycle){
  base::InsecureRandomGenerator gen;
  auto initial_entropy=entropy_calls;
  if(entropy_calls!=static_cast<unsigned>(cycle*4))return 82;
  in_allocator=true;
  uint64_t a=seeds[2],b=seeds[3];
  for(int i=0;i<1000;++i){if(gen.RandUint64()!=reference(a,b))return 83;}
  in_allocator=false;
  if(entropy_calls!=initial_entropy+4||crypto_calls!=0)return 84;
 }
 if(base::RandUint64()!=42||crypto_calls!=1)return 85;
 puts("nonallocating-first-use-and-reseed; 2000 exact xorshift results; crypto unchanged");
 return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='cef random regression ') as tmp:
            root=Path(tmp)
            for name,body,expected in [('original',actual,73),('fixed',actual.replace(fix.RAND_OLD,fix.RAND_NEW,1),0)]:
                source=root/(name+'.cc');exe=root/(name+('.exe' if os.name=='nt' else ''))
                source.write_text(harness.replace('NONALLOC',header).replace('GENERATOR',body))
                result=subprocess.run([compiler,'-std=c++20','-Wall','-Wextra','-Werror',str(source),'-o',str(exe)],capture_output=True,text=True,timeout=90)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                result=subprocess.run([exe],capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
                if not expected:self.assertIn('2000 exact xorshift',result.stdout)

class DeploymentTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('runtime_deploy_build',ROOT/'vcpkg/ports/cef-static/source_build.py')
        self.build=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.build)

    def test_missing_or_empty_icu_fails_before_native_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'out';out.mkdir();deploy=root/'deploy';deploy.mkdir()
            logs=root/'logs';logs.mkdir()
            for empty in (False,True):
                if empty:(out/'icudtl.dat').write_bytes(b'')
                (logs/'runtime-data.json').write_text('stale')
                with self.assertRaisesRegex(RuntimeError,'Required ICU'):
                    self.build.stage_runtime_data(out,deploy,logs)
                self.assertFalse((logs/'runtime-data.json').exists())

    def test_copy_data_not_libraries_and_record_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'out';out.mkdir();deploy=root/'deploy';deploy.mkdir()
            logs=root/'logs';logs.mkdir()
            (out/'icudtl.dat').write_bytes(b'ICU test fixture, not real ICU')
            (out/'resources.pak').write_bytes(b'resource fixture')
            (out/'libcef.dll').write_bytes(b'shared forbidden')
            (out/'libcef.so').write_bytes(b'shared forbidden')
            (out/'locales').mkdir();(out/'locales/en-US.pak').write_bytes(b'locale fixture')
            self.build.stage_runtime_data(out,deploy,logs)
            proof=json.loads((logs/'runtime-data.json').read_text())
            self.assertFalse(proof['engine_runtime_verified'])
            self.assertEqual({r['path'] for r in proof['files']}, {'icudtl.dat','resources.pak','locales/en-US.pak'})
            for entry in proof['files']:
                self.assertEqual(entry['sha256'],self.build.digest(deploy/entry['path']))
            self.assertFalse((deploy/'libcef.dll').exists());self.assertFalse((deploy/'libcef.so').exists())

    def test_both_supplemental_patches_precede_gn_and_native_receipts(self):
        text=(ROOT/'vcpkg/ports/cef-static/source_build.py').read_text()
        config=text[text.index('def configuration('):text.index('def execute_smoke(')]
        self.assertLess(config.index("HERE/'runtime_startup.py'"),config.index('gn_generate_command'))
        self.assertIn("HERE/'skia_x11_link.py'",config)
        runtime=text[text.index('def compile_and_test('):]
        self.assertLess(runtime.index('stage_runtime_data('),runtime.index('execute_smoke('))
        self.assertLess(runtime.index('execute_smoke('),runtime.index("'engine-build-receipt.json').write_text"))


if __name__=='__main__':unittest.main()
