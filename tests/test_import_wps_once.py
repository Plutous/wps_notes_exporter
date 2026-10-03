import base64
import json
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

import yaml

from import_wps_once import (
    ArchiveValidationError,
    GROUP_NAMESPACE,
    NOTE_NAMESPACE,
    load_wps_sources,
    markdown_to_quill,
    merge_backups,
    parse_group_mappings,
    read_pluto_backup,
    stable_group_uuid,
    stable_note_uuid,
    utc_iso,
    validate_pluto_data,
    write_backup_atomic,
)


TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nXsAAAAASUVORK5CYII="
)
GROUP_ID = "f3be574f-63b1-42d5-9ca6-4cdda91c1cba"
NOTE_ID = "8d644d34-0549-4430-b595-4a599f09a366"


def base_group(name="现有分组"):
    return {
        "id": GROUP_ID,
        "name": name,
        "sortOrder": 0,
        "createdAt": "2026-01-01T00:00:00.000000Z",
        "updatedAt": "2026-01-01T00:00:00.000000Z",
        "deletedAt": None,
        "revision": 1,
    }


def base_note():
    return {
        "id": NOTE_ID,
        "groupId": GROUP_ID,
        "title": "已有便签",
        "contentDelta": '[{"insert":"已有正文\\n"}]',
        "plainText": "已有正文",
        "isPinned": False,
        "sortOrder": 0,
        "createdAt": "2026-01-01T00:00:00.000000Z",
        "updatedAt": "2026-01-02T00:00:00.000000Z",
        "deletedAt": None,
        "revision": 1,
        "syncStatus": "local",
        "deviceId": "existing-device",
    }


