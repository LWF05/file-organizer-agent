import json
import tempfile
import unittest
from pathlib import Path

from agent_core import (
    HISTORY_FILE, PlanItem, UNCERTAIN, discard_undo, execute_plan,
    local_plan, parse_categories, read_history, scan_folder, undo_last,
    validate_plan,
)


class FileAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.folder = Path(self.temp.name)
        (self.folder / "report.pdf").write_bytes(b"pdf")
        (self.folder / "photo.jpg").write_bytes(b"jpg")
        (self.folder / "unknown.bin").write_bytes(b"bin")
        self.categories = ("文档", "图片")

    def tearDown(self):
        self.temp.cleanup()

    def test_categories_reject_path_and_duplicates(self):
        self.assertEqual(parse_categories("文档，图片"), ("文档", "图片"))
        for text in ("文档，文档", "../other，图片", "CON，图片"):
            with self.assertRaises(ValueError):
                parse_categories(text)

    def test_scan_only_top_level_files(self):
        (self.folder / "folder").mkdir()
        (self.folder / "folder" / "nested.pdf").write_bytes(b"nested")
        (self.folder / HISTORY_FILE).write_text("{}", encoding="utf-8")
        names = [item.name for item in scan_folder(self.folder)]
        self.assertEqual(names, ["photo.jpg", "report.pdf", "unknown.bin"])

    def test_local_plan_and_safe_execute_undo(self):
        files = scan_folder(self.folder)
        plan = local_plan(files, self.categories)
        targets = {item.name: item.category for item in plan}
        self.assertEqual(targets["report.pdf"], "文档")
        self.assertEqual(targets["photo.jpg"], "图片")
        self.assertEqual(targets["unknown.bin"], UNCERTAIN)
        result = execute_plan(self.folder, plan, self.categories)
        self.assertEqual(len(result["moved"]), 2)
        self.assertTrue((self.folder / "文档" / "report.pdf").exists())
        self.assertTrue((self.folder / "unknown.bin").exists())
        self.assertTrue(read_history(self.folder)["pending_undo"])
        undone = undo_last(self.folder)
        self.assertEqual(len(undone["restored"]), 2)
        self.assertTrue((self.folder / "report.pdf").exists())
        self.assertFalse(read_history(self.folder)["pending_undo"])

    def test_model_plan_rejects_hallucinated_file(self):
        files = scan_folder(self.folder)
        raw = {"plans": [
            {"name": "report.pdf", "category": "文档", "reason": "pdf"},
            {"name": "photo.jpg", "category": "图片", "reason": "jpg"},
            {"name": "invented.bin", "category": UNCERTAIN, "reason": "unknown"},
        ]}
        with self.assertRaises(ValueError):
            validate_plan(raw, files, self.categories)

    def test_conflicting_target_is_not_overwritten(self):
        (self.folder / "文档").mkdir()
        (self.folder / "文档" / "report.pdf").write_bytes(b"existing")
        plan = local_plan(scan_folder(self.folder), self.categories)
        result = execute_plan(self.folder, plan, self.categories)
        self.assertEqual((self.folder / "文档" / "report.pdf").read_bytes(), b"existing")
        self.assertTrue((self.folder / "report.pdf").exists())
        self.assertTrue(any(name == "report.pdf" for name, _ in result["skipped"]))

    def test_new_run_requires_explicitly_discarding_pending_undo(self):
        plan = local_plan(scan_folder(self.folder), self.categories)
        execute_plan(self.folder, plan, self.categories)
        remaining = scan_folder(self.folder)
        with self.assertRaises(ValueError):
            execute_plan(self.folder, local_plan(remaining, self.categories), self.categories)
        discard_undo(self.folder)
        self.assertFalse(read_history(self.folder)["pending_undo"])


if __name__ == "__main__":
    unittest.main()
