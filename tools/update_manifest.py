#!/usr/bin/env python3
"""원격 콘텐츠 저장소(ksafetrip-content)의 manifest.json 을 갱신해요.

사용법:
    python3 update_manifest.py <저장소 폴더> [--min-version N]

- 폴더 안 콘텐츠 JSON(manifest 에 이미 있는 이름 + 알려진 이름) 의 sha256 을 다시 계산해요.
- 내용이 바뀐 파일만 그 파일의 version 을 +1 하고, 하나라도 바뀌면 contentVersion 을 +1 해요.
- --min-version: contentVersion 이 이 값보다 커지도록 맞춰요.
  앱 번들의 contentVersion 보다 원격이 커야 앱이 "새 콘텐츠" 로 받아 가기 때문이에요 (export_content.sh 가 넘겨 줘요).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

KNOWN_FILES = ["etiquette", "transport", "phrases", "restaurants", "emergency",
               "places", "events", "specialties", "moderation"]
KST = dt.timezone(dt.timedelta(hours=9))


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def update(folder: Path, min_version: int = 0, today: str | None = None) -> bool:
    """manifest 를 갱신하고, 바뀐 것이 있으면 True."""
    path = folder / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "contentVersion": 0, "minAppVersion": "1.0", "updatedAt": "", "files": {},
    }
    files = manifest.setdefault("files", {})
    names = sorted(set(files) | {name for name in KNOWN_FILES if (folder / f"{name}.json").exists()})

    changed = False
    for name in names:
        file_path = folder / f"{name}.json"
        if not file_path.exists():
            continue
        digest = sha256_of(file_path)
        entry = files.get(name)
        if entry and entry.get("sha256") == digest:
            continue
        files[name] = {"version": (entry or {}).get("version", 0) + 1, "sha256": digest}
        changed = True

    version = int(manifest.get("contentVersion", 0))
    if changed:
        version += 1
    if version <= min_version:
        version = min_version + 1
        changed = True
    if not changed:
        return False
    manifest["contentVersion"] = version
    manifest["updatedAt"] = today or dt.datetime.now(KST).date().isoformat()
    # 앱의 ContentManifest 와 같은 모양, 파일 순서 고정
    manifest["files"] = {name: files[name] for name in sorted(files)}
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"manifest.json 갱신 (contentVersion = {version})")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder")
    parser.add_argument("--min-version", type=int, default=0)
    args = parser.parse_args()
    if not update(Path(args.folder), args.min_version):
        print("manifest.json: 바뀐 내용 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
