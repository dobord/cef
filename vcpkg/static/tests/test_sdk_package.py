"""Distribution fixtures only: these tests do not build or certify CEF."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import io
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
SPEC = importlib.util.spec_from_file_location('distribution', ROOT/'vcpkg/static/sdk_package.py')
pack = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(pack)
COMMIT = 'a'*40


def fixture_receipt(triplet='x64-windows-static'):
    """Deliberate validator fixture, never a receipt for a real CEF binary."""
    n = 3 if triplet == 'x64-windows-static' else 1
    return {'schema': 1, **pack.PINS, 'integration_commit': COMMIT, 'triplet': triplet,
            'configuration': 'Release', 'engine_linkage': 'static', 'capi_only': True,
            'sdk_relocation_verified': True, 'application_relocation_verified': True,
            'sandbox_verified': False, 'system_libraries_static': False,
            'executable_sha256': 'b'*64,
            'smoke': {'cef': pack.VERSION, 'engine': 'static', 'interface': 'capi',
                      'javascript': True, 'paint': True, 'browser_modules_clean': True,
                      'renderer_modules_clean': True, 'browser_pid': 1, 'renderer_pid': 2},
            'runtime_runs': {'required_runs': n, 'passed_runs': n, 'status': 'success',
                             'engine_runtime_verified': True, 'no_retry_on_failure': True,
                             'executable_sha256': 'b'*64,
                             'runs': [{'number': i, 'cwd': 'fixture/profile-'+str(i),
                                       'status': 'success', 'engine_runtime_verified': True}
                                      for i in range(1, n+1)]}}


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cef sdk packaging ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make(self, triplet='x64-windows-static', size=10000, limit=4096, tag=''):
        receipt = fixture_receipt(triplet)
        name = 'cef-152.0.6-'+triplet+'-static-engine-capi'
        root = self.root/tag if tag else self.root
        sdk = root/'original SDK'; sdk.mkdir(parents=True)
        (sdk/'library.lib').write_bytes(bytes(range(256))*(size//256+1))
        output = root/'dist'
        result = pack.package_sdk(sdk, output, name, receipt, root/'diagnostics.json',
                                  part_bytes=limit, compression=zipfile.ZIP_STORED)
        return output/(name+'.manifest.json'), result, sdk

    def test_stream_split_relocation_join_crc_and_embedded_receipt(self):
        manifest, result, sdk = self.make()
        self.assertGreater(result['part_count'], 1)
        shutil.rmtree(sdk)
        moved = self.root/'downloaded SDK with spaces'; manifest.parent.rename(moved)
        manifest = moved/manifest.name
        joined = self.root/'joined ZIP with spaces.zip'
        pack.join_bundle(manifest, joined)
        self.assertEqual(pack.record(joined)['sha256'], result['archive']['sha256'])
        with zipfile.ZipFile(joined) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(json.loads(z.read(result['name']+'/static-sdk-receipt.json')), fixture_receipt())
            self.assertTrue(z.read(result['name']+'/library.lib').startswith(bytes(range(256))))
        # Use the COPIED standalone helper, not the source checkout module.
        helper = moved/(result['name']+'-restore.py')
        p = subprocess.run([sys.executable, '-I', helper, 'verify', manifest],
                           cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
        other = self.root/'CLI joined.zip'
        p = subprocess.run([sys.executable, '-I', helper, 'join', manifest, '--output', other],
                           cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
        self.assertEqual(pack.record(joined)['sha256'], pack.record(other)['sha256'])

    def test_single_zip_linux_is_still_directly_usable(self):
        manifest, result, _ = self.make('x64-linux', size=512, limit=100000)
        self.assertEqual(result['part_count'], 1)
        archive = manifest.parent/result['archive']['name']
        with zipfile.ZipFile(archive) as z: self.assertIsNone(z.testzip())
        with self.assertRaises(ValueError): pack.join_bundle(manifest, archive)

    def test_exact_chunk_boundaries_and_reader_seeks(self):
        for size in (1, 8, 9, 16, 17):
            with self.subTest(size=size):
                folder = self.root/str(size); folder.mkdir()
                writer = pack.PartWriter(folder, 'x.zip', 8)
                data = bytes(range(size))
                for offset in range(0, size, 3): writer.write(data[offset:offset+3])
                writer.close()
                self.assertEqual(sum(p['size'] for p in writer.parts), size)
                self.assertEqual(len(writer.parts), (size+7)//8)
                self.assertTrue(all(0 < p['size'] <= 8 for p in writer.parts))
                with pack.PartReader(folder, writer.parts) as reader:
                    self.assertEqual(reader.read(3), data[:3])
                    reader.seek(0); self.assertEqual(reader.read(), data)
                    reader.seek(-1, 2); self.assertEqual(reader.read(), data[-1:])
                    reader.seek(size+1); self.assertEqual(reader.read(), b'')
                    with self.assertRaises(ValueError): reader.seek(-1)

    def test_capacity_and_part_count_guards(self):
        for bad in (0, -1, True, pack.ASSET_LIMIT, pack.ASSET_LIMIT+1):
            with self.assertRaises(ValueError): pack.PartWriter(self.root, 'x.zip', bad)
        with mock.patch.object(pack, 'MAX_PARTS', 2):
            writer = pack.PartWriter(self.root, 'x.zip', 1)
            try:
                with self.assertRaises(ValueError): writer.write(b'abc')
            finally: writer.close()

    def test_reordered_missing_extra_and_corrupt_parts_rejected_before_join(self):
        manifest, result, _ = self.make()
        base = json.loads(manifest.read_text())
        target = self.root/'should not exist.zip'
        mutated = copy.deepcopy(base); mutated['parts'].reverse(); pack.dump(manifest, mutated)
        with self.assertRaises(ValueError): pack.join_bundle(manifest, target)
        pack.dump(manifest, base)
        first = manifest.parent/base['parts'][0]['name']; original = first.read_bytes()
        first.unlink()
        with self.assertRaises(OSError): pack.join_bundle(manifest, target)
        first.write_bytes(original[:-1])
        with self.assertRaises(ValueError): pack.join_bundle(manifest, target)
        first.write_bytes(bytes([original[0]^1])+original[1:])
        with self.assertRaises(ValueError): pack.join_bundle(manifest, target)
        first.write_bytes(original)
        extra = manifest.parent/(result['archive']['name']+'.part999'); extra.write_bytes(b'x')
        with self.assertRaises(ValueError): pack.join_bundle(manifest, target)
        self.assertFalse(target.exists()); self.assertFalse(list(self.root.glob('.cef-sdk-join-*')))

    def test_unsafe_names_metadata_and_size_mismatch_rejected(self):
        manifest, _, _ = self.make(); base = json.loads(manifest.read_text())
        for change in ('../escape', '/absolute', 'C:\\escape', '@args', 'part;other', 'a\nb'):
            data = copy.deepcopy(base); data['parts'][0]['name'] = change; pack.dump(manifest, data)
            with self.subTest(change=change), self.assertRaises(ValueError): pack.verify_bundle(manifest)
        data = copy.deepcopy(base); data['archive']['size'] += 1; pack.dump(manifest, data)
        with self.assertRaises(ValueError): pack.verify_bundle(manifest)
        data = copy.deepcopy(base); data['metadata']['restore']['name'] = '../evil.py'; pack.dump(manifest, data)
        with self.assertRaises(ValueError): pack.verify_bundle(manifest)

    def test_provenance_runtime_pins_and_repetitions_are_required(self):
        base = fixture_receipt()
        mutations = [('integration_commit', 'not-a-commit'), ('cef_commit', 'c'*40),
                     ('engine_linkage', 'shared'), ('sdk_relocation_verified', False),
                     ('application_relocation_verified', 1), ('executable_sha256', '')]
        for key, value in mutations:
            data = copy.deepcopy(base); data[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): pack.validate_receipt(data)
        for key, value in [('passed_runs', 2), ('required_runs', 1), ('no_retry_on_failure', False),
                           ('executable_sha256', 'c'*64)]:
            data = copy.deepcopy(base); data['runtime_runs'][key] = value
            with self.assertRaises(ValueError): pack.validate_receipt(data)
        data = copy.deepcopy(base); data['smoke']['renderer_pid'] = 1
        with self.assertRaises(ValueError): pack.validate_receipt(data)
        data = copy.deepcopy(base); data['runtime_runs']['runs'][1]['cwd'] = data['runtime_runs']['runs'][0]['cwd']
        with self.assertRaises(ValueError): pack.validate_receipt(data)
        with self.assertRaises(ValueError): pack.validate_receipt(base, 'c'*40)

    def rewrite_zip(self, manifest, entries):
        data = json.loads(manifest.read_text()); self.assertEqual(len(data['parts']), 1)
        archive = manifest.parent/data['archive']['name']
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_STORED) as z:
            for name, content in entries: z.writestr(name, content)
        info = pack.record(archive); data['archive'] = info.copy(); data['parts'] = [info]
        checksum = manifest.parent/data['metadata']['checksum']['name']
        checksum.write_text(info['sha256']+'  '+info['name']+'\n')
        data['metadata']['checksum'] = pack.record(checksum); pack.dump(manifest, data)

    def test_internal_receipt_must_match_external_even_when_hashes_match(self):
        manifest, result, _ = self.make('x64-linux', limit=100000)
        self.rewrite_zip(manifest, [(result['name']+'/static-sdk-receipt.json', b'{}')])
        with self.assertRaises(ValueError): pack.verify_bundle(manifest)

    def test_zip_traversal_duplicates_and_crc_are_checked(self):
        manifest, result, _ = self.make('x64-linux', limit=100000)
        receipt = (manifest.parent/(result['name']+'.json')).read_bytes()
        for bad in [result['name']+'/../escape', '/etc/file', result['name']+'/C:bad']:
            self.rewrite_zip(manifest, [(bad, b'data'), (result['name']+'/static-sdk-receipt.json', receipt)])
            with self.assertRaises(ValueError): pack.verify_bundle(manifest)
        self.rewrite_zip(manifest, [(result['name']+'/payload', b'unique-data'),
                                    (result['name']+'/static-sdk-receipt.json', receipt)])
        data = json.loads(manifest.read_text()); archive = manifest.parent/data['archive']['name']
        raw = archive.read_bytes(); self.assertEqual(raw.count(b'unique-data'), 1)
        archive.write_bytes(raw.replace(b'unique-data', b'unique-bad!'))
        data['archive'] = pack.record(archive); data['parts'] = [data['archive'].copy()]
        checksum = manifest.parent/data['metadata']['checksum']['name']
        checksum.write_text(data['archive']['sha256']+'  '+archive.name+'\n')
        data['metadata']['checksum'] = pack.record(checksum); pack.dump(manifest, data)
        with self.assertRaises(zipfile.BadZipFile): pack.verify_bundle(manifest)

    def test_failure_does_not_leave_uploadable_output_or_overwrite_old_files(self):
        sdk = self.root/'sdk'; sdk.mkdir(); (sdk/'file').write_bytes(b'data')
        dest = self.root/'output'; dest.mkdir(); diagnostics = self.root/'diag.json'
        name = 'cef-152.0.6-x64-linux-static-engine-capi'
        with mock.patch.object(pack, 'verify_bundle', side_effect=ValueError('injected verifier failure')):
            with self.assertRaises(ValueError): pack.package_sdk(sdk, dest, name, fixture_receipt('x64-linux'), diagnostics)
        self.assertEqual(list(dest.iterdir()), [])
        self.assertEqual(json.loads(diagnostics.read_text())['status'], 'failed')
        (dest/'existing').write_bytes(b'keep')
        with self.assertRaises(ValueError): pack.package_sdk(sdk, dest, name, fixture_receipt('x64-linux'), diagnostics)
        self.assertEqual((dest/'existing').read_bytes(), b'keep')

    def test_join_never_overwrites_output(self):
        manifest, _, _ = self.make()
        output = self.root/'existing.zip'; output.write_bytes(b'keep')
        with self.assertRaises(ValueError): pack.join_bundle(manifest, output)
        self.assertEqual(output.read_bytes(), b'keep')

    def test_release_verifies_one_native_pair_and_exact_upload_file_set(self):
        for platform, triplet in [('linux','x64-linux'), ('windows','x64-windows-static')]:
            manifest, _, _ = self.make(triplet, tag=platform+'-input')
            manifest.parent.rename(self.root/platform)
        files = pack.verify_release(self.root, COMMIT)
        self.assertGreater(len(files), 12)
        self.assertEqual(len({Path(p).name for p in files}), len(files))
        with self.assertRaises(ValueError): pack.verify_release(self.root, 'c'*40)
        (self.root/'windows/unexpected.zip').write_bytes(b'bad')
        with self.assertRaises(ValueError): pack.verify_release(self.root, COMMIT)

    def test_symlink_assets_and_source_escape_are_rejected(self):
        if os.name == 'nt': self.skipTest('symlink privilege is not assumed on Windows')
        manifest, _, _ = self.make()
        data = json.loads(manifest.read_text()); part = manifest.parent/data['parts'][0]['name']
        saved = self.root/'outside'; part.rename(saved); part.symlink_to(saved)
        with self.assertRaises(ValueError): pack.verify_bundle(manifest)
        sdk = self.root/'escape-sdk'; sdk.mkdir(); (sdk/'link').symlink_to(saved)
        with self.assertRaises(ValueError):
            pack.package_sdk(sdk,self.root/'escape-output','cef-152.0.6-x64-linux-static-engine-capi',
                             fixture_receipt('x64-linux'),self.root/'escape.json')

    def test_zip64_paths_with_lowered_zip64_threshold(self):
        with mock.patch.object(zipfile, 'ZIP64_LIMIT', 1024):
            manifest, result, _ = self.make(size=20000)
            pack.join_bundle(manifest, self.root/'zip64.zip')
        self.assertTrue(pack.verify_bundle(manifest)['zip_crc_verified'])


@unittest.skipUnless(os.environ.get('CEF_SDK_LARGE_PACKAGE_TEST') == '1', 'explicit 2 GiB native fixture')
class LargeDistributionTests(unittest.TestCase):
    def test_real_zip_beyond_release_limit_is_streamed_and_verified(self):
        logs = ROOT/'static-diagnostics/sdk-packaging-regression'; logs.mkdir(parents=True, exist_ok=True)
        proof = logs/'large-result.json'; proof.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix='cef large SDK ', dir=os.environ.get('RUNNER_TEMP')) as tmp:
            root = Path(tmp); sdk = root/'sdk'; sdk.mkdir()
            # Sparse source, but the ZIP_STORED output really writes over 2 GiB.
            with (sdk/'large.lib').open('wb') as f: f.truncate(pack.ASSET_LIMIT+65536)
            triplet = 'x64-windows-static' if os.name == 'nt' else 'x64-linux'
            name = 'cef-152.0.6-'+triplet+'-static-engine-capi'
            result = pack.package_sdk(sdk, root/'dist', name, fixture_receipt(triplet),
                                      logs/'large-packaging.json', compression=zipfile.ZIP_STORED)
            self.assertGreater(result['archive']['size'], pack.ASSET_LIMIT)
            self.assertEqual(result['part_count'], 2)
            for file in (root/'dist').iterdir(): self.assertLess(file.stat().st_size, pack.ASSET_LIMIT)
            self.assertFalse((root/'dist'/(name+'.zip')).exists())
            pack.dump(proof, {'fixture_only': True, 'cef_runtime_verified': False,
                              'status':'success', 'large_zip64': result,
                              'no_monolithic_archive_created': True})


if __name__ == '__main__': unittest.main()
