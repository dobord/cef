"""Reconciliation with 11aefd9/8e75ce3. Fixtures/mocks are not engine tests."""
from __future__ import annotations
import ast
import contextlib
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

PORT = Path(__file__).resolve().parents[1]
ROOT = PORT.parents[2]
spec = importlib.util.spec_from_file_location('reconciled_builder', PORT/'source_build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class PreservedLauncherTests(unittest.TestCase):
    def test_tools_phase_is_retained_and_never_prepares_chromium(self):
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(sys, 'argv', ['source_build.py', 'tools', '--work', temp, '--logs', temp]), \
             mock.patch.object(build.platform, 'machine', return_value='x86_64'), \
             mock.patch.object(build, 'setup_environment') as env, \
             mock.patch.object(build, 'check_tools') as tools, \
             mock.patch.object(build, 'prepare') as prepare, \
             mock.patch.object(build, 'configuration') as config, \
             mock.patch.object(build, 'compile_and_test') as native:
            build.main()
            env.assert_called_once()
            tools.assert_called_once()
            prepare.assert_not_called()
            config.assert_not_called()
            native.assert_not_called()

    def test_git_and_network_implementations_are_not_duplicated(self):
        tree = ast.parse((PORT/'source_build.py').read_text(encoding='utf-8'))
        functions = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        self.assertEqual(len(functions), len(set(functions)))
        for name in ('git_program', 'python_script', 'check_tools', 'run_network'):
            self.assertEqual(functions.count(name), 1)
        for superseded in ('git_executable', 'run_network_step', 'source_sync'):
            self.assertNotIn(superseded, functions)

    @unittest.skipUnless(shutil.which('git'), 'Native Git needed')
    def test_actual_git_hash_with_empty_path(self):
        git = str(Path(shutil.which('git')).resolve())
        with tempfile.TemporaryDirectory(prefix='cef git spaces ') as temp:
            root = Path(temp)
            for args in (['init'], ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                                    'commit', '--allow-empty', '-m', 'local test fixture']):
                subprocess.run([git, *args], cwd=root, check=True, capture_output=True)
            expected = subprocess.check_output([git, 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
            with mock.patch.dict(os.environ, {'CEF_STATIC_GIT': git, 'PATH': ''}):
                self.assertEqual(build.git_hash(root), expected)

    @unittest.skipUnless(shutil.which('git'), 'Native Git needed')
    def test_setup_environment_accepts_missing_path_and_pins_git(self):
        git = str(Path(shutil.which('git')).resolve())
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.dict(os.environ, {'CEF_STATIC_GIT': git}, clear=True):
            work = Path(temp)
            build.setup_environment(work)
            self.assertEqual(os.environ['CEF_STATIC_GIT'], git)
            self.assertIn(str(Path(git).parent), os.environ['PATH'].split(os.pathsep))
            self.assertIn(str(work/'depot_tools'), os.environ['PATH'].split(os.pathsep))
            self.assertEqual(os.environ['DEPOT_TOOLS_UPDATE'], '0')

    @unittest.skipUnless(shutil.which('git'), 'Native Git needed')
    def test_real_tools_command_in_isolated_python(self):
        git = str(Path(shutil.which('git')).resolve())
        with tempfile.TemporaryDirectory(prefix='cef toolcheck spaces ') as temp:
            env = dict(os.environ, CEF_STATIC_GIT=git, PATH='')
            result = subprocess.run([sys.executable, '-I', str(PORT/'source_build.py'),
                                     'tools', '--work', temp, '--logs', temp],
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            receipt = json.loads((Path(temp)/'toolchain.json').read_text())
            self.assertIs(receipt['sibling_import_verified'], True)
            self.assertIs(receipt['engine_build_verified'], False)
            self.assertEqual(Path(receipt['git']), Path(git))
            self.assertFalse((Path(temp)/'prepared.json').exists())


class PreservedPreparationTests(unittest.TestCase):
    def prepare_case(self, *, fail=None, resumed=False, change_pin=False):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            logs = work/'logs'; logs.mkdir()
            source = work/'download/chromium/src'
            (work/'depot_tools/.git').mkdir(parents=True)
            (source/'cef/.git').mkdir(parents=True)
            if resumed:
                (source/'.git').mkdir()
            data = b'# synthetic automation content; subprocess mocked\n'
            calls = []
            def git_hash(path):
                if path == work/'depot_tools': return build.DEPOT
                if path == source/'cef': return build.CEF
                if change_pin and 'verify-runhooks' in [x[0] for x in calls]: return 'wrong-pin'
                return build.CHROMIUM
            def fake_run(command, cwd, logs, name, **kwargs):
                calls.append((name, list(map(str, command))))
                if name == fail: raise RuntimeError('injected failure: '+name)
                return 'fixture completed'
            with mock.patch.object(build, 'WINDOWS', False), \
                 mock.patch.object(build.shutil, 'disk_usage', return_value=mock.Mock(free=200*1024**3)), \
                 mock.patch.object(build, 'git_hash', side_effect=git_hash), \
                 mock.patch.object(build.urllib.request, 'urlopen', return_value=io.BytesIO(data)), \
                 mock.patch.object(build, 'AUTOMATE_SHA256', hashlib.sha256(data).hexdigest()), \
                 mock.patch.object(build, 'run', side_effect=fake_run), \
                 mock.patch.object(build, 'run_network', side_effect=fake_run):
                if fail or change_pin:
                    with self.assertRaises(RuntimeError): build.prepare(work, logs)
                    self.assertFalse((work/'prepared.json').exists())
                    self.assertFalse((logs/'source-provenance.json').exists())
                else:
                    self.assertEqual(build.prepare(work, logs), source)
                    self.assertTrue((work/'prepared.json').exists())
                    self.assertTrue((logs/'source-provenance.json').exists())
            return calls

    def test_fresh_checkout_runs_dependency_sync_patch_and_hooks(self):
        calls = self.prepare_case()
        self.assertEqual([c[0] for c in calls], ['source-sync', 'verify-dependency-sync',
                                               'verify-runhooks-patch', 'verify-runhooks'])
        self.assertEqual(calls[0][1][1], '-c')
        self.assertEqual(calls[1][1][-3:], ['sync', '--no-history', '--nohooks'])
        self.assertEqual(calls[2][1][1], '-c')
        self.assertIn('--patch-file', calls[2][1])
        self.assertEqual(calls[3][1][-1], 'runhooks')

    def test_resumed_checkout_does_not_skip_dependency_and_hook_verification(self):
        calls = self.prepare_case(resumed=True)
        self.assertEqual([c[0] for c in calls], ['source-sync', 'verify-dependency-sync',
                                               'verify-runhooks-patch', 'verify-runhooks'])

    def test_failed_dependency_sync_cannot_certify_preparation(self):
        calls = self.prepare_case(fail='verify-dependency-sync')
        self.assertNotIn('verify-runhooks', [c[0] for c in calls])

    def test_failed_patch_cannot_certify_preparation(self):
        self.prepare_case(fail='verify-runhooks-patch')

    def test_failed_hooks_cannot_certify_preparation(self):
        self.prepare_case(fail='verify-runhooks')

    def test_post_hook_pin_change_cannot_certify_preparation(self):
        self.prepare_case(change_pin=True)

    def test_failed_source_sync_never_reaches_hooks(self):
        calls = self.prepare_case(fail='source-sync')
        self.assertEqual([c[0] for c in calls], ['source-sync'])


class ConfigurationMergeTests(unittest.TestCase):
    def test_isolated_python_wrapper_and_early_link_audit_coexist(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)/'source'
            cef = source/'cef'; (cef/'tools').mkdir(parents=True)
            (cef/'tools/gn_args.py').write_text(
                'def GetMergedArgs(args): return args\n'
                'def GetConfigArgs(args, debug, cpu): return args\n'
                'def GetConfigFileContents(args): return "# synthetic GN fixture"\n')
            logs = Path(temp)/'logs'; logs.mkdir()
            calls = {}
            def run(command, cwd, logs, name, **kwargs):
                calls[name] = list(map(str, command))
                if name == 'gn-graph': return '{"//cef:cef_static_smoke":{"ldflags":["-Wl,-z,now"]}}'
                if name == 'native-link-edge': return 'app:\n  input: link\n    obj/engine.a\n  outputs:\n'
                return ''
            with mock.patch.object(build, 'WINDOWS', False), \
                 mock.patch.object(build, 'run', side_effect=run), \
                 mock.patch.object(build, 'find_binary', return_value=Path('/unused-fixture-tool')):
                build.configuration(source, logs)
            for name in ('cef-translator', 'cef-upstream-patches', 'static-source-patches'):
                self.assertEqual(calls[name][1], '-c')
                self.assertIn('runpy.run_path', calls[name][2])
            self.assertIn('--fail-on-unused-args', calls['gn-gen'])
            audit = json.loads((logs/'link-option-audit.json').read_text())
            self.assertIs(audit['engine_link_verified'], False)
            self.assertIs(audit['engine_runtime_verified'], False)
            self.assertEqual(audit['semantic_link_flags'], ['-Wl,-z,now'])


class WorkflowMergeTests(unittest.TestCase):
    def test_native_toolcheck_is_kept_before_any_source_sync(self):
        workflow = (ROOT/'.github/workflows/static-engine-build.yml').read_text()
        self.assertEqual(workflow.count('python vcpkg/static/toolcheck.py --triplet'), 1)
        self.assertLess(workflow.index('python vcpkg/static/toolcheck.py'),
                        workflow.index('source_build.py prepare'))
        self.assertIn('source_build.py check', workflow)
        self.assertNotIn('source_build.py regressions', workflow)

    def test_original_toolcheck_ports_keep_one_shared_git_acquisition(self):
        native = (PORT/'portfile.cmake').read_text()
        probe = (ROOT/'vcpkg/static/toolcheck-port/portfile.cmake').read_text()
        self.assertEqual(native.count('acquire_git.cmake'), 1)
        self.assertEqual(probe.count('acquire_git.cmake'), 1)
        self.assertNotIn('vcpkg_find_acquire_program(GIT)', native)
        self.assertIn('source_build.py" tools', native)
        self.assertIn('source_build.py" tools', probe)

    def test_cache_fallback_and_publication_verification_remain(self):
        workflow = (ROOT/'.github/workflows/static-engine-build.yml').read_text()
        self.assertIn('            cef-static-152-linux-\n', workflow)
        self.assertIn("if: github.event_name != 'pull_request'", workflow)
        self.assertIn('needs: source-build', workflow)
        self.assertIn("receipt['integration_commit']", workflow)
        self.assertIn('browser_modules_clean', workflow)
        self.assertNotIn('--clobber', workflow)

if __name__ == '__main__':
    unittest.main()
