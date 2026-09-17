#!/usr/bin/env python3
"""Import a hash-locked, verified CEF SDK; never certify a new native build.

Only the CEF-owned package subtree is installed. Old vcpkg status databases,
triplets and toolchains are not transplanted. Downloaded Python is data only.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import urllib.parse
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cef_sdk_validator", HERE.parent / "static/sdk_package.py")
assert spec and spec.loader
sdk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sdk)
TRIPLETS = {"x64-linux-static-release": "x64-linux", "x64-windows-static-release": "x64-windows-static"}
MAX_UNPACKED = 12 * 1024**3
MAX_JSON = 32 * 1024**2
REPOSITORY = "dobord/cef"
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def require(value: bool, message: str) -> None:
    if not value:
        raise ValueError(message)


def read_json(path: Path) -> dict:
    value = sdk.read_json(path)
    require(isinstance(value, dict), "Expected a JSON object")
    return value


def validate_record(record: dict, *, json_file: bool = False) -> None:
    require(isinstance(record, dict) and set(record) == {"name", "size", "sha256"}, "Invalid asset record")
    sdk.safe_name(record["name"])
    limit = MAX_JSON if json_file else sdk.ASSET_LIMIT - 1
    require(type(record["size"]) is int and 0 < record["size"] <= limit, "Invalid asset size")
    require(isinstance(record["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is not None,
            "Invalid asset hash")


def validate_lock(lock: dict) -> None:
    require(isinstance(lock, dict) and set(lock) == {"schema", "tag", "tested_commit", "platforms"}, "Invalid SDK lock fields")
    require(type(lock["schema"]) is int and lock["schema"] == 1, "Unsupported SDK lock schema")
    sdk.safe_name(lock["tag"])
    require(isinstance(lock["tested_commit"], str) and re.fullmatch(r"[0-9a-f]{40}", lock["tested_commit"]) is not None,
            "A full tested commit is required")
    require(isinstance(lock["platforms"], dict) and set(lock["platforms"]) == set(TRIPLETS), "Both target triplets are required")
    for triplet, entry in lock["platforms"].items():
        require(isinstance(entry, dict) and set(entry) == {"source_triplet", "manifest"}, "Invalid platform lock")
        require(entry["source_triplet"] == TRIPLETS[triplet], "Wrong source triplet")
        validate_record(entry["manifest"], json_file=True)
        require(entry["manifest"]["name"].endswith(".manifest.json"), "Not an SDK manifest")


class ReleaseRedirects(urllib.request.HTTPRedirectHandler):
    """Never redirect a public download to a local/credentialed endpoint."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        require(target.scheme == "https" and target.hostname in {
            "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"
        } and target.port in (None, 443) and not target.username and not target.password,
                "Untrusted release redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_file(path: Path, record: dict) -> None:
    validate_record(record)
    require(path.is_file() and not path.is_symlink(), "Missing or redirected release asset")
    require(path.stat().st_size == record["size"] and sdk.hash_file(path) == record["sha256"], "Release asset digest/size mismatch")


def acquire(record: dict, directory: Path, tag: str) -> Path:
    validate_record(record)
    sdk.safe_name(tag)
    directory.mkdir(parents=True, exist_ok=True)
    require(not directory.is_symlink(), "Redirected download directory")
    path = directory / record["name"]
    if path.exists() or path.is_symlink():
        check_file(path, record)
        return path
    url = "https://github.com/" + REPOSITORY + "/releases/download/" + tag + "/" + record["name"]
    fd, temporary_name = tempfile.mkstemp(prefix=".cef-download-", dir=directory)
    temporary = Path(temporary_name)
    try:
        opener = urllib.request.build_opener(ReleaseRedirects())
        request = urllib.request.Request(url, headers={"User-Agent": "cef-locked-sdk-import/1"})
        with os.fdopen(fd, "wb") as output, opener.open(request, timeout=120) as response:
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                require(count <= record["size"], "Release asset exceeds its locked size")
                output.write(chunk)
        check_file(temporary, record)
        if path.exists():
            check_file(path, record)
        else:
            os.replace(temporary, path)
        return path
    finally:
        temporary.unlink(missing_ok=True)


def member_path(name: str) -> PurePosixPath:
    require(isinstance(name, str) and not any(c in name for c in '\\:\"<>|?*')
            and not any(ord(c) < 32 or ord(c) == 127 for c in name), "Unsafe SDK member")
    parts = name.split("/")
    require(0 < len(parts) <= 64 and len(name) <= 4096 and all(
        p not in ("", ".", "..") and not p.endswith((" ", ".")) and not RESERVED.match(p) for p in parts),
        "Unsafe SDK member path")
    return PurePosixPath(name)


def selected_member(name: str, package_name: str, source_triplet: str) -> PurePosixPath | None:
    path = member_path(name)
    prefix = (package_name, "installed", source_triplet)
    if tuple(path.parts[:3]) != prefix:
        return None
    relative = PurePosixPath(*path.parts[3:])
    if tuple(relative.parts[:2]) not in {
        ("include", "cef-static"), ("lib", "cef-static"), ("share", "cef-static")
    }:
        raise ValueError("Unexpected file in CEF package prefix: " + str(relative))
    require(relative.suffix.lower() not in {".c", ".cc", ".cpp", ".cxx", ".pdb", ".o", ".obj", ".dll", ".so"},
            "Implementation, debug or shared runtime file in SDK payload")
    require(not any(p.casefold() in {".git", "downloads", "buildtrees"} for p in relative.parts),
            "Workspace data in CEF payload: " + str(relative))
    return relative


