#!/usr/bin/env python3
"""One-time WPS Notes export -> Pluto Notes backup merger.

This tool never touches Pluto's SQLite database. It reads two ZIP archives,
builds a merged backup in memory, validates it against CloudBackupService's
current format, and only then atomically publishes a new ZIP.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import stat
import sys
import tempfile
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token


PLUTO_FORMAT_NAME = "pluto-notes-backup"
PLUTO_FORMAT_VERSION = 1
IMPORT_DEVICE_ID = "wps-import-once"
MAX_ARCHIVE_UNCOMPRESSED = 2 * 1024 * 1024 * 1024
MAX_ENTRY_UNCOMPRESSED = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 2000

# Never change these namespaces after the first release: UUID stability depends on them.
NOTE_NAMESPACE = uuid.UUID("85d8d9bf-0f73-56af-8974-23a1e02a4a78")
GROUP_NAMESPACE = uuid.UUID("c49f33cc-6a30-5ec6-9be2-acde0d5da9c7")
ATTACHMENT_NAMESPACE = uuid.UUID("6e4d04ce-71d4-583b-840d-113763314541")
CONFLICT_NAMESPACE = uuid.UUID("53525735-6a64-59be-8f60-b56365390fc5")

FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
CHECKBOX_RE = re.compile(r"^\[([ xX])\]\s+")
GENERATED_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")


class ImportToolError(RuntimeError):
    pass


class ArchiveValidationError(ImportToolError):
    pass


class NoteConversionError(ImportToolError):
    pass


@dataclass(frozen=True)
class ImageAsset:
    source_path: str
    file_name: str
    data: bytes
    sha256: str
    mime_type: str
    extension: str


@dataclass
class WpsNoteSource:
    metadata: dict[str, Any]
    front_matter: dict[str, Any]
    markdown: str
    images: dict[str, ImageAsset]
    fingerprint: str


@dataclass
class CandidateNote:
    note: dict[str, Any]
    attachments: list[dict[str, Any]]
    attachment_bytes: dict[str, bytes]


@dataclass
class ImportReport:
    new_groups: int = 0
    reused_groups: int = 0
    new_notes: int = 0
    skipped_notes: int = 0
    conflicts: int = 0
    attachments: int = 0
    skipped_unmapped_notes: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)
    group_mappings: list[dict[str, Any]] = field(default_factory=list)
    unmapped_groups: list[dict[str, Any]] = field(default_factory=list)
    conflict_items: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "new_groups": self.new_groups,
            "reused_groups": self.reused_groups,
            "new_notes": self.new_notes,
            "skipped_notes": self.skipped_notes,
            "conflicts": self.conflicts,
            "attachments": self.attachments,
            "skipped_unmapped_notes": self.skipped_unmapped_notes,
            "failure_count": len(self.failures),
            "failures": self.failures,
            "group_mappings": self.group_mappings,
            "unmapped_groups": self.unmapped_groups,
            "conflict_items": self.conflict_items,
        }


@dataclass
class MergeResult:
    manifest: dict[str, Any]
    data: dict[str, Any]
    files: dict[str, bytes]
    report: ImportReport


class SafeZip:
    """Read-only ZIP wrapper that rejects unsafe or ambiguous entries."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.zip = zipfile.ZipFile(path, "r")
        except (OSError, zipfile.BadZipFile) as error:
            raise ArchiveValidationError(f"ZIP 无效或已损坏：{path.name}") from error
        self.entries: dict[str, zipfile.ZipInfo] = {}
        total = 0
        try:
            for info in self.zip.infolist():
                name = validate_zip_name(info.filename)
                if name in self.entries:
                    raise ArchiveValidationError(f"ZIP 包含重复条目：{name}")
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise ArchiveValidationError(f"ZIP 不允许符号链接：{name}")
                if info.file_size > MAX_ENTRY_UNCOMPRESSED:
                    raise ArchiveValidationError(f"ZIP 条目过大：{name}")
                if (
                    info.compress_size > 0
                    and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
                ):
                    raise ArchiveValidationError(f"ZIP 条目压缩比异常：{name}")
                total += info.file_size
                if total > MAX_ARCHIVE_UNCOMPRESSED:
                    raise ArchiveValidationError("ZIP 解压后总体积超过安全上限。")
                self.entries[name] = info
            bad = self.zip.testzip()
            if bad is not None:
                raise ArchiveValidationError(f"ZIP CRC 校验失败：{bad}")
        except Exception:
            self.zip.close()
            raise

    def __enter__(self) -> "SafeZip":
        return self

    def __exit__(self, *_: object) -> None:
        self.zip.close()

    def has(self, name: str) -> bool:
        return validate_zip_name(name) in self.entries

    def read(self, name: str) -> bytes:
        safe = validate_zip_name(name)
        info = self.entries.get(safe)
        if info is None or info.is_dir():
            raise ArchiveValidationError(f"ZIP 条目不存在：{safe}")
        return self.zip.read(info)

    def names(self) -> list[str]:
        return list(self.entries)


