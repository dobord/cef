#!/usr/bin/env python3
"""Exercise the actual pinned release importer and a relocated native consumer."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sdk_import

ROOT = Path(__file__).resolve().parents[2]


def run(argv: list[str | Path], cwd: Path, *, env: dict | None = None) -> None:
    subprocess.run([str(p) for p in argv], cwd=cwd, env=env,
                   check=True, timeout=1800)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--required-profile",
                        choices=("engine-static", "static-third-party"),
                        default="engine-static")
    args = parser.parse_args()
    work, evidence = args.work.resolve(), args.evidence.resolve()
    if work.exists() or evidence.is_relative_to(work):
        raise ValueError("Use a new workspace with evidence outside the hidden SDK")
    work.mkdir(parents=True)
    evidence.mkdir(parents=True, exist_ok=True)
    windows = os.name == "nt"
    triplet = "x64-windows-static-release" if windows else "x64-linux-static-release"
    lock = sdk_import.read_json(ROOT / "vcpkg/integration/release.lock.json")
    transport = sdk_import.obtain(lock, triplet, work / "download")
    prefix = work / "imported-sdk"
    if args.required_profile == "static-third-party" and not windows:
        raise ValueError("Strict release requalification is Windows-only")
    acquisition = sdk_import.install_bundle(
        lock, triplet, transport, prefix, required_profile=args.required_profile)
    (evidence / "acquisition.json").write_bytes(sdk_import.sdk.json_bytes(acquisition))
    shutil.copyfile(prefix / "share/cef-static/static-link-inventory.json", evidence / "upstream-link-inventory.json")
    shutil.rmtree(work / "download")
    if windows:
        cpp_build = work / "cpp-support-build"
        cpp_install = work / "cpp-support-install"
        run([
            "cmake", "-S", ROOT / "vcpkg/ports/cef-static/cpp_support",
            "-B", cpp_build, "-G", "Visual Studio 17 2022", "-A", "x64",
            "-DCEF_RECIPE_SOURCE=" + ROOT.as_posix(),
            "-DCEF_PACKAGE_PREFIX=" + prefix.as_posix(),
        ], work)
        run(["cmake", "--build", cpp_build, "--config", "Release", "--parallel", "2"], work)
        run(["cmake", "--install", cpp_build, "--config", "Release",
             "--prefix", cpp_install], work)
        archive = cpp_install / "lib/cef-static/cef_cpp_support.lib"
        if not archive.is_file() or archive.stat().st_size == 0:
            raise ValueError("Native CEF C++ support archive was not produced")
    build = work / "consumer"
    command = ["cmake", "-S", ROOT / "vcpkg/static/consumer", "-B", build,
               "-DCMAKE_PREFIX_PATH=" + str(prefix),
               "-DCEF_STATIC_SMOKE_SOURCE=" + str(ROOT / "vcpkg/ports/cef-static/smoke.c"),
               "-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF", "-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF"]
    command += ["-G", "Visual Studio 17 2022", "-A", "x64"] if windows else ["-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release"]
    run(command, work)
    run(["cmake", "--build", build, "--config", "Release", "--parallel", "2"], work)
    binary_dir = build / "Release" if windows else build
    deployed = work / "deployed"
    deployed.mkdir()
    name = "cef_static_smoke.exe" if windows else "cef_static_smoke"
    shutil.copy2(binary_dir / name, deployed / name)
    for resource in ("icudtl.dat", "resources.pak", "chrome_100_percent.pak", "chrome_200_percent.pak",
                     "snapshot_blob.bin", "v8_context_snapshot.bin"):
        if (binary_dir / resource).is_file():
            shutil.copy2(binary_dir / resource, deployed / resource)
    if (binary_dir / "locales").is_dir():
        shutil.copytree(binary_dir / "locales", deployed / "locales")
    runtime_env = os.environ.copy()
    if args.required_profile == "static-third-party":
        runtime_env["CEF_STATIC_STRICT_THIRD_PARTY"] = "1"
    run([sys.executable, ROOT / "vcpkg/integration/driver.py", "verify-consumer",
         "--work", work / "no-chromium-workspace", "--logs", evidence,
         "--contract", acquisition["manifest_sha256"], "--state", evidence / "consumer.json",
         "--executable", deployed / name, "--hide", prefix, "--hide", build],
        work, env=runtime_env)
    proof = sdk_import.read_json(evidence / "consumer.json")
    if proof.get("kind") != "consumer-verification" or proof.get("engine_linkage") != "static":
        raise ValueError("Native release-import verification did not complete")
    if args.required_profile == "static-third-party":
        if (acquisition.get("strict_requalification_required") is not True
                or proof.get("third_party_libraries_static") is not True
                or proof.get("smoke", {}).get("third_party_modules_static") is not True):
            raise ValueError("Strict Windows release reuse was not independently requalified")
    print("LOCKED_CEF_IMPORT_NATIVE_CONSUMER_VERIFIED", flush=True)


if __name__ == "__main__":
    main()