def make_pluto(path: Path, group_name="现有分组") -> None:
    data = {
        "groups": [base_group(group_name)],
        "notes": [base_note()],
        "attachments": [],
        "settings": {"defaultTargetGroupId": GROUP_ID, "keep": "unchanged"},
    }
    manifest = {
        "formatName": "pluto-notes-backup",
        "backupFormatVersion": 1,
        "appVersion": "0.6.3",
        "createdAt": "2026-09-28T03:12:51.018395Z",
        "devicePlatform": "flutter",
        "dataVersion": 3,
        "noteCount": 1,
        "groupCount": 1,
        "attachmentCount": 0,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr("data.json", json.dumps(data, ensure_ascii=False))


def make_wps(
    path: Path,
    *,
    group="现有分组",
    wps_id="wps-001",
    title="导入便签",
    body="普通正文",
    image=False,
    include_image=True,
) -> None:
    root = "wps-export"
    relative = f"{group}/{title}.md"
    front = {
        "wps_id": wps_id,
        "group": group,
        "created_at": "2026-09-28T10:00:00+08:00",
        "updated_at": "2026-09-28T11:00:00+08:00",
        "pinned": True,
    }
    markdown_body = f"# {title}\n\n{body}\n"
    if image:
        markdown_body += "\n![图片](images/pic.png)\n"
    markdown = "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False) + "---\n\n" + markdown_body
    note_meta = {
        "wps_id": wps_id,
        "title": title,
        "group": group,
        "path": relative,
        "created_at": front["created_at"],
        "updated_at": front["updated_at"],
        "pinned": True,
        "image_count": 1 if image else 0,
        "unsupported": [],
    }
    manifest = {
        "format_version": 1,
        "source": "https://note.wps.cn/",
        "generated_at": "2026-09-28T12:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "counts": {"found": 1, "exported": 1, "failed": 0, "images": 1 if image else 0},
        "notes": [note_meta],
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{root}/manifest.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr(f"{root}/export-report.md", "# report\n\n- failed: 0\n")
        archive.writestr(f"{root}/{relative}", markdown)
        if image and include_image:
            archive.writestr(f"{root}/{group}/images/pic.png", TINY_PNG)


class ImportWpsOnceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pluto = self.root / "pluto.zip"
        self.wps = self.root / "wps.zip"
        make_pluto(self.pluto)

    def tearDown(self):
        self.temp.cleanup()

    def test_stable_uuids(self):
        self.assertEqual(stable_note_uuid("abc"), stable_note_uuid("abc"))
        self.assertEqual(stable_group_uuid("学习"), stable_group_uuid("学习"))
        self.assertEqual(uuid.UUID(stable_note_uuid("abc")).version, 5)
        self.assertEqual(stable_note_uuid("abc"), str(uuid.uuid5(NOTE_NAMESPACE, "abc")))
        self.assertEqual(stable_group_uuid("学习"), str(uuid.uuid5(GROUP_NAMESPACE, "学习")))

    def test_time_conversion_to_utc(self):
        self.assertEqual(
            utc_iso("2026-09-28T10:30:00+08:00"),
            "2026-09-28T02:30:00.000000Z",
        )

    def test_markdown_to_quill_delta(self):
        markdown = "## 标题\n\n**粗体** *斜体* <u>下划线</u> ~~删除~~\n\n1. 有序\n2. 列表\n\n- 无序\n"
        ops, plain = markdown_to_quill(markdown)
        attributes = [op.get("attributes", {}) for op in ops]
        self.assertIn({"header": 2}, attributes)
        self.assertTrue(any(a.get("bold") for a in attributes))
        self.assertTrue(any(a.get("italic") for a in attributes))
        self.assertTrue(any(a.get("underline") for a in attributes))
        self.assertTrue(any(a.get("strike") for a in attributes))
        self.assertTrue(any(a.get("list") == "ordered" for a in attributes))
        self.assertTrue(any(a.get("list") == "bullet" for a in attributes))
        self.assertIn("有序", plain)

    def test_checklist_delta(self):
        ops, _ = markdown_to_quill("- [x] 完成\n- [ ] 未完成\n")
        lists = [op.get("attributes", {}).get("list") for op in ops]
        self.assertIn("checked", lists)
        self.assertIn("unchecked", lists)
        self.assertFalse(any("[x]" in op.get("insert", "") for op in ops if isinstance(op.get("insert"), str)))

    def test_group_mapping_reuses_name_and_creates_real_ungrouped(self):
        make_wps(self.wps, group="现有分组")
        result = merge_backups(self.wps, self.pluto, {"现有分组": "现有分组"})
        self.assertEqual(result.report.reused_groups, 1)
        self.assertEqual(result.report.new_groups, 0)
        imported = next(n for n in result.data["notes"] if n["id"] == stable_note_uuid("wps-001"))
        self.assertEqual(imported["groupId"], GROUP_ID)

        second = self.root / "ungrouped.zip"
        make_wps(second, group="未分组", wps_id="wps-002")
        result = merge_backups(second, self.pluto, {"未分组": "未分组"})
        group = next(g for g in result.data["groups"] if g["name"] == "未分组")
        imported = next(n for n in result.data["notes"] if n["id"] == stable_note_uuid("wps-002"))
        self.assertEqual(imported["groupId"], group["id"])

    def test_group_mapping_can_rename_and_unmapped_groups_are_skipped(self):
        make_wps(self.wps, group="现有分组")
        renamed = merge_backups(self.wps, self.pluto, {"现有分组": "迁移后的分组"})
        target = next(g for g in renamed.data["groups"] if g["name"] == "迁移后的分组")
        imported = next(n for n in renamed.data["notes"] if n["id"] == stable_note_uuid("wps-001"))
        self.assertEqual(imported["groupId"], target["id"])
        self.assertEqual(renamed.report.group_mappings[0]["pluto_group"], "迁移后的分组")

        excluded = merge_backups(self.wps, self.pluto, {})
        self.assertEqual(excluded.report.new_notes, 0)
        self.assertEqual(excluded.report.skipped_unmapped_notes, 1)
        self.assertEqual(excluded.report.unmapped_groups[0]["wps_group"], "现有分组")
        self.assertFalse(any(n["id"] == stable_note_uuid("wps-001") for n in excluded.data["notes"]))

    def test_parse_repeated_group_mappings(self):
        self.assertEqual(
            parse_group_mappings(["学习=知识库", "工作=项目"]),
            {"学习": "知识库", "工作": "项目"},
        )

    def test_image_attachment_and_embed(self):
        make_wps(self.wps, image=True)
        result = merge_backups(self.wps, self.pluto, {"现有分组": "现有分组"})
        self.assertEqual(result.report.attachments, 1)
        attachment = result.data["attachments"][0]
        self.assertEqual(attachment["noteId"], stable_note_uuid("wps-001"))
        self.assertEqual(result.files[attachment["archivePath"]], TINY_PNG)
        note = next(n for n in result.data["notes"] if n["id"] == stable_note_uuid("wps-001"))
        ops = json.loads(note["contentDelta"])
        self.assertIn(
            {"insert": {"image": f"attachment://{attachment['id']}"}},
            ops,
        )

    def test_merge_preserves_existing_data_and_settings(self):
        make_wps(self.wps)
        _, original, _ = read_pluto_backup(self.pluto)
        result = merge_backups(self.wps, self.pluto, {"现有分组": "现有分组"})
        self.assertIn(original["groups"][0], result.data["groups"])
        self.assertIn(original["notes"][0], result.data["notes"])
        self.assertEqual(result.data["settings"], original["settings"])
        imported = next(n for n in result.data["notes"] if n["id"] == stable_note_uuid("wps-001"))
        self.assertEqual(imported["createdAt"], "2026-09-28T02:00:00.000000Z")
        self.assertEqual(imported["updatedAt"], "2026-09-28T03:00:00.000000Z")
        self.assertTrue(imported["isPinned"])

    def test_repeated_import_does_not_duplicate(self):
        make_wps(self.wps, image=True)
        first = merge_backups(self.wps, self.pluto, {"现有分组": "现有分组"})
        merged = self.root / "merged.zip"
        write_backup_atomic(first, merged)
        second = merge_backups(self.wps, merged, {"现有分组": "现有分组"})
        self.assertEqual(second.report.new_notes, 0)
        self.assertEqual(second.report.skipped_notes, 1)
        self.assertEqual(second.report.attachments, 0)
        self.assertEqual(len(second.data["notes"]), len(first.data["notes"]))

    def test_existing_stable_id_with_different_content_creates_conflict_copy(self):
        make_wps(self.wps)
        manifest, data, files = read_pluto_backup(self.pluto)
        colliding = dict(base_note())
        colliding["id"] = stable_note_uuid("wps-001")
        colliding["title"] = "本地已有不同内容"
        data["notes"].append(colliding)
        manifest["noteCount"] += 1
        collision_backup = self.root / "collision.zip"
        with zipfile.ZipFile(collision_backup, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            archive.writestr("data.json", json.dumps(data, ensure_ascii=False))
            for name, blob in files.items():
                archive.writestr(name, blob)
        result = merge_backups(self.wps, collision_backup, {"现有分组": "现有分组"})
        self.assertEqual(result.report.conflicts, 1)
        self.assertEqual(result.report.new_notes, 1)
        conflict = next(
            note for note in result.data["notes"]
            if note["title"].endswith("（WPS 导入冲突副本）")
        )
        self.assertNotEqual(conflict["id"], colliding["id"])
        self.assertIn(colliding, result.data["notes"])

    def test_invalid_image_reference_is_reported(self):
        make_wps(self.wps, image=True, include_image=False)
        result = merge_backups(self.wps, self.pluto, {"现有分组": "现有分组"})
        self.assertEqual(len(result.report.failures), 1)
        self.assertEqual(result.report.new_notes, 0)

    def test_corrupt_zip_is_rejected(self):
        broken = self.root / "broken.zip"
        broken.write_bytes(b"not a zip")
        make_wps(self.wps)
        with self.assertRaises(ArchiveValidationError):
            merge_backups(self.wps, broken, {"现有分组": "现有分组"})

    def test_non_pluto_backup_is_rejected(self):
        wrong = self.root / "wrong.zip"
        with zipfile.ZipFile(wrong, "w") as archive:
            archive.writestr("manifest.json", '{"formatName":"other","backupFormatVersion":1}')
            archive.writestr("data.json", '{"groups":[],"notes":[],"attachments":[],"settings":{}}')
        make_wps(self.wps)
        with self.assertRaises(ArchiveValidationError):
            merge_backups(self.wps, wrong, {"现有分组": "现有分组"})

    def test_output_passes_cloud_decode_equivalent_validation(self):
        make_wps(self.wps, image=True)
        result = merge_backups(self.wps, self.pluto, {"现有分组": "现有分组"})
        output = self.root / "output.zip"
        write_backup_atomic(result, output)
        manifest, data, files = read_pluto_backup(output)
        validate_pluto_data(manifest, data, files)
        with zipfile.ZipFile(output) as archive:
            self.assertIn("import-report.json", archive.namelist())
            self.assertIn("import-report.md", archive.namelist())

    def test_zip_slip_is_rejected(self):
        malicious = self.root / "malicious.zip"
        with zipfile.ZipFile(malicious, "w") as archive:
            archive.writestr("../manifest.json", "{}")
        with self.assertRaises(ArchiveValidationError):
            read_pluto_backup(malicious)


if __name__ == "__main__":
    unittest.main()
