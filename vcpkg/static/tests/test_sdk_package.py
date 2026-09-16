"""Transport regression only: synthetic receipts never certify an actual CEF SDK."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('sdk_package_tested', ROOT/'vcpkg/static/sdk_package.py')
pkg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pkg)
COMMIT = 'a'*40


def synthetic_receipt(triplet='x64-windows-static'):
    count = 3 if triplet == 'x64-windows-static' else 1
    return {'schema': 1, 'integration_commit': COMMIT, 'triplet': triplet,
        'cef_commit': pkg.CEF, 'chromium_commit': pkg.CHROMIUM, 'vcpkg_commit': pkg.VCPKG,
        'configuration': 'Release', 'engine_linkage': 'static', 'capi_only': True,
        'sdk_relocation_verified': True, 'application_relocation_verified': True,
        'sandbox_verified': False, 'system_libraries_static': False,
        'executable_sha256': 'b'*64,
        'smoke': {'cef': pkg.VERSION, 'engine': 'static', 'interface': 'capi', 'javascript': True,
            'paint': True, 'browser_modules_clean': True, 'renderer_modules_clean': True,
            'browser_pid': 1, 'renderer_pid': 2},
        'smoke_runs': {'status': 'success', 'required_runs': count, 'passed_runs': count,
            'engine_runtime_verified': True, 'no_retry_on_failure': True,
            'executable_sha256': 'b'*64,
            'runs': [{'number': i, 'cwd': f'fixture-profile-{i}', 'status': 'success',
                      'engine_runtime_verified': True} for i in range(1, count+1)]}}


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cef package ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'SDK source'
        self.source.mkdir()
        (self.source/'static-sdk-receipt.json').write_bytes(pkg.json_bytes({'fixture_only': True}))
        (self.source/'library.bin').write_bytes(bytes(range(256))*80)
        (self.source/'empty').touch()
        self.output = self.root/'downloaded SDK'

    def bundle(self, limit=512, **kwargs):
        return pkg.package_sdk(self.source, self.output, 'fixture-sdk', part_bytes=limit, **kwargs)

    def mutate_manifest(self, path, change):
        data = pkg.read_json(path)
        change(data)
        path.write_bytes(pkg.json_bytes(data))

    def test_single_zip_and_multipart_roundtrip_preserve_every_file_and_mode(self):
        for limit in (65536, 256):
            with self.subTest(limit=limit):
                output = self.root/str(limit)
                manifest = pkg.package_sdk(self.source, output, 'fixture-sdk', part_bytes=limit)
                data = pkg.verify_bundle(manifest)['manifest']
                self.assertEqual(len(data['parts']) > 1, limit == 256)
                assembled = pkg.assemble(manifest)
                self.assertEqual(pkg.hash_file(assembled), data['archive']['sha256'])
                with zipfile.ZipFile(assembled) as z:
                    self.assertIsNone(z.testzip())
                    for original in self.source.iterdir():
                        self.assertEqual(z.read('fixture-sdk/'+original.name), original.read_bytes())
                        self.assertGreaterEqual(z.getinfo('fixture-sdk/'+original.name).extract_version, 45)
                self.assertTrue(all(p['size'] <= limit for p in data['parts']))

    def test_exact_part_boundaries_never_create_empty_trailing_part(self):
        for size in (4095, 4096, 4097, 8192):
            folder = self.root/str(size); folder.mkdir()
            with pkg.SplitWriter(folder, 'boundary', 4096) as writer:
                writer.write(b'x'*size)
                archive = writer.finish()
                self.assertEqual(len(writer.parts), (size+4095)//4096)
                self.assertEqual(sum(p['size'] for p in writer.parts), size)
                self.assertTrue(all(p['size'] > 0 for p in writer.parts))
                with pkg.PartsReader([folder/p['name'] for p in writer.parts]) as stream:
                    stream.seek(4090)
                    self.assertEqual(stream.read(30), (b'x'*size)[4090:4120])
                self.assertEqual(archive['sha256'], hashlib.sha256(b'x'*size).hexdigest())

    def test_missing_truncated_corrupt_reordered_or_duplicate_parts_fail(self):
        manifest = self.bundle()
        original = manifest.read_bytes()
        data = pkg.read_json(manifest)
        part = self.output/data['parts'][0]['name']; saved = part.read_bytes()
        for replacement in (None, saved[:-1], b'X'+saved[1:]):
            if replacement is None: part.unlink()
            else: part.write_bytes(replacement)
            with self.assertRaises(ValueError): pkg.verify_bundle(manifest)
            part.write_bytes(saved)
        for change in (lambda d: d['parts'].reverse(), lambda d: d['parts'].append(d['parts'][0]),
                       lambda d: d['archive'].update(sha256='0'*64)):
            self.mutate_manifest(manifest, change)
            with self.assertRaises(ValueError): pkg.verify_bundle(manifest)
            manifest.write_bytes(original)

    def test_unsafe_asset_paths_sizes_and_extra_files_fail(self):
        manifest = self.bundle(); original = manifest.read_bytes()
        for name in ('../escape', '/abs', 'C:\\escape', '@file', 'a/b', 'a;echo', '..'):
            self.mutate_manifest(manifest, lambda d: d['parts'][0].update(name=name))
            with self.assertRaises(ValueError): pkg.verify_bundle(manifest)
            manifest.write_bytes(original)
        self.mutate_manifest(manifest, lambda d: d.update(part_bytes=pkg.ASSET_LIMIT))
        with self.assertRaises(ValueError): pkg.verify_bundle(manifest)
        manifest.write_bytes(original)
        (self.output/'stale.zip').write_bytes(b'not this run')
        with self.assertRaises(ValueError): pkg.verify_bundle(manifest)

    def test_payload_and_internal_receipt_must_match_inventory(self):
        manifest = self.bundle(); original = manifest.read_bytes()
        self.mutate_manifest(manifest, lambda d: d['entries'][0].update(sha256='0'*64))
        with self.assertRaises(ValueError): pkg.verify_bundle(manifest)
        manifest.write_bytes(original)
        receipt = self.output/'fixture-sdk.json'; receipt.write_bytes(b'{"fixture_only":false}\n')
        self.mutate_manifest(manifest, lambda d: d.update(receipt=pkg.file_record(receipt)))
        with self.assertRaisesRegex(ValueError, 'Inner and outer'): pkg.verify_bundle(manifest)

    def test_special_paths_and_symlink_zip_metadata_rejected(self):
        for path in ('../file', '/abs', 'fixture-sdk/../file', 'fixture-sdk//file',
                     'fixture-sdk/a\\b', 'fixture-sdk/C:drive'):
            with self.assertRaises(ValueError): pkg.validate_entry(path, 'fixture-sdk')
        (self.source/'LIBRARY.bin').write_bytes(b'case collision')
        with self.assertRaises(ValueError): self.bundle()
        self.assertFalse(self.output.exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink creation; manifest rejection is tested on both')
    def test_symlinks_are_not_followed(self):
        (self.source/'link').symlink_to(self.source/'library.bin')
        with self.assertRaises(ValueError): self.bundle()
        (self.source/'link').unlink()
        manifest = self.bundle(); data = pkg.read_json(manifest)
        part = self.output/data['parts'][0]['name']; other = self.root/'original'; part.rename(other)
        part.symlink_to(other)
        with self.assertRaises(ValueError): pkg.verify_bundle(manifest)

    def test_failed_write_publishes_nothing_and_preserves_source(self):
        before = {p.name: p.read_bytes() for p in self.source.iterdir()}
        with mock.patch.object(pkg.SplitWriter, 'write', side_effect=OSError('simulated disk full')):
            with self.assertRaises(OSError): self.bundle()
        self.assertFalse(self.output.exists())
        self.assertEqual({p.name: p.read_bytes() for p in self.source.iterdir()}, before)
        self.assertFalse(list(self.root.glob('.cef-sdk-*')))

    def test_existing_outputs_never_overwritten(self):
        self.output.mkdir(); (self.output/'owned').write_text('keep')
        with self.assertRaises(ValueError): self.bundle()
        self.assertEqual((self.output/'owned').read_text(), 'keep')
        shutil.rmtree(self.output)
        manifest = self.bundle(); target = self.root/'existing.zip'; target.write_text('keep')
        with self.assertRaises(ValueError): pkg.assemble(manifest, target)
        self.assertEqual(target.read_text(), 'keep')

    def test_duplicate_json_and_boolean_sizes_rejected(self):
        manifest = self.bundle(); original = manifest.read_bytes()
        manifest.write_text('{"schema":1,"schema":1}')
        with self.assertRaises(ValueError): pkg.verify_bundle(manifest)
        manifest.write_bytes(original)
        self.mutate_manifest(manifest, lambda d: d['parts'][0].update(size=True))
        with self.assertRaises(ValueError): pkg.verify_bundle(manifest)

    def test_standalone_helper_in_isolated_python_assembles_correct_zip(self):
        manifest = self.bundle()
        helper = self.output/'fixture-sdk-assemble.py'
        subprocess.run([sys.executable, '-I', helper, 'verify', manifest], cwd=self.root, check=True,
                       capture_output=True, timeout=30)
        subprocess.run([sys.executable, '-I', helper, 'assemble', manifest], cwd=self.root, check=True,
                       capture_output=True, timeout=30)
        self.assertEqual(pkg.hash_file(self.output/'fixture-sdk.zip'), pkg.read_json(manifest)['archive']['sha256'])

    def test_release_pair_verification_and_runtime_failures(self):
        artifacts = self.root/'artifacts'; artifacts.mkdir()
        for platform, triplet in pkg.TRIPLETS.items():
            receipt = synthetic_receipt(triplet)
            (self.source/'static-sdk-receipt.json').write_bytes(pkg.json_bytes(receipt))
            pkg.package_sdk(self.source, artifacts/platform,
                f'cef-152.0.6-{triplet}-static-engine-capi', part_bytes=65536 if platform == 'linux' else 512)
        files = pkg.release_files(artifacts, COMMIT)
        self.assertTrue(all(p.is_file() and p.stat().st_size < pkg.ASSET_LIMIT for p in files))
        with self.assertRaises(ValueError): pkg.release_files(artifacts, 'c'*40)
        for mutation in (lambda r: r.update(fixture_only=True), lambda r: r.update(sdk_relocation_verified=False),
                         lambda r: r.update(cef_commit='0'*40), lambda r: r['smoke'].update(paint=False),
                         lambda r: r['smoke'].update(renderer_pid=1),
                         lambda r: r['smoke_runs'].update(passed_runs=2),
                         lambda r: r['smoke_runs']['runs'][1].update(status='failed'),
                         lambda r: r['smoke_runs']['runs'][1].update(cwd='fixture-profile-1'),
                         lambda r: r['smoke_runs'].update(executable_sha256='0'*64)):
            receipt = synthetic_receipt(); mutation(receipt)
            with self.assertRaises(ValueError): pkg.verify_receipt(receipt, COMMIT, 'x64-windows-static')
        (artifacts/'windows'/'unexpected.dll').write_bytes(b'not a verified asset')
        with self.assertRaises(ValueError): pkg.release_files(artifacts, COMMIT)

    def test_ci_packaging_follows_full_runtime_and_preserves_failure_evidence(self):
        ci = (ROOT/'vcpkg/static/ci.py').read_text()
        self.assertLess(ci.index('proof = build.execute_smoke'), ci.index('sdk_package.package_sdk'))
        self.assertIn('sdk_package.verify_receipt', ci)
        self.assertIn("diagnostics/'static-sdk-receipt.json'", ci)
        self.assertIn("'transport_verified': False", ci)
        self.assertNotIn('SDK ZIP exceeds the per-asset', ci)


class NativeTransportTests(unittest.TestCase):
    def evidence(self, name, result):
        folder = ROOT/'static-diagnostics/sdk-package-regression'; folder.mkdir(parents=True, exist_ok=True)
        (folder/(name+'.json')).write_bytes(pkg.json_bytes({'fixture_only': True,
            'engine_runtime_verified': False, **result}))

    def test_real_native_static_library_survives_pack_join_and_relocation(self):
        cc = shutil.which('cl.exe') if os.name == 'nt' else shutil.which('cc')
        ar = shutil.which('lib.exe') if os.name == 'nt' else shutil.which('ar')
        if not (cc and ar):
            if os.environ.get('CEF_PACKAGE_REQUIRE_NATIVE') == '1': self.fail('Native compiler required')
            self.skipTest('native compiler unavailable')
        with tempfile.TemporaryDirectory(prefix='cef native transport ') as temp:
            root = Path(temp); sdk = root/'producer'; sdk.mkdir()
            (sdk/'include').mkdir(); (sdk/'lib').mkdir()
            (sdk/'include/answer.h').write_text('int answer(void);\n')
            source = root/'answer.c'; source.write_text('int answer(void) { return 42; }\n')
            obj = root/('answer.obj' if os.name == 'nt' else 'answer.o')
            libname = 'answer.lib' if os.name == 'nt' else 'libanswer.a'
            commands = []
            def run(command):
                p = subprocess.run(list(map(str, command)), cwd=root, capture_output=True, text=True, timeout=60)
                commands.append({'command': list(map(str, command)), 'exit_code': p.returncode})
                self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
            run([cc, '/nologo', '/MT', '/W4', '/WX', '/c', source, '/Fo'+str(obj)] if os.name == 'nt'
                else [cc, '-Wall', '-Wextra', '-Werror', '-c', source, '-o', obj])
            run([ar, '/nologo', '/OUT:'+str(sdk/'lib'/libname), obj] if os.name == 'nt'
                else [ar, 'rcs', sdk/'lib'/libname, obj])
            (sdk/'static-sdk-receipt.json').write_bytes(pkg.json_bytes({'fixture_only': True}))
            manifest = pkg.package_sdk(sdk, root/'download', 'fixture-sdk', part_bytes=512)
            assembled = pkg.assemble(manifest)
            shutil.rmtree(sdk); source.unlink(); obj.unlink()
            with zipfile.ZipFile(assembled) as z: z.extractall(root/'relocated')
            restored = root/'relocated/fixture-sdk'
            main = root/'main.c'; main.write_text('#include "answer.h"\n'
                '#if defined(_WIN32) && (!defined(_MT) || defined(_DLL))\n#error Static CRT required\n#endif\n'
                'int main(void) { return answer() == 42 ? 0 : 1; }\n')
            exe = root/('standalone.exe' if os.name == 'nt' else 'standalone')
            run([cc, '/nologo', '/MT', '/W4', '/WX', '/I'+str(restored/'include'), main,
                 restored/'lib'/libname, '/Fe'+str(exe)] if os.name == 'nt' else
                [cc, '-Wall', '-Wextra', '-Werror', '-I'+str(restored/'include'), main,
                 restored/'lib'/libname, '-o', exe])
            restored.rename(root/'hidden-sdk')
            run([exe])
            self.evidence('native', {'status': 'success', 'commands': commands,
                                    'source_removed': True, 'sdk_hidden_during_run': True})

    @unittest.skipUnless(os.environ.get('CEF_PACKAGE_LARGE') == '1', '2-GiB boundary exercised in native CI')
    def test_actual_archive_larger_than_two_gib_uses_production_part_size(self):
        with tempfile.TemporaryDirectory(prefix='cef large transport ', dir=os.environ.get('RUNNER_TEMP')) as temp:
            root = Path(temp)
            self.assertGreater(shutil.disk_usage(root).free, 8*1024**3, 'Need disk for real 2-GiB regression')
            sdk = root/'SDK'; sdk.mkdir()
            payload = sdk/'large.lib'
            # Sparse input avoids storing a second large producer. ZIP_STORED
            # guarantees the archive crosses 2 GiB, independent of compression.
            with payload.open('wb') as stream: stream.truncate(pkg.ASSET_LIMIT+4096)
            (sdk/'static-sdk-receipt.json').write_bytes(pkg.json_bytes({'fixture_only': True}))
            manifest = pkg.package_sdk(sdk, root/'assets', 'large-fixture', compression=zipfile.ZIP_STORED)
            data = pkg.read_json(manifest)
            self.assertGreater(data['archive']['size'], pkg.ASSET_LIMIT)
            self.assertEqual(len(data['parts']), 3)
            self.assertEqual([p['size'] for p in data['parts'][:2]], [pkg.PART_BYTES]*2)
            self.assertTrue(all(p['size'] < pkg.ASSET_LIMIT for p in data['parts']))
            assembled = pkg.assemble(manifest)
            self.assertEqual(pkg.hash_file(assembled), data['archive']['sha256'])
            with zipfile.ZipFile(assembled) as z:
                self.assertIsNone(z.testzip())
                self.assertEqual(z.getinfo('large-fixture/large.lib').file_size, pkg.ASSET_LIMIT+4096)
            self.evidence('large-boundary', {'status': 'success', 'archive': data['archive'],
                'parts': data['parts'], 'roundtrip_sha256_verified': True,
                'zip64_headers': True, 'stream_block_bytes': pkg.BLOCK})


if __name__ == '__main__': unittest.main()
