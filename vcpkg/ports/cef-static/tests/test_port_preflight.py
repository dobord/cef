"""Execute actual port gates in CMake script mode; never simulate an engine build."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

PORT = Path(__file__).resolve().parents[1] / 'portfile.cmake'


@unittest.skipUnless(shutil.which('cmake'), 'CMake required for port preflight test')
class PortPreflightTests(unittest.TestCase):
    def check_gate(self, host: str, target: str, *, architecture: str = 'x64',
                   system: str = 'Linux', library: str = 'static', crt: str = 'static'):
        # These are the documented portfile variables, deliberately WITHOUT
        # VCPKG_HOST_TRIPLET. Port preflight must work in real script scope.
        definitions = {
            'HOST_TRIPLET': host,
            'VCPKG_TARGET_ARCHITECTURE': architecture,
            'VCPKG_TARGET_IS_WINDOWS': 'ON' if target == 'Windows' else 'OFF',
            'VCPKG_TARGET_IS_LINUX': 'ON' if target == 'Linux' else 'OFF',
            'VCPKG_LIBRARY_LINKAGE': library,
            'VCPKG_CRT_LINKAGE': crt,
            'CMAKE_HOST_WIN32': 'ON' if system == 'Windows' else 'OFF',
            'CMAKE_HOST_SYSTEM_NAME': system,
        }
        body = '\n'.join(f'set({key} "{value}")' for key, value in definitions.items())
        body += '\nmacro(vcpkg_find_acquire_program)\n'
        body += '  message(STATUS "CEF_PORT_GATES_PASSED")\n'
        body += '  return()\nendmacro()\n'
        body += f'include("{PORT.as_posix()}")\n'
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / 'preflight.cmake'
            script.write_text(body)
            result = subprocess.run(['cmake', '-P', str(script)], text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=20, check=False)
        return result

    def test_native_linux(self):
        result = self.check_gate('x64-linux', 'Linux')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('CEF_PORT_GATES_PASSED', result.stdout)

    def test_native_windows_with_static_target_crt(self):
        result = self.check_gate('x64-windows', 'Windows', system='Windows')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('CEF_PORT_GATES_PASSED', result.stdout)

    def test_cross_os_rejected(self):
        for system, target in [('Linux', 'Windows'), ('Windows', 'Linux')]:
            with self.subTest(system=system, target=target):
                result = self.check_gate('x64-' + system.lower(), target, system=system)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertNotIn('CEF_PORT_GATES_PASSED', result.stdout)

    def test_non_x64_host_rejected(self):
        result = self.check_gate('arm64-linux', 'Linux')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('requires an x64 host', result.stdout)

    def test_non_x64_target_rejected(self):
        result = self.check_gate('x64-linux', 'Linux', architecture='arm64')
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_dynamic_library_rejected(self):
        result = self.check_gate('x64-linux', 'Linux', library='dynamic')
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_dynamic_windows_crt_rejected(self):
        result = self.check_gate('x64-windows', 'Windows', system='Windows', crt='dynamic')
        self.assertNotEqual(result.returncode, 0, result.stdout)


if __name__ == '__main__':
    unittest.main()
