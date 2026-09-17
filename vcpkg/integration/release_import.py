"""Import a pinned, verified CEF release payload; never execute downloaded helpers."""
from __future__ import annotations
import importlib.util
import os
import re
from pathlib import Path
import shutil
import stat
import tempfile
import urllib.parse
import urllib.request
import zipfile
from common import require, regular, relative, digest, validate_lock, write_json

ROOT = Path(__file__).resolve().parents[1]


def validator():
    spec = importlib.util.spec_from_file_location("pinned_cef_sdk_package", ROOT / "static/sdk_package.py")
    require(spec is not None and spec.loader is not None, "missing canonical SDK validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fetch_assets(release: dict, downloads: Path) -> Path:
    """Public HTTPS only; hashes are committed inputs, not values trusted from a tag."""
    folder = downloads / ("cef-" + release["manifest_sha256"])
    folder.mkdir(parents=True, exist_ok=True)
    for asset in release["assets"]:
        path = folder / asset["name"]
        if path.exists():
            require(regular(path) and path.stat().st_size == asset["size"] and digest(path) == asset["sha256"], "cached CEF asset does not match lock")
            continue
        url = "https://github.com/dobord/cef/releases/download/" + urllib.parse.quote(release["tag"], safe="") + "/" + urllib.parse.quote(asset["name"], safe="")
        fd, temporary_name = tempfile.mkstemp(prefix=".download-", dir=folder)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "cef-static-vcpkg/1"}), timeout=120) as response:
                    require(urllib.parse.urlsplit(response.geturl()).scheme == "https", "insecure asset redirect")
                    count = 0
                    while block := response.read(1024 * 1024):
                        count += len(block)
                        require(count <= asset["size"], "oversized CEF asset")
                        stream.write(block)
            require(temporary.stat().st_size == asset["size"] and digest(temporary) == asset["sha256"], "CEF asset integrity failure")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return folder


def selected_payload(name: str, bundle: str, source_triplet: str) -> str | None:
    path = relative(name)
    prefix = (bundle, "installed", source_triplet)
    if path.parts[:3] != prefix:
        return None
    parts = path.parts[3:]
    if len(parts) < 3 or parts[:2] not in (("include", "cef-static"), ("lib", "cef-static"), ("share", "cef-static")):
        return None
    result = "/".join(parts)
    require(not re.search(r"(?i)\.(?:dll|dylib|so)(?:\.|$)", result) and not result.lower().endswith((".pdb", ".c", ".cc", ".cpp", ".cxx")), "forbidden CEF SDK payload")
    return result


def import_release(lock: dict, platform: str, downloads: Path, prefix: Path) -> dict:
    validate_lock(lock)
    require(lock["acquisition"] == "release", "not a release acquisition")
    from common import SOURCE_TRIPLETS, semantic_key
    release = lock["platforms"][platform]["release"]
    folder = fetch_assets(release, downloads)
    module = validator()
    manifest_path = folder / (release["name"] + ".manifest.json")
    require(digest(manifest_path) == release["manifest_sha256"], "manifest changed")
    verified = module.verify_bundle(manifest_path)
    manifest, receipt = verified["manifest"], verified["receipt"]
    module.verify_receipt(receipt, release["commit"], SOURCE_TRIPLETS[platform])
    require(receipt["cef_commit"] == lock["cef_sha"] and receipt["chromium_commit"] == lock["chromium_sha"], "release source mismatch")
    require(not prefix.exists() or not any(prefix.iterdir()), "CEF package destination must be empty")
    prefix.mkdir(parents=True, exist_ok=True)
    records, seen = [], {}
    paths = [folder / p["name"] for p in manifest["parts"]]
    # verify_bundle already hashes every member and verifies CRCs; extraction is
    # still bounded and validates paths/types independently before creating files.
    with module.PartsReader(paths) as parts, zipfile.ZipFile(parts) as archive:
        selected = []
        total = 0
        for item in archive.infolist():
            if item.is_dir():
                continue
            name = selected_payload(item.filename, manifest["name"], SOURCE_TRIPLETS[platform])
            if name is None:
                continue
            kind = stat.S_IFMT(item.external_attr >> 16)
            require(kind in (0, stat.S_IFREG) and not item.flag_bits & 1, "unsupported SDK member")
            components = relative(name).parts
            for i in range(1, len(components) + 1):
                exact = components[:i]
                folded = tuple(p.casefold() for p in exact)
                require(folded not in seen or seen[folded] == exact, "SDK case alias")
                seen[folded] = exact
            total += item.file_size
            require(0 <= item.file_size <= 4 * 1024**3 and total <= 32 * 1024**3 and len(selected) < 100000, "SDK payload limits")
            selected.append((item, name))
        require(selected, "no CEF payload in verified SDK")
        for item, name in selected:
            destination = prefix / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
            destination.chmod(0o644)
            records.append({"path": name, "size": destination.stat().st_size, "sha256": digest(destination)})
    require((prefix / "share/cef-static/cef-static-config.cmake").is_file(), "CEF CMake package missing")
    require((prefix / "share/cef-static/resources/icudtl.dat").is_file(), "CEF ICU data missing")
    evidence = {"schema": 1, "kind": "release-import", "semantic_key": semantic_key(lock, platform),
                "producer_commit": release["commit"], "original_triplet": SOURCE_TRIPLETS[platform],
                "target_triplet": lock["platforms"][platform]["triplet"], "upstream_manifest_sha256": release["manifest_sha256"],
                "engine_linkage": "static", "target_runtime_verified": False, "files": records}
    share = prefix / "share/cef-static"
    write_json(share / "upstream-sdk-receipt.json", receipt)
    write_json(share / "acquisition-receipt.json", evidence)
    return evidence
