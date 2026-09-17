"""The sole port entry point for source and release acquisition; no fallback."""
from __future__ import annotations
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from common import PLATFORMS, require, read_json, validate_lock, write_json, semantic_key
from release_import import import_release
import static_dependencies

ROOT = Path(__file__).resolve().parents[1]


def run(args: list[str]) -> None:
    subprocess.run(args, check=True)


def install(lock: dict, platform: str, prefix: Path, work: Path, downloads: Path,
            dependencies: Path, pkgconf: Path | None, jobs: int) -> None:
    validate_lock(lock)
    require(platform in PLATFORMS and (os.name == "nt") == (platform == "windows"), "native target host required")
    require(type(jobs) is int and 1 <= jobs <= 1024, "invalid job count")
    recipe = ROOT / "ports/cef-static"
    if lock["acquisition"] == "release":
        import_release(lock, platform, downloads, prefix)
    else:
        spec = importlib.util.spec_from_file_location("locked_cef_source", recipe / "source_build.py")
        require(spec is not None and spec.loader is not None, "missing source recipe")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        require(module.CEF == lock["cef_sha"] and module.CHROMIUM == lock["chromium_sha"], "source recipe and lock disagree")
        logs = work / "integration-logs"
        run([sys.executable, str(recipe / "source_build.py"), "build", "--work", str(work), "--logs", str(logs), "--jobs", str(jobs)])
        run([sys.executable, str(recipe / "export_static.py"), "--source", str(work / "download/chromium/src"),
             "--out", str(work / "download/chromium/src/out/CEF_Static_Release_x64"), "--diagnostics", str(logs), "--prefix", str(prefix)])
        write_json(prefix / "share/cef-static/acquisition-receipt.json", {
            "schema": 1, "kind": "source", "recipe_sha": lock["recipe_sha"], "semantic_key": semantic_key(lock, platform),
            "engine_linkage": "static", "target_runtime_verified": False})
    profile = lock["platforms"][platform]["profile"]
    if profile == "static-third-party" and platform == "linux":
        require(pkgconf is not None, "static Linux requires target pkg-config resolution")
        static_dependencies.apply(prefix, dependencies, pkgconf)
    else:
        config = prefix / "share/cef-static/cef-static-config.cmake"
        with config.open("a", encoding="utf-8") as stream:
            stream.write('\nset(CEF_STATIC_DEPENDENCY_PROFILE "' + profile + '")\n')
    write_json(prefix / "share/cef-static/build-lock.json", lock)
    # This is an installed package, NOT a proof of this SDK consumer's runtime.
    write_json(prefix / "share/cef-static/package-contract.json", {"schema": 1, "triplet": PLATFORMS[platform],
        "profile": profile, "engine_linkage": "static", "capi_only": True, "configuration": "Release",
        "semantic_key": semantic_key(lock, platform), "consumer_verified": False})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--platform", choices=PLATFORMS, required=True)
    for field in ("prefix", "work", "downloads", "dependencies"):
        parser.add_argument("--" + field, type=Path, required=True)
    parser.add_argument("--pkgconf", type=Path)
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    install(read_json(args.lock), args.platform, args.prefix.resolve(), args.work.resolve(), args.downloads.resolve(),
            args.dependencies.resolve(), args.pkgconf, args.jobs)

if __name__ == "__main__":
    main()
