#!/usr/bin/env python3
"""Inventory and copy a reviewed Fizgig dataset; Python standard library only.

Audit reports mechanical findings, never image quality or identity. Preparation
defaults to a dry run and only writes to a new destination when --apply is set.
Exit 0 means the operation completed; exit 2 means invalid input or an IO error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath, PureWindowsPath


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
SPLITS = {"train", "holdout", "rejected"}
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.I)


class DatasetError(ValueError):
    """An input failed validation before it could be used."""


def absolute(path):
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def is_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def no_links(path):
    for part in (path, *path.parents):
        if is_link(part):
            raise DatasetError("拒絕含 symlink／junction／reparse point 的路徑")


def under(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def source_root(value):
    root = absolute(value)
    no_links(root)
    if not root.is_dir():
        raise DatasetError("source 必須是已存在的目錄")
    return root


def source_file(root, name):
    if not isinstance(name, str) or not name or "\\" in name:
        raise DatasetError("image 必須是以 / 分隔的非空相對路徑")
    parts = name.split("/")
    if PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive:
        raise DatasetError("image 不接受絕對路徑或磁碟代號")
    for part in parts:
        if (part in ("", ".", "..") or part.endswith((".", " "))
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or RESERVED.fullmatch(part.split(".")[0])):
            raise DatasetError("image 路徑含不可攜名稱或路徑跳脫")
    path = root.joinpath(*parts)
    no_links(path)
    if not under(path.resolve(), root.resolve()) or not path.is_file():
        raise DatasetError("image 必須指向 source 內的普通檔案")
    return path


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def auxiliary(path):
    name = path.stem.casefold()
    if name.endswith("-masklabel"):
        return "mask"
    if name.startswith("_") or any(x in name for x in ("contact_sheet", "contact-sheet", "contactsheet")):
        return "auxiliary_image"
    return None


def caption_for(path):
    sidecar = path.with_suffix(".txt")
    no_links(sidecar)
    if not sidecar.exists():
        return None
    if not sidecar.is_file():
        raise DatasetError("caption sidecar 不是普通檔案")
    return sidecar.read_text(encoding="utf-8-sig").strip()


def dimensions(path):
    """Read PNG/JPEG headers only. This is not full image decoding/validation."""
    with path.open("rb") as handle:
        head = handle.read(24)
        if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR" and len(head) == 24:
            return list(struct.unpack(">II", head[16:24]))
        if not head.startswith(b"\xff\xd8"):
            return None
        handle.seek(2)
        while handle.tell() < 1024 * 1024:
            if handle.read(1) != b"\xff":
                return None
            marker = handle.read(1)
            while marker == b"\xff":
                marker = handle.read(1)
            if not marker or marker in (b"\xd9", b"\xda"):
                return None
            if marker[0] in (0x01, *range(0xD0, 0xD9)):
                continue
            raw = handle.read(2)
            if len(raw) != 2:
                return None
            size = struct.unpack(">H", raw)[0]
            if size < 2:
                return None
            if marker[0] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                frame = handle.read(5)
                if len(frame) == 5:
                    height, width = struct.unpack(">HH", frame[1:5])
                    return [width, height]
                return None
            handle.seek(size - 2, 1)
    return None


def audit(root):
    items, findings, hashes = [], [], defaultdict(list)
    def walk_error(error):
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        current = Path(directory)
        dirs.sort()
        for name in list(dirs):
            if is_link(current / name):
                findings.append({"code": "skipped_link", "path": (current / name).relative_to(root).as_posix()})
                dirs.remove(name)
        for name in sorted(files):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if is_link(path):
                findings.append({"code": "skipped_link", "path": relative})
                continue
            if not path.is_file():
                findings.append({"code": "skipped_non_file", "path": relative})
                continue
            if path.suffix.lower() not in IMAGE_EXTENSIONS:
                if path.suffix.lower() != ".txt":
                    findings.append({"code": "non_image_file", "path": relative})
                continue
            kind = auxiliary(path) or "image"
            record = {"image": relative, "kind": kind, "bytes": path.stat().st_size, "sha256": digest(path)}
            record["dimensions"] = dimensions(path)
            if kind != "image":
                findings.append({"code": kind, "path": relative})
            else:
                hashes[record["sha256"]].append(relative)
                try:
                    caption = caption_for(path)
                    record["caption_status"] = "missing" if caption is None else "empty" if not caption else "present"
                except (UnicodeError, DatasetError):
                    record["caption_status"] = "unreadable"
                if record["caption_status"] != "present":
                    findings.append({"code": "caption_" + record["caption_status"], "path": relative})
                if not record["dimensions"]:
                    findings.append({"code": "dimensions_unknown", "path": relative})
            items.append(record)
    duplicates = [names for names in hashes.values() if len(names) > 1]
    return {"version": 1, "mode": "audit", "status": "complete", "items": items,
            "findings": findings, "exact_duplicates": duplicates,
            "counts": dict(Counter(item["kind"] for item in items)),
            "limits": ["僅盤點檔案與 PNG/JPEG 尺寸標頭；未完整解碼圖片。",
                       "人物身分、模糊、美顏、浮水印、近重複與素材多樣性需要視覺檢查。"]}


def plan(root, manifest_path, destination):
    destination = absolute(destination)
    no_links(destination)
    if under(destination, root) or under(root, destination):
        raise DatasetError("destination 與 source 不可重疊")
    if destination.exists():
        raise DatasetError("destination 已存在；請指定新目錄，不覆寫")
    if not destination.parent.is_dir():
        raise DatasetError("destination 的父目錄必須已存在")
    manifest_path = absolute(manifest_path)
    no_links(manifest_path)
    doc = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if (not isinstance(doc, dict) or type(doc.get("version")) is not int
            or doc["version"] != 1 or not isinstance(doc.get("items"), list)):
        raise DatasetError("manifest 需要 version: 1 與 items 陣列")
    if not doc["items"]:
        raise DatasetError("manifest items 不可為空")
    names, seen, groups, hashes, prepared = set(), set(), defaultdict(set), defaultdict(set), []
    for entry in doc["items"]:
        if not isinstance(entry, dict):
            raise DatasetError("items 內容必須是物件")
        if set(entry) - {"image", "split", "group", "caption", "reason", "sha256"}:
            raise DatasetError("manifest item 含未知欄位")
        path = source_file(root, entry.get("image"))
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise DatasetError("manifest image 副檔名不支援")
        split, group = entry.get("split"), entry.get("group")
        if (not isinstance(split, str) or split not in SPLITS
                or not isinstance(group, str) or not group.strip()):
            raise DatasetError("每張圖需要 split 與非空的拍攝來源 group")
        if auxiliary(path) and split != "rejected":
            raise DatasetError("mask／輔助圖片不能當 train 或 holdout 素材")
        key, stem = entry["image"].casefold(), path.stem.casefold()
        if key in seen or stem in names:
            raise DatasetError("重複選取或跨來源檔名 stem 碰撞（不分大小寫）")
        seen.add(key)
        names.add(stem)
        content_hash = digest(path)
        if "sha256" in entry and entry["sha256"] != content_hash:
            raise DatasetError("圖片內容與 manifest sha256 不符，需重新檢查")
        if "caption" in entry:
            caption = entry["caption"]
            if not isinstance(caption, str):
                raise DatasetError("caption 覆蓋值必須是字串")
            caption = caption.strip()
        else:
            caption = caption_for(path)
        if split == "train" and not caption:
            raise DatasetError("每張 train 圖都需要非空 UTF-8 caption")
        if "reason" in entry and not isinstance(entry["reason"], str):
            raise DatasetError("reason 必須是字串")
        if split in {"train", "holdout"}:
            groups[group.strip()].add(split)
            hashes[content_hash].add(split)
        prepared.append({"image": entry["image"], "split": split, "group": group.strip(),
                         "caption": caption, "reason": entry.get("reason", ""), "sha256": content_hash,
                         "output": split + "/" + path.name})
    if any(len(splits) > 1 for splits in groups.values()):
        raise DatasetError("同一拍攝來源 group 跨越 train／holdout，會造成驗證洩漏")
    if any(len(splits) > 1 for splits in hashes.values()):
        raise DatasetError("相同圖片內容跨越 train／holdout，會造成驗證洩漏")
    if not any(item["split"] == "train" for item in prepared):
        raise DatasetError("manifest 至少需要一張 train 圖")
    return {"version": 1, "mode": "prepare", "status": "planned", "items": prepared,
            "counts": dict(Counter(item["split"] for item in prepared))}


def apply_plan(root, destination, prepared):
    """Copy only after full validation; leave partial output on IO failure."""
    destination = absolute(destination)
    no_links(destination)
    destination.mkdir(exist_ok=False)
    for split in sorted(SPLITS):
        (destination / split).mkdir()
    for entry in prepared["items"]:
        source = source_file(root, entry["image"])
        target = destination / entry["output"]
        no_links(target)
        copied_hash = hashlib.sha256()
        with source.open("rb") as reader, target.open("xb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(chunk)
                copied_hash.update(chunk)
        if copied_hash.hexdigest() != entry["sha256"]:
            raise DatasetError("來源在複製期間變更；保留未完成 destination，請重新檢查")
        if entry["caption"] is not None:
            sidecar = target.with_suffix(".txt")
            no_links(sidecar)
            with sidecar.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(entry["caption"] + "\n")
    prepared = {**prepared, "status": "prepared"}
    write_json(destination / "manifest.json", prepared)
    return prepared


def write_json(path, value):
    path = absolute(path)
    no_links(path)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="盤點及複製已人工選定的 Fizgig 資料集；不連網、不改原檔。")
    sub = parser.add_subparsers(dest="mode", required=True)
    audit_parser = sub.add_parser("audit", help="盤點圖片、caption、精確重複與輔助檔案")
    audit_parser.add_argument("--source", required=True)
    audit_parser.add_argument("--output", help="新 JSON 報告路徑，必須在 source 外；省略則只印 stdout")
    prepare_parser = sub.add_parser("prepare", help="依 manifest 預檢與規劃；--apply 才複製")
    prepare_parser.add_argument("--source", required=True)
    prepare_parser.add_argument("--manifest", required=True)
    prepare_parser.add_argument("--destination", required=True)
    prepare_parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = source_root(args.source)
        if args.mode == "audit":
            output = absolute(args.output) if args.output else None
            if output and under(output, root):
                raise DatasetError("audit report 必須放在 source 外")
            result = audit(root)
            if output:
                write_json(output, result)
        else:
            result = plan(root, args.manifest, args.destination)
            if args.apply:
                result = apply_plan(root, args.destination, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DatasetError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