def validate_zip_name(raw: str) -> str:
    if not raw or "\x00" in raw or "\\" in raw:
        raise ArchiveValidationError(f"ZIP 条目路径非法：{raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ArchiveValidationError(f"ZIP 条目路径非法：{raw!r}")
    if re.match(r"^[A-Za-z]:", raw) or any(part in ("", ".") for part in path.parts):
        raise ArchiveValidationError(f"ZIP 条目路径非法：{raw!r}")
    return path.as_posix()


def safe_join_zip(base_file: str, relative: str, root: str) -> str:
    if not relative or "\\" in relative or re.match(r"^[A-Za-z]:", relative):
        raise NoteConversionError(f"图片引用路径非法：{relative!r}")
    rel = PurePosixPath(relative)
    if rel.is_absolute():
        raise NoteConversionError(f"图片引用路径非法：{relative!r}")
    combined = PurePosixPath(base_file).parent.joinpath(rel)
    parts: list[str] = []
    for part in combined.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise NoteConversionError(f"图片引用越出导出包：{relative!r}")
            parts.pop()
        else:
            parts.append(part)
    normalized = PurePosixPath(*parts).as_posix()
    if root and normalized != root and not normalized.startswith(root + "/"):
        raise NoteConversionError(f"图片引用越出导出包：{relative!r}")
    return validate_zip_name(normalized)


def stable_note_uuid(wps_id: str) -> str:
    if not str(wps_id).strip():
        raise NoteConversionError("WPS 便签缺少 wps_id。")
    return str(uuid.uuid5(NOTE_NAMESPACE, str(wps_id).strip()))


def stable_group_uuid(group_name: str, variant: str = "") -> str:
    normalized = normalize_group_name(group_name)
    key = normalized if not variant else f"{variant}:{normalized}"
    return str(uuid.uuid5(GROUP_NAMESPACE, key))


def stable_attachment_uuid(note_id: str, source_path: str, digest: str) -> str:
    return str(uuid.uuid5(ATTACHMENT_NAMESPACE, f"{note_id}:{source_path}:{digest}"))


def normalize_group_name(value: Any) -> str:
    name = unicodedata.normalize("NFC", str(value or "").strip())
    return name or "未分组"


def utc_iso(value: Any, default_timezone: str = "Asia/Shanghai") -> str:
    if value in (None, ""):
        raise NoteConversionError("时间字段为空。")
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise NoteConversionError(f"时间格式无效：{value!r}") from error
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(default_timezone))
        except Exception as error:
            raise NoteConversionError(f"时区无效：{default_timezone!r}") from error
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def parse_iso(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ArchiveValidationError("备份时间字段无效。")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError as error:
        raise ArchiveValidationError(f"备份时间字段无效：{value!r}") from error


def parse_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    match = FRONT_MATTER_RE.match(raw.lstrip("\ufeff"))
    if not match:
        raise NoteConversionError("Markdown 缺少 YAML 元数据。")
    try:
        value = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as error:
        raise NoteConversionError("Markdown YAML 元数据无法解析。") from error
    if not isinstance(value, dict):
        raise NoteConversionError("Markdown YAML 元数据不是对象。")
    return dict(value), raw.lstrip("\ufeff")[match.end() :]


def strip_generated_title(markdown: str, title: str) -> str:
    lines = markdown.splitlines(keepends=True)
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return ""
    match = GENERATED_TITLE_RE.match(lines[first].strip())
    if match and match.group(1).strip() == title.strip():
        del lines[first]
        if first < len(lines) and not lines[first].strip():
            del lines[first]
    return "".join(lines)


def detect_image(data: bytes, source_name: str) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    guessed = mimetypes.guess_type(source_name)[0]
    raise NoteConversionError(
        f"图片内容无效或 Pluto 不支持（检测类型：{guessed or 'unknown'}）。"
    )


class DeltaBuilder:
    def __init__(self) -> None:
        self.ops: list[dict[str, Any]] = []

    def text(self, value: str, attributes: dict[str, Any] | None = None) -> None:
        if not value:
            return
        attrs = dict(attributes or {})
        if (
            self.ops
            and isinstance(self.ops[-1].get("insert"), str)
            and self.ops[-1].get("attributes", {}) == attrs
        ):
            self.ops[-1]["insert"] += value
            return
        op: dict[str, Any] = {"insert": value}
        if attrs:
            op["attributes"] = attrs
        self.ops.append(op)

    def embed(self, image_reference: str) -> None:
        self.ops.append({"insert": {"image": image_reference}})

    def newline(self, attributes: dict[str, Any] | None = None) -> None:
        self.text("\n", attributes)

    def finish(self) -> list[dict[str, Any]]:
        if not self.ops:
            self.newline()
        elif not (
            isinstance(self.ops[-1].get("insert"), str)
            and self.ops[-1]["insert"].endswith("\n")
        ):
            self.newline()
        return self.ops


def markdown_to_quill(
    markdown: str,
    image_handler: Callable[[str, str], str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    # A small extension used by some Markdown editors; raw <u> is also handled.
    markdown = re.sub(r"\+\+([^+\n]+)\+\+", r"<u>\1</u>", markdown)
    parser = MarkdownIt("commonmark", {"html": True}).enable("strikethrough")
    tokens = parser.parse(markdown)
    builder = DeltaBuilder()
    list_stack: list[str] = []
    item_stack: list[dict[str, Any]] = []
    heading_level: int | None = None

    def render_inline(children: list[Token], item: dict[str, Any] | None) -> None:
        active: dict[str, Any] = {}
        checkbox_checked = False
        for child in children:
            token_type = child.type
            if token_type == "strong_open":
                active["bold"] = True
            elif token_type == "strong_close":
                active.pop("bold", None)
            elif token_type == "em_open":
                active["italic"] = True
            elif token_type == "em_close":
                active.pop("italic", None)
            elif token_type == "s_open":
                active["strike"] = True
            elif token_type == "s_close":
                active.pop("strike", None)
            elif token_type == "link_open":
                active["link"] = child.attrGet("href") or ""
            elif token_type == "link_close":
                active.pop("link", None)
            elif token_type == "code_inline":
                builder.text(child.content, {**active, "code": True})
            elif token_type == "text":
                content = child.content
                if item is not None and not item["checkbox_scanned"]:
                    item["checkbox_scanned"] = True
                    check = CHECKBOX_RE.match(content)
                    if check:
                        item["checkbox"] = "checked" if check.group(1).lower() == "x" else "unchecked"
                        content = content[check.end() :]
                builder.text(content, active)
            elif token_type in ("softbreak", "hardbreak"):
                builder.text("\n", active)
            elif token_type == "image":
                source = child.attrGet("src") or ""
                alt = child.content or "图片"
                if image_handler is None:
                    raise NoteConversionError(f"Markdown 包含图片但未提供附件处理器：{source}")
                builder.embed(image_handler(source, alt))
            elif token_type == "html_inline":
                lower = child.content.strip().lower()
                if re.fullmatch(r"<u(?:\s[^>]*)?>", lower):
                    active["underline"] = True
                elif lower == "</u>":
                    active.pop("underline", None)
                elif re.fullmatch(r"<br\s*/?>", lower):
                    builder.text("\n", active)
                else:
                    # Ignore harmless formatting tags but never interpret scripts.
                    if re.search(r"<(script|iframe|object|embed)\b", lower):
                        raise NoteConversionError("Markdown 包含不安全的 HTML 组件。")
            elif token_type == "html_block":
                if re.search(r"<(script|iframe|object|embed)\b", child.content.lower()):
                    raise NoteConversionError("Markdown 包含不安全的 HTML 组件。")
            checkbox_checked = checkbox_checked or bool(item and item.get("checkbox"))

    for token in tokens:
        kind = token.type
        if kind == "bullet_list_open":
            list_stack.append("bullet")
        elif kind == "ordered_list_open":
            list_stack.append("ordered")
        elif kind in ("bullet_list_close", "ordered_list_close"):
            if list_stack:
                list_stack.pop()
        elif kind == "list_item_open":
            item_stack.append({"checkbox": None, "checkbox_scanned": False, "wrote_line": False})
        elif kind == "list_item_close":
            if item_stack:
                item_stack.pop()
        elif kind == "heading_open":
            heading_level = int(token.tag[1:]) if token.tag.startswith("h") else 1
        elif kind == "heading_close":
            builder.newline({"header": heading_level or 1})
            heading_level = None
        elif kind == "inline":
            item = item_stack[-1] if item_stack else None
            render_inline(token.children or [], item)
        elif kind == "paragraph_close":
            if item_stack and list_stack:
                item = item_stack[-1]
                attrs: dict[str, Any] = {"list": item.get("checkbox") or list_stack[-1]}
                if len(list_stack) > 1:
                    attrs["indent"] = len(list_stack) - 1
                builder.newline(attrs)
                item["wrote_line"] = True
            elif heading_level is None:
                builder.newline()
        elif kind == "fence" or kind == "code_block":
            lines = token.content.splitlines() or [""]
            for line in lines:
                builder.text(line)
                builder.newline({"code-block": True})
        elif kind == "blockquote_open":
            pass
        elif kind == "blockquote_close":
            pass
        elif kind == "hr":
            builder.text("---")
            builder.newline()
        elif kind == "html_block":
            if re.search(r"<(script|iframe|object|embed)\b", token.content.lower()):
                raise NoteConversionError("Markdown 包含不安全的 HTML 组件。")

    ops = builder.finish()
    plain = "".join(op["insert"] for op in ops if isinstance(op.get("insert"), str)).rstrip()
    return ops, plain


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_json(archive: SafeZip, name: str) -> dict[str, Any]:
    try:
        value = json.loads(archive.read(name).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArchiveValidationError(f"JSON 无法解析：{name}") from error
    if not isinstance(value, dict):
        raise ArchiveValidationError(f"JSON 顶层不是对象：{name}")
    return value


def validate_pluto_data(
    manifest: dict[str, Any],
    data: dict[str, Any],
    files: dict[str, bytes],
) -> None:
    if manifest.get("formatName") != PLUTO_FORMAT_NAME:
        raise ArchiveValidationError("不是 Pluto Notes 备份文件。")
    if int(manifest.get("backupFormatVersion", -1)) != PLUTO_FORMAT_VERSION:
        raise ArchiveValidationError("Pluto Notes 备份格式版本不受支持。")
    for key in ("groups", "notes", "attachments"):
        if not isinstance(data.get(key), list):
            raise ArchiveValidationError(f"Pluto 备份缺少列表：{key}")
    if not isinstance(data.get("settings", {}), dict):
        raise ArchiveValidationError("Pluto 备份 settings 无效。")

    groups = data["groups"]
    notes = data["notes"]
    attachments = data["attachments"]
    group_ids: set[str] = set()
    for group in groups:
        required = {"id", "name", "sortOrder", "createdAt", "updatedAt", "deletedAt", "revision"}
        if not isinstance(group, dict) or not required.issubset(group):
            raise ArchiveValidationError("Pluto 分组记录字段不完整。")
        group_id = group["id"]
        if not isinstance(group_id, str) or group_id in group_ids:
            raise ArchiveValidationError("Pluto 分组 ID 无效或重复。")
        uuid.UUID(group_id)
        if not isinstance(group["name"], str) or not group["name"].strip():
            raise ArchiveValidationError("Pluto 分组名称为空。")
        if not isinstance(group["sortOrder"], int) or int(group["revision"]) < 1:
            raise ArchiveValidationError("Pluto 分组排序或 revision 无效。")
        parse_iso(group["createdAt"])
        parse_iso(group["updatedAt"])
        if group["deletedAt"] is not None:
            parse_iso(group["deletedAt"])
        group_ids.add(group_id)

    note_ids: set[str] = set()
    image_refs: dict[str, set[str]] = {}
    for note in notes:
        required = {
            "id", "groupId", "title", "contentDelta", "plainText", "isPinned",
            "sortOrder", "createdAt", "updatedAt", "deletedAt", "revision",
            "syncStatus", "deviceId",
        }
        if not isinstance(note, dict) or not required.issubset(note):
            raise ArchiveValidationError("Pluto 便签记录字段不完整。")
        current_id = note["id"]
        if not isinstance(current_id, str) or current_id in note_ids:
            raise ArchiveValidationError("Pluto 便签 ID 无效或重复。")
        uuid.UUID(current_id)
        if note["groupId"] not in group_ids:
            raise ArchiveValidationError("备份中的便签分组引用无效。")
        if not isinstance(note["contentDelta"], str):
            raise ArchiveValidationError("Pluto contentDelta 不是字符串。")
        try:
            ops = json.loads(note["contentDelta"])
        except json.JSONDecodeError as error:
            raise ArchiveValidationError("Pluto contentDelta 无法解析。") from error
        if not isinstance(ops, list):
            raise ArchiveValidationError("Pluto contentDelta 不是操作数组。")
        references: set[str] = set()
        for op in ops:
            if not isinstance(op, dict) or "insert" not in op:
                raise ArchiveValidationError("Pluto Delta 操作无效。")
            inserted = op["insert"]
            if isinstance(inserted, dict) and "image" in inserted:
                image = inserted["image"]
                if not isinstance(image, str) or not image.startswith("attachment://"):
                    raise ArchiveValidationError("Pluto 图片 embed 引用无效。")
                references.add(image[13:])
        if not isinstance(note["plainText"], str) or not isinstance(note["isPinned"], bool):
            raise ArchiveValidationError("Pluto plainText 或 isPinned 类型无效。")
        if not isinstance(note["sortOrder"], int) or int(note["revision"]) < 1:
            raise ArchiveValidationError("Pluto 便签排序或 revision 无效。")
        if not isinstance(note["syncStatus"], str) or not isinstance(note["deviceId"], str):
            raise ArchiveValidationError("Pluto 便签同步字段无效。")
        parse_iso(note["createdAt"])
        parse_iso(note["updatedAt"])
        if note["deletedAt"] is not None:
            parse_iso(note["deletedAt"])
        note_ids.add(current_id)
        image_refs[current_id] = references

    attachment_ids: set[str] = set()
    attachments_by_note: dict[str, set[str]] = {}
    storage_keys: set[str] = set()
    for attachment in attachments:
        required = {
            "id", "noteId", "fileName", "mimeType", "storageKey", "sha256",
            "size", "createdAt", "deletedAt", "syncStatus", "archivePath",
        }
        if not isinstance(attachment, dict) or not required.issubset(attachment):
            raise ArchiveValidationError("Pluto 附件记录字段不完整。")
        attachment_id = attachment["id"]
        if not isinstance(attachment_id, str) or attachment_id in attachment_ids:
            raise ArchiveValidationError("Pluto 附件 ID 无效或重复。")
        uuid.UUID(attachment_id)
        if attachment["noteId"] not in note_ids:
            raise ArchiveValidationError("备份中的附件引用无效。")
        storage_key = attachment["storageKey"]
        if not isinstance(storage_key, str) or not storage_key:
            raise ArchiveValidationError("Pluto 附件 storageKey 无效。")
        if storage_key in storage_keys:
            raise ArchiveValidationError("Pluto 附件 storageKey 重复。")
        archive_path = attachment["archivePath"]
        if archive_path is not None:
            archive_path = validate_zip_name(archive_path)
            blob = files.get(archive_path)
            if blob is None:
                raise ArchiveValidationError("备份附件文件缺失。")
            if len(blob) != int(attachment["size"]):
                raise ArchiveValidationError("备份附件大小不匹配。")
            if hashlib.sha256(blob).hexdigest() != attachment["sha256"]:
                raise ArchiveValidationError("备份附件哈希不匹配。")
        parse_iso(attachment["createdAt"])
        if attachment["deletedAt"] is not None:
            parse_iso(attachment["deletedAt"])
        attachment_ids.add(attachment_id)
        storage_keys.add(storage_key)
        attachments_by_note.setdefault(attachment["noteId"], set()).add(attachment_id)

    for current_id, references in image_refs.items():
        if not references.issubset(attachments_by_note.get(current_id, set())):
            raise ArchiveValidationError("便签图片 embed 缺少对应附件记录。")

    active_notes = sum(note["deletedAt"] is None for note in notes)
    active_groups = sum(group["deletedAt"] is None for group in groups)
    expected = {
        "noteCount": active_notes,
        "groupCount": active_groups,
        "attachmentCount": len(attachments),
    }
    for key, value in expected.items():
        if int(manifest.get(key, -1)) != value:
            raise ArchiveValidationError(f"manifest {key} 与 data.json 不一致。")


def read_pluto_backup(path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    with SafeZip(path) as archive:
        manifest = _read_json(archive, "manifest.json")
        data = _read_json(archive, "data.json")
        files = {
            name: archive.read(name)
            for name, info in archive.entries.items()
            if not info.is_dir() and name not in {"manifest.json", "data.json"}
        }
    validate_pluto_data(manifest, data, files)
    return manifest, data, files


def locate_wps_root(archive: SafeZip) -> str:
    candidates: list[str] = []
    for name in archive.names():
        if name == "manifest.json" or name.endswith("/manifest.json"):
            try:
                value = json.loads(archive.read(name).decode("utf-8"))
            except Exception:
                continue
            if isinstance(value, dict) and "format_version" in value and "notes" in value:
                candidates.append(PurePosixPath(name).parent.as_posix())
    if len(candidates) != 1:
        raise ArchiveValidationError("WPS 导出包必须且只能包含一份 manifest.json。")
    return "" if candidates[0] == "." else candidates[0]


def prefixed(root: str, relative: str) -> str:
    return validate_zip_name(f"{root}/{relative}" if root else relative)


def load_wps_sources(
    path: Path,
) -> tuple[dict[str, Any], list[WpsNoteSource], list[dict[str, str]]]:
    with SafeZip(path) as archive:
        root = locate_wps_root(archive)
        manifest = _read_json(archive, prefixed(root, "manifest.json"))
        report_path = prefixed(root, "export-report.md")
        if not archive.has(report_path):
            raise ArchiveValidationError("WPS 导出包缺少 export-report.md。")
        if not isinstance(manifest.get("notes"), list):
            raise ArchiveValidationError("WPS manifest notes 无效。")
        timezone_name = str(manifest.get("timezone") or "Asia/Shanghai")
        sources: list[WpsNoteSource] = []
        failures: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for meta in manifest["notes"]:
            if not isinstance(meta, dict):
                raise ArchiveValidationError("WPS manifest 含无效便签记录。")
            current_wps_id = str(meta.get("wps_id") or "").strip()
            if not current_wps_id or current_wps_id in seen_ids:
                raise ArchiveValidationError("WPS manifest wps_id 为空或重复。")
            seen_ids.add(current_wps_id)
            try:
                relative_path = validate_zip_name(str(meta.get("path") or ""))
                markdown_path = prefixed(root, relative_path)
                try:
                    raw = archive.read(markdown_path).decode("utf-8-sig")
                except UnicodeDecodeError as error:
                    raise NoteConversionError("WPS Markdown 不是 UTF-8。") from error
                front, body = parse_front_matter(raw)
                if str(front.get("wps_id") or "") != current_wps_id:
                    raise NoteConversionError("WPS Markdown 与 manifest ID 不一致。")
                group_name = normalize_group_name(meta.get("group"))
                if normalize_group_name(front.get("group")) != group_name:
                    raise NoteConversionError("WPS Markdown 与 manifest 分组不一致。")
                if not isinstance(meta.get("pinned"), bool):
                    raise NoteConversionError("WPS manifest pinned 不是布尔值。")
                title = str(meta.get("title") or "")
                body = strip_generated_title(body, title)
                image_paths = extract_markdown_image_sources(body)
                images: dict[str, ImageAsset] = {}
                for source in image_paths:
                    resolved = safe_join_zip(markdown_path, source, root)
                    if not archive.has(resolved):
                        raise NoteConversionError(f"Markdown 图片引用不存在：{source!r}")
                    blob = archive.read(resolved)
                    mime_type, extension = detect_image(blob, resolved)
                    digest = hashlib.sha256(blob).hexdigest()
                    images[source] = ImageAsset(
                        source_path=resolved,
                        file_name=PurePosixPath(resolved).name,
                        data=blob,
                        sha256=digest,
                        mime_type=mime_type,
                        extension=extension,
                    )
                fingerprint = hashlib.sha256(
                    canonical_json(
                        {
                            "wps_id": current_wps_id,
                            "title": title,
                            "group": group_name,
                            "created_at": utc_iso(meta.get("created_at"), timezone_name),
                            "updated_at": utc_iso(meta.get("updated_at"), timezone_name),
                            "pinned": bool(meta.get("pinned")),
                            "markdown": body,
                            "images": {key: value.sha256 for key, value in sorted(images.items())},
                        }
                    ).encode("utf-8")
                ).hexdigest()
                sources.append(WpsNoteSource(dict(meta), front, body, images, fingerprint))
            except (ImportToolError, ValueError) as error:
                failures.append({"wps_id": current_wps_id, "reason": str(error)})
        expected = manifest.get("counts") or {}
        if int(expected.get("exported", len(sources) + len(failures))) != len(sources) + len(failures):
            raise ArchiveValidationError("WPS manifest 便签计数不一致。")
        return manifest, sources, failures


def extract_markdown_image_sources(markdown: str) -> list[str]:
    parser = MarkdownIt("commonmark", {"html": True}).enable("strikethrough")
    sources: list[str] = []
    for token in parser.parse(markdown):
        if token.type != "inline":
            continue
        for child in token.children or []:
            if child.type == "image":
                source = child.attrGet("src") or ""
                if source not in sources:
                    sources.append(source)
    return sources


def build_candidate(
    source: WpsNoteSource,
    note_uuid: str,
    group_id: str,
    timezone_name: str,
    conflict_copy: bool = False,
) -> CandidateNote:
    attachments: dict[str, dict[str, Any]] = {}
    attachment_bytes: dict[str, bytes] = {}
    created_at = utc_iso(source.metadata.get("created_at"), timezone_name)
    updated_at = utc_iso(source.metadata.get("updated_at"), timezone_name)

    def image_handler(source_path: str, _alt: str) -> str:
        asset = source.images.get(source_path)
        if asset is None:
            raise NoteConversionError(f"Markdown 图片引用无效：{source_path!r}")
        attachment_id = stable_attachment_uuid(note_uuid, asset.source_path, asset.sha256)
        archive_path = f"attachments/{attachment_id}{asset.extension}"
        attachments.setdefault(
            attachment_id,
            {
                "id": attachment_id,
                "noteId": note_uuid,
                "fileName": asset.file_name,
                "mimeType": asset.mime_type,
                "storageKey": attachment_id,
                "sha256": asset.sha256,
                "size": len(asset.data),
                "createdAt": created_at,
                "deletedAt": None,
                "syncStatus": "local",
                "archivePath": archive_path,
            },
        )
        attachment_bytes[archive_path] = asset.data
        return f"attachment://{attachment_id}"

    ops, plain_text = markdown_to_quill(source.markdown, image_handler)
    title = str(source.metadata.get("title") or "")
    if conflict_copy:
        title = f"{title}（WPS 导入冲突副本）"
    note = {
        "id": note_uuid,
        "groupId": group_id,
        "title": title,
        "contentDelta": json.dumps(ops, ensure_ascii=False, separators=(",", ":")),
        "plainText": plain_text,
        "isPinned": bool(source.metadata.get("pinned")),
        "sortOrder": 0,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "deletedAt": None,
        "revision": 1,
        "syncStatus": "local",
        "deviceId": IMPORT_DEVICE_ID,
    }
    return CandidateNote(note, list(attachments.values()), attachment_bytes)


def note_equivalent(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    keys = (
        "groupId", "title", "contentDelta", "plainText", "isPinned", "sortOrder",
        "createdAt", "updatedAt", "deletedAt", "revision", "syncStatus", "deviceId",
    )
    return all(existing.get(key) == candidate.get(key) for key in keys)


def attachments_equivalent(
    candidate: CandidateNote,
    existing_attachments: dict[str, dict[str, Any]],
    files: dict[str, bytes],
) -> bool:
    for expected in candidate.attachments:
        current = existing_attachments.get(expected["id"])
        if current != expected:
            return False
        if files.get(expected["archivePath"]) != candidate.attachment_bytes[expected["archivePath"]]:
            return False
    return True


def map_groups(
    data: dict[str, Any],
    sources: list[WpsNoteSource],
    group_name_mapping: Mapping[str, str],
    timezone_name: str,
    report: ImportReport,
) -> dict[str, str]:
    groups: list[dict[str, Any]] = data["groups"]
    existing_ids = {group["id"]: group for group in groups}
    active_by_name: dict[str, dict[str, Any]] = {}
    for group in groups:
        if group.get("deletedAt") is None:
            active_by_name.setdefault(normalize_group_name(group.get("name")), group)
    source_names = sorted(
        {normalize_group_name(source.metadata.get("group")) for source in sources}
    )
    target_names = sorted({group_name_mapping[name] for name in source_names})
    maximum_sort = max((int(group.get("sortOrder", 0)) for group in groups), default=-1)
    target_ids: dict[str, str] = {}
    target_actions: dict[str, str] = {}
    for offset, target_name in enumerate(target_names, start=1):
        existing = active_by_name.get(target_name)
        if existing is not None:
            target_ids[target_name] = existing["id"]
            target_actions[target_name] = "reused"
            report.reused_groups += 1
            continue
        group_id = stable_group_uuid(target_name)
        collision = existing_ids.get(group_id)
        if collision is not None:
            group_id = stable_group_uuid(target_name, "active-import")
            collision = existing_ids.get(group_id)
        if collision is not None:
            raise ImportToolError("稳定分组 UUID 与现有不同分组冲突。")
        group_sources = [
            source
            for source in sources
            if group_name_mapping[normalize_group_name(source.metadata.get("group"))]
            == target_name
        ]
        created = min(utc_iso(s.metadata.get("created_at"), timezone_name) for s in group_sources)
        updated = max(utc_iso(s.metadata.get("updated_at"), timezone_name) for s in group_sources)
        record = {
            "id": group_id,
            "name": target_name,
            "sortOrder": maximum_sort + offset,
            "createdAt": created,
            "updatedAt": updated,
            "deletedAt": None,
            "revision": 1,
        }
        groups.append(record)
        existing_ids[group_id] = record
        target_ids[target_name] = group_id
        target_actions[target_name] = "created"
        report.new_groups += 1

    result: dict[str, str] = {}
    for source_name in source_names:
        target_name = group_name_mapping[source_name]
        group_id = target_ids[target_name]
        result[source_name] = group_id
        report.group_mappings.append(
            {
                "wps_group": source_name,
                "pluto_group": target_name,
                "pluto_group_id": group_id,
                "action": target_actions[target_name],
            }
        )
    return result


def parse_group_mappings(values: Iterable[str]) -> dict[str, str]:
    """Parse repeated ``WPS_GROUP=PLUTO_GROUP`` command-line mappings."""
    result: dict[str, str] = {}
    for raw in values:
        source, separator, target = raw.partition("=")
        if not separator:
            raise ImportToolError(
                f"分组映射格式无效：{raw!r}；应为 WPS分组=Pluto分组。"
            )
        source_name = normalize_group_name(source)
        target_name = normalize_group_name(target)
        if source_name in result:
            raise ImportToolError(f"WPS 分组重复设置映射：{source_name}")
        result[source_name] = target_name
    return result


def select_mapped_sources(
    sources: list[WpsNoteSource],
    group_name_mapping: Mapping[str, str],
    report: ImportReport,
) -> list[WpsNoteSource]:
    normalized_mapping = {
        normalize_group_name(source): normalize_group_name(target)
        for source, target in group_name_mapping.items()
    }
    selected: list[WpsNoteSource] = []
    skipped_counts: dict[str, int] = {}
    for source in sources:
        source_name = normalize_group_name(source.metadata.get("group"))
        if source_name in normalized_mapping:
            selected.append(source)
        else:
            skipped_counts[source_name] = skipped_counts.get(source_name, 0) + 1
    report.skipped_unmapped_notes = sum(skipped_counts.values())
    report.unmapped_groups = [
        {"wps_group": name, "note_count": count, "action": "skipped"}
        for name, count in sorted(skipped_counts.items())
    ]
    return selected


def merge_backups(
    wps_path: Path,
    pluto_path: Path,
    group_name_mapping: Mapping[str, str] | None = None,
) -> MergeResult:
    original_manifest, original_data, original_files = read_pluto_backup(pluto_path)
    wps_manifest, sources, source_failures = load_wps_sources(wps_path)
    data = json.loads(json.dumps(original_data, ensure_ascii=False))
    files = dict(original_files)
    report = ImportReport()
    report.failures.extend(source_failures)
    timezone_name = str(wps_manifest.get("timezone") or "Asia/Shanghai")
    normalized_mapping = {
        normalize_group_name(source): normalize_group_name(target)
        for source, target in (group_name_mapping or {}).items()
    }
    sources = select_mapped_sources(sources, normalized_mapping, report)
    group_map = map_groups(data, sources, normalized_mapping, timezone_name, report)
    notes: list[dict[str, Any]] = data["notes"]
    attachments: list[dict[str, Any]] = data["attachments"]
    notes_by_id = {note["id"]: note for note in notes}
    attachments_by_id = {attachment["id"]: attachment for attachment in attachments}

    for source in sources:
        wps_id = str(source.metadata.get("wps_id") or "")
        try:
            group_name = normalize_group_name(source.metadata.get("group"))
            group_id = group_map[group_name]
            base_id = stable_note_uuid(wps_id)
            candidate = build_candidate(source, base_id, group_id, timezone_name)
            existing = notes_by_id.get(base_id)
            if existing is None:
                selected = candidate
            elif note_equivalent(existing, candidate.note) and attachments_equivalent(
                candidate, attachments_by_id, files
            ):
                report.skipped_notes += 1
                continue
            else:
                report.conflicts += 1
                conflict_id = str(uuid.uuid5(CONFLICT_NAMESPACE, f"{base_id}:{source.fingerprint}"))
                selected = build_candidate(
                    source, conflict_id, group_id, timezone_name, conflict_copy=True
                )
                conflict_existing = notes_by_id.get(conflict_id)
                report.conflict_items.append(
                    {
                        "wps_id": wps_id,
                        "existing_note_id": base_id,
                        "conflict_note_id": conflict_id,
                    }
                )
                if conflict_existing is not None:
                    if note_equivalent(conflict_existing, selected.note) and attachments_equivalent(
                        selected, attachments_by_id, files
                    ):
                        report.skipped_notes += 1
                        continue
                    raise NoteConversionError("稳定冲突副本 ID 已存在但内容不同。")

            for attachment in selected.attachments:
                collision = attachments_by_id.get(attachment["id"])
                if collision is not None and collision != attachment:
                    raise NoteConversionError("附件 UUID 与现有附件冲突。")
                archive_path = attachment["archivePath"]
                existing_blob = files.get(archive_path)
                if existing_blob is not None and existing_blob != selected.attachment_bytes[archive_path]:
                    raise NoteConversionError("附件归档路径与现有文件冲突。")
            notes.append(selected.note)
            notes_by_id[selected.note["id"]] = selected.note
            for attachment in selected.attachments:
                if attachment["id"] not in attachments_by_id:
                    attachments.append(attachment)
                    attachments_by_id[attachment["id"]] = attachment
                    files[attachment["archivePath"]] = selected.attachment_bytes[attachment["archivePath"]]
                    report.attachments += 1
            report.new_notes += 1
        except Exception as error:
            report.failures.append({"wps_id": wps_id, "reason": str(error)})

    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    manifest = dict(original_manifest)
    manifest.update(
        {
            "formatName": PLUTO_FORMAT_NAME,
            "backupFormatVersion": PLUTO_FORMAT_VERSION,
            "createdAt": created_at,
            "noteCount": sum(note.get("deletedAt") is None for note in notes),
            "groupCount": sum(group.get("deletedAt") is None for group in data["groups"]),
            "attachmentCount": len(attachments),
        }
    )
    validate_pluto_data(manifest, data, files)
    return MergeResult(manifest, data, files, report)


def report_markdown(report: ImportReport) -> str:
    lines = [
        "# WPS → Pluto Notes 导入报告",
        "",
        f"- 新增分组数：{report.new_groups}",
        f"- 复用分组数：{report.reused_groups}",
        f"- 新增便签数：{report.new_notes}",
        f"- 跳过便签数：{report.skipped_notes}",
        f"- 冲突数：{report.conflicts}",
        f"- 附件数：{report.attachments}",
        f"- 因未配置分组映射而跳过的便签数：{report.skipped_unmapped_notes}",
        f"- 失败数：{len(report.failures)}",
        "",
    ]
    if report.group_mappings:
        lines.extend(["## 分组映射", ""])
        for item in report.group_mappings:
            lines.append(
                f"- WPS `{item['wps_group']}` → Pluto `{item['pluto_group']}` "
                f"（{item['action']}，`{item['pluto_group_id']}`）"
            )
        lines.append("")
    if report.unmapped_groups:
        lines.extend(["## 未配置映射的分组", ""])
        for item in report.unmapped_groups:
            lines.append(
                f"- WPS `{item['wps_group']}`：跳过 {item['note_count']} 条便签"
            )
        lines.append("")
    if report.failures:
        lines.extend(["## 失败项", ""])
        for failure in report.failures:
            lines.append(f"- `{failure['wps_id']}`：{failure['reason']}")
        lines.append("")
    if report.conflict_items:
        lines.extend(["## 冲突副本", ""])
        for item in report.conflict_items:
            lines.append(
                f"- WPS `{item['wps_id']}`：保留 `{item['existing_note_id']}`，"
                f"新增/复用冲突副本 `{item['conflict_note_id']}`"
            )
        lines.append("")
    return "\n".join(lines)


def write_backup_atomic(result: MergeResult, output: Path) -> None:
    output = output.resolve()
    if output.exists():
        raise ImportToolError(f"输出文件已存在，拒绝覆盖：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    report_json = json.dumps(result.report.to_json(), ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    report_md = report_markdown(result.report).encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=output.name + ".",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, blob in sorted(result.files.items()):
                if name in {"manifest.json", "data.json", "import-report.json", "import-report.md"}:
                    continue
                archive.writestr(validate_zip_name(name), blob)
            archive.writestr(
                "manifest.json",
                json.dumps(result.manifest, ensure_ascii=False, separators=(",", ":")),
            )
            archive.writestr(
                "data.json",
                json.dumps(result.data, ensure_ascii=False, separators=(",", ":")),
            )
            archive.writestr("import-report.json", report_json)
            archive.writestr("import-report.md", report_md)
        # Re-open the exact bytes that will be published and run equivalent decode validation.
        read_pluto_backup(temporary)
        if result.report.failures:
            raise ImportToolError("存在导入失败项，拒绝生成正式备份；请先查看 dry-run 结果。")
        if output.exists():
            raise ImportToolError(f"输出文件在处理期间已出现，拒绝覆盖：{output}")
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="一次性把 WPS Markdown 导出包合并进现有 Pluto Notes 备份。"
    )
    parser.add_argument("--wps-export", required=True, type=Path, help="WPS 导出 ZIP")
    parser.add_argument("--pluto-backup", required=True, type=Path, help="现有 Pluto Notes 备份 ZIP")
    parser.add_argument("--output", required=True, type=Path, help="合并后的 Pluto Notes 备份 ZIP")
    parser.add_argument(
        "--group-map",
        action="append",
        default=[],
        metavar="WPS分组=Pluto分组",
        help="分组映射，可重复指定；未配置映射的 WPS 分组不会导入",
    )
    parser.add_argument("--dry-run", action="store_true", help="完整解析和校验，但不生成输出文件")
    return parser.parse_args(argv)


def log_report(report: ImportReport, dry_run: bool) -> None:
    prefix = "dry-run 完成" if dry_run else "合并完成"
    print(
        f"{prefix}：新增分组 {report.new_groups}，复用分组 {report.reused_groups}，"
        f"新增便签 {report.new_notes}，跳过 {report.skipped_notes}，"
        f"未映射过滤 {report.skipped_unmapped_notes}，冲突 {report.conflicts}，"
        f"附件 {report.attachments}，失败 {len(report.failures)}。",
        flush=True,
    )
    if report.failures:
        print("存在失败项；日志仅显示数量，未打印便签正文。", flush=True)


def main(argv: Iterable[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    try:
        wps_path = args.wps_export.expanduser().resolve(strict=True)
        pluto_path = args.pluto_backup.expanduser().resolve(strict=True)
        output = args.output.expanduser().resolve()
        group_name_mapping = parse_group_mappings(args.group_map)
        if output in {wps_path, pluto_path}:
            raise ImportToolError("输出路径不得与任一输入 ZIP 相同。")
        result = merge_backups(wps_path, pluto_path, group_name_mapping)
        if args.dry_run:
            log_report(result.report, True)
            return 0 if not result.report.failures else 1
        write_backup_atomic(result, output)
        log_report(result.report, False)
        print(f"输出：{output}", flush=True)
        return 0
    except (ImportToolError, OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"错误：{error}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
