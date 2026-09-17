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


def run(argv: list[str | Path], cwd: Path) -> None:
    subprocess.run([str(p) for p in argv], cwd=cwd, check=True, timeout=1800)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
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
    acquisition = sdk_import.install_bundle(lock, triplet, transport, prefix)
    (evidence / "acquisition.json").write_bytes(sdk_import.sdk.json_bytes(acquisition))
    shutil.copyfile(prefix / "share/cef-static/static-link-inventory.json", evidence / "upstream-link-inventory.json")
    shutil.rmtree(work / "download")
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
    run([sys.executable, ROOT / "vcpkg/integration/driver.py", "verify-consumer",
         "--work", work / "no-chromium-workspace", "--logs", evidence,
         "--contract", acquisition["manifest_sha256"], "--state", evidence / "consumer.json",
         "--executable", deployed / name, "--hide", prefix, "--hide", build], work)
    proof = sdk_import.read_json(evidence / "consumer.json")
    if proof.get("kind") != "consumer-verification" or proof.get("engine_linkage") != "static":
        raise ValueError("Native release-import verification did not complete")
    print("LOCKED_CEF_IMPORT_NATIVE_CONSUMER_VERIFIED", flush=True)


if __name__ == "__main__":
    main()