def install_bundle(lock: dict, triplet: str, directory: Path, destination: Path, *, required_profile: str = "engine-static") -> dict:
    validate_lock(lock)
    require(triplet in TRIPLETS, "Unsupported native Release triplet")
    # No receipt from the existing release proves closure of every platform dependency.
    require(required_profile == "engine-static", "This release importer cannot certify static-third-party or fully-static profiles")
    entry = lock["platforms"][triplet]
    manifest_path = directory / entry["manifest"]["name"]
    check_file(manifest_path, entry["manifest"])
    manifest = read_json(manifest_path)
    require(isinstance(manifest.get("entries"), list) and 0 < len(manifest["entries"]) <= 100000, "Invalid SDK inventory")
    total = 0
    for item in manifest["entries"]:
        require(isinstance(item, dict) and type(item.get("size")) is int and item["size"] >= 0, "Invalid member size")
        total += item["size"]
        require(total <= MAX_UNPACKED, "SDK exceeds unpacked size budget")
        member_path(item["path"])
    verified = sdk.verify_bundle(manifest_path, strict=False)
    receipt = verified["receipt"]
    sdk.verify_receipt(receipt, lock["tested_commit"], entry["source_triplet"])
    require(not destination.is_symlink(), "Redirected package destination")
    require(not destination.exists() or (destination.is_dir() and not any(destination.iterdir())), "Package destination must be empty")
    destination.parent.mkdir(parents=True, exist_ok=True)
    require(shutil.disk_usage(destination.parent).free >= total + 256 * 1024**2, "Insufficient package disk space")
    paths = [directory / part["name"] for part in manifest["parts"]]
    with tempfile.TemporaryDirectory(prefix=".cef-install-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "package"
        stage.mkdir()
        count = 0
        with sdk.PartsReader(paths) as stream, zipfile.ZipFile(stream) as archive:
            for info in archive.infolist():
                relative = selected_member(info.filename, manifest["name"], entry["source_triplet"])
                if relative is None:
                    continue
                target = stage.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                target.chmod(0o755 if info.external_attr >> 16 & 0o111 else 0o644)
                if target.suffix.lower() in {".a", ".lib", ".rlib"}:
                    with target.open("rb") as source:
                        require(source.read(8) == b"!<arch>\n", "Non-relocatable archive in SDK")
                count += 1
        for name in ("share/cef-static/cef-static-config.cmake", "share/cef-static/copyright",
                     "share/cef-static/static-link-inventory.json", "include/cef-static/include/capi/cef_app_capi.h",
                     "share/cef-static/resources/icudtl.dat"):
            require((stage / name).is_file(), "Missing required CEF payload file: " + name)
        require(any((stage / "lib/cef-static").glob("*.a")) or any((stage / "lib/cef-static").glob("*.lib")), "Missing CEF static archives")
        result = {"schema": 1, "mode": "release-import", "release_tag": lock["tag"],
                  "tested_commit": lock["tested_commit"], "source_triplet": entry["source_triplet"], "target_triplet": triplet,
                  "manifest_sha256": entry["manifest"]["sha256"], "archive_sha256": manifest["archive"]["sha256"],
                  "engine_linkage": "static", "capi_only": True, "configuration": "Release",
                  "system_libraries_static": receipt["system_libraries_static"],
                  "sandbox_verified": receipt["sandbox_verified"], "imported_files": count,
                  "consumer_requalification_required": True}
        (stage / "share/cef-static/upstream-sdk-receipt.json").write_bytes(sdk.json_bytes(receipt))
        (stage / "share/cef-static/acquisition.json").write_bytes(sdk.json_bytes(result))
        if destination.exists():
            destination.rmdir()
        stage.rename(destination)
    return result


def obtain(lock: dict, triplet: str, cache: Path) -> Path:
    validate_lock(lock)
    require(triplet in TRIPLETS, "Unsupported triplet")
    record = lock["platforms"][triplet]["manifest"]
    directory = cache / record["sha256"]
    manifest_path = acquire(record, directory, lock["tag"])
    manifest = read_json(manifest_path)
    require(isinstance(manifest.get("parts"), list) and 0 < len(manifest["parts"]) <= sdk.MAX_PARTS,
            "Invalid transport parts")
    require(isinstance(manifest.get("helpers"), list) and len(manifest["helpers"]) == 2, "Invalid helper inventory")
    for asset in [manifest["receipt"], *manifest["helpers"], *manifest["parts"]]:
        acquire(asset, directory, lock["tag"])
    return directory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--triplet", choices=TRIPLETS, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--required-profile", choices=("engine-static", "static-third-party", "fully-static"), required=True)
    args = parser.parse_args()
    require((os.name == "nt") == (args.triplet == "x64-windows-static-release"), "A native host is required")
    require(args.required_profile == "engine-static", "Release payload is not certified for the requested strict profile")
    lock = read_json(args.lock)
    directory = obtain(lock, args.triplet, args.cache.resolve())
    install_bundle(lock, args.triplet, directory, args.prefix.resolve(), required_profile=args.required_profile)


if __name__ == "__main__":
    main()
