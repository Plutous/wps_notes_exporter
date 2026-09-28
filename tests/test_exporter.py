import tempfile
import unittest
from pathlib import Path

from export_wps_notes import (
    build_markdown,
    epoch_to_iso,
    markdown_from_html,
    safe_name,
    unique_path,
)


class ExporterTests(unittest.TestCase):
    def test_safe_name_handles_windows_rules(self):
        self.assertEqual(safe_name('a<b>:c/'), 'a_b__c_')
        self.assertEqual(safe_name('CON'), '_CON')
        self.assertEqual(safe_name('...'), '未命名')

    def test_epoch_to_iso_uses_requested_timezone(self):
        self.assertEqual(epoch_to_iso(0), '')
        self.assertEqual(epoch_to_iso(1_725_000_000_000), '2024-08-30T14:40:00+08:00')

    def test_html_to_markdown_preserves_checklist_and_reports_style(self):
        markdown, unsupported = markdown_from_html(
            '<p style="color:red;text-align:center"><input type="checkbox" checked> 完成</p>'
        )
        self.assertIn('- [x] 完成', markdown)
        self.assertIn('文字颜色', unsupported)
        self.assertIn('对齐方式', unsupported)

    def test_build_markdown_has_yaml_and_heading(self):
        text, metadata = build_markdown(
            {
                'noteId': 'abc',
                'title': '标题',
                'createTime': 1_725_000_000_000,
                'contentUpdateTime': 1_725_000_001_000,
                'star': 1,
            },
            '学习',
            '正文',
            'Asia/Shanghai',
        )
        self.assertIn('wps_id: abc', text)
        self.assertIn('# 标题', text)
        self.assertTrue(metadata['pinned'])

    def test_unique_path_adds_id_on_collision(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / '标题.md').write_text('one', encoding='utf-8')
            self.assertEqual(unique_path(directory, '标题', '.md', 'abcdef').name, '标题-abcdef.md')


if __name__ == '__main__':
    unittest.main()
