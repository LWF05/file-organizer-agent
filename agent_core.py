"""Planning, validation, execution and undo for a local file-organizing agent.

Only top-level regular files in a user-selected folder are considered. The model
never receives file contents and never performs file operations directly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


HISTORY_FILE = ".file_agent_history.json"
UNCERTAIN = "待确认"
DEFAULT_CATEGORIES = ("文档", "图片", "代码", "压缩包", "其他")
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


@dataclass(frozen=True)
class FileInfo:
    name: str
    suffix: str
    size: int


@dataclass(frozen=True)
class PlanItem:
    name: str
    category: str
    reason: str


def valid_category(name: str) -> bool:
    name = name.strip()
    if not name or name in {".", ".."} or name.endswith((".", " ")):
        return False
    if any(char in name for char in '<>:"/\\|?*') or any(ord(char) < 32 for char in name):
        return False
    if name.split(".", 1)[0].upper() in WINDOWS_RESERVED:
        return False
    return True


def parse_categories(text: str) -> tuple[str, ...]:
    parts = [part.strip() for part in re.split(r"[,，;；\n]+", text) if part.strip()]
    if not 2 <= len(parts) <= 12 or len(set(parts)) != len(parts):
        raise ValueError("请输入 2 到 12 个不重复的分类，用逗号隔开")
    if UNCERTAIN in parts:
        raise ValueError("“待确认”由程序自动保留，无需写入分类")
    if not all(valid_category(part) for part in parts):
        raise ValueError("分类名不能包含路径符号或 Windows 保留名称")
    return tuple(parts)


def scan_folder(folder: Path) -> list[FileInfo]:
    folder = folder.resolve(strict=True)
    if not folder.is_dir():
        raise ValueError("请选择文件夹")
    items = []
    for path in folder.iterdir():
        if path.name in {HISTORY_FILE, HISTORY_FILE + ".tmp"} or path.is_symlink() or not path.is_file():
            continue
        items.append(FileInfo(path.name, path.suffix.lower(), path.stat().st_size))
    return sorted(items, key=lambda item: item.name.casefold())


def local_plan(files: Iterable[FileInfo], categories: tuple[str, ...]) -> list[PlanItem]:
    """A transparent offline baseline; uncertain names are left untouched."""
    suffix_groups = {
        "图片": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"},
        "文档": {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".txt", ".md"},
        "代码": {".py", ".cpp", ".c", ".h", ".hpp", ".cs", ".js", ".ts", ".java"},
        "压缩包": {".zip", ".rar", ".7z", ".tar", ".gz"},
    }
    result = []
    for file in files:
        match = next((name for name, endings in suffix_groups.items()
                      if file.suffix in endings and name in categories), None)
        if match:
            result.append(PlanItem(file.name, match, f"根据扩展名 {file.suffix} 分类"))
        elif "其他" in categories:
            result.append(PlanItem(file.name, "其他", "扩展名未匹配已知类型"))
        else:
            result.append(PlanItem(file.name, UNCERTAIN, "分类信息不足，等待人工确认"))
    return result


def validate_plan(raw: object, files: list[FileInfo], categories: tuple[str, ...]) -> list[PlanItem]:
    if not isinstance(raw, dict) or not isinstance(raw.get("plans"), list):
        raise ValueError("模型未返回 plans 列表")
    rows = raw["plans"]
    names = [file.name for file in files]
    if len(rows) != len(names):
        raise ValueError("模型返回的文件数量不正确")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("模型方案包含无效项目")
        name, category, reason = row.get("name"), row.get("category"), row.get("reason")
        if not isinstance(name, str) or not isinstance(category, str) or not isinstance(reason, str):
            raise ValueError("模型方案字段格式错误")
        if category not in (*categories, UNCERTAIN):
            raise ValueError(f"模型提出了未允许的分类：{category}")
        result.append(PlanItem(name, category, reason.strip()[:180] or "模型未提供理由"))
    if sorted(row.name for row in result) != sorted(names):
        raise ValueError("模型遗漏、重复或编造了文件名")
    return result


def execute_plan(folder: Path, plan: list[PlanItem], categories: tuple[str, ...]) -> dict:
    """Move only verified top-level files, never overwrite, and save an undo log."""
    folder = folder.resolve(strict=True)
    current = {file.name for file in scan_folder(folder)}
    if sorted(item.name for item in plan) != sorted(current):
        raise ValueError("文件夹内容已变化，请重新扫描并规划")
    if (folder / HISTORY_FILE).exists():
        history = read_history(folder)
        if history and history.get("pending_undo"):
            raise ValueError("上次操作尚可撤销；请先撤销或明确放弃该记录")

    for item in plan:
        if item.category != UNCERTAIN and (item.category not in categories or not valid_category(item.category)):
            raise ValueError(f"不允许的分类：{item.category}")

    moved, skipped = [], []
    for item in plan:
        if item.category == UNCERTAIN:
            skipped.append((item.name, "等待人工确认"))
            continue
        source = folder / item.name
        target_dir = folder / item.category
        target = target_dir / item.name
        if not source.is_file() or source.is_symlink():
            skipped.append((item.name, "源文件已变化"))
            continue
        if target_dir.exists() and (target_dir.is_symlink() or not target_dir.is_dir()):
            skipped.append((item.name, "目标文件夹不安全"))
            continue
        if target.exists() or target.is_symlink():
            skipped.append((item.name, "目标已有同名文件"))
            continue
        target_dir.mkdir(exist_ok=True)
        shutil.move(str(source), str(target))
        moved.append({"name": item.name, "category": item.category})
        _write_history(folder, moved, pending_undo=True)
    return {"moved": moved, "skipped": skipped}


def _write_history(folder: Path, moved: list[dict], pending_undo: bool) -> None:
    payload = {
        "folder": str(folder),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pending_undo": pending_undo,
        "moved": moved,
    }
    tmp = folder / (HISTORY_FILE + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, folder / HISTORY_FILE)


def read_history(folder: Path) -> dict | None:
    path = folder / HISTORY_FILE
    if not path.is_file() or path.is_symlink():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("folder") != str(folder.resolve()):
        raise ValueError("撤销记录不属于当前文件夹")
    return data


def undo_last(folder: Path) -> dict:
    folder = folder.resolve(strict=True)
    history = read_history(folder)
    if not history or not history.get("pending_undo"):
        raise ValueError("没有可撤销的操作")
    restored, skipped = [], []
    remaining = list(history.get("moved", []))
    for item in reversed(remaining[:]):
        name, category = item.get("name"), item.get("category")
        if (not isinstance(name, str) or not name or name in {".", ".."}
                or "/" in name or "\\" in name or Path(name).name != name
                or not isinstance(category, str) or not valid_category(category)):
            skipped.append((str(name), "撤销记录无效"))
            continue
        target = folder / category / name
        original = folder / name
        if (target.parent.is_symlink() or not target.is_file() or target.is_symlink()
                or original.exists() or original.is_symlink()):
            skipped.append((name, "文件已变化或原位置已有同名文件"))
            continue
        shutil.move(str(target), str(original))
        remaining.remove(item)
        restored.append(name)
        _write_history(folder, remaining, pending_undo=bool(remaining))
    if not remaining:
        _write_history(folder, [], pending_undo=False)
    return {"restored": restored, "skipped": skipped}


def discard_undo(folder: Path) -> None:
    folder = folder.resolve(strict=True)
    history = read_history(folder)
    if history and history.get("pending_undo"):
        _write_history(folder, history.get("moved", []), pending_undo=False)
