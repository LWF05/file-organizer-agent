"""Tkinter desktop interface for the file-organizing agent."""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from agent_core import (
    DEFAULT_CATEGORIES, UNCERTAIN, FileInfo, PlanItem, discard_undo,
    execute_plan, local_plan, parse_categories, read_history, scan_folder,
    undo_last,
)
from llm import deepseek_plan


def pretty_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


class FileAgentApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("资料整理 Agent")
        self.geometry("1060x710")
        self.minsize(850, 570)
        self.configure(bg="#f5f7fa")

        self.folder: Path | None = None
        self.files: list[FileInfo] = []
        self.plan: list[PlanItem] = []
        self.categories = DEFAULT_CATEGORIES
        self.busy = False

        self.folder_var = tk.StringVar(value="尚未选择文件夹")
        self.categories_var = tk.StringVar(value="，".join(DEFAULT_CATEGORIES))
        self.goal_var = tk.StringVar(value="根据文件名和类型整理；不确定的文件先保留")
        self.mode_var = tk.StringVar(value="本地规则")
        self.edit_var = tk.StringVar(value=UNCERTAIN)
        self.status_var = tk.StringVar(value="选择一个文件夹开始。只处理该文件夹顶层的普通文件。")

        self._configure_style()
        self._build_ui()

    def _configure_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f5f7fa")
        style.configure("TLabel", background="#f5f7fa", foreground="#253047", font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", background="#f5f7fa", foreground="#182437", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Sub.TLabel", background="#f5f7fa", foreground="#66758a", font=("Microsoft YaHei UI", 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 7))
        style.configure("Accent.TButton", background="#2763c5", foreground="white", font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#1c4fa3"), ("disabled", "#a9b8d0")])
        style.configure("TEntry", padding=5)
        style.configure("Treeview", font=("Microsoft YaHei UI", 9), rowheight=29, background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"), padding=7)

    def _build_ui(self):
        outer = ttk.Frame(self, padding=22)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="资料整理 Agent", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="扫描 → 生成分类方案 → 人工修改 → 确认移动 → 可撤销", style="Sub.TLabel").pack(anchor="w", pady=(2, 18))

        folder_row = ttk.Frame(outer)
        folder_row.pack(fill="x", pady=(0, 10))
        ttk.Label(folder_row, text="目标文件夹", width=12).pack(side="left")
        ttk.Entry(folder_row, textvariable=self.folder_var, state="readonly").pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.browse_button = ttk.Button(folder_row, text="选择文件夹", command=self.choose_folder)
        self.browse_button.pack(side="left", padx=(0, 6))
        self.scan_button = ttk.Button(folder_row, text="重新扫描", command=self.scan)
        self.scan_button.pack(side="left")

        goal_row = ttk.Frame(outer)
        goal_row.pack(fill="x", pady=(0, 10))
        ttk.Label(goal_row, text="整理目标", width=12).pack(side="left")
        ttk.Entry(goal_row, textvariable=self.goal_var).pack(side="left", fill="x", expand=True)

        options_row = ttk.Frame(outer)
        options_row.pack(fill="x", pady=(0, 15))
        ttk.Label(options_row, text="允许的分类", width=12).pack(side="left")
        ttk.Entry(options_row, textvariable=self.categories_var).pack(side="left", fill="x", expand=True, padx=(0, 15))
        ttk.Label(options_row, text="规划方式").pack(side="left", padx=(0, 7))
        ttk.Combobox(options_row, textvariable=self.mode_var,
                     values=("本地规则", "DeepSeek API"), state="readonly", width=15).pack(side="left")

        action_row = ttk.Frame(outer)
        action_row.pack(fill="x", pady=(0, 9))
        self.plan_button = ttk.Button(action_row, text="生成分类方案", style="Accent.TButton", command=self.generate_plan)
        self.plan_button.pack(side="left")
        ttk.Label(action_row, text="模型只读取文件名、扩展名与大小；不会读取文件内容。", style="Sub.TLabel").pack(side="left", padx=14)

        tree_frame = ttk.Frame(outer)
        tree_frame.pack(fill="both", expand=True)
        columns = ("name", "size", "category", "reason")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        for key, title, width in (
            ("name", "文件名", 260), ("size", "大小", 95),
            ("category", "目标分类", 125), ("reason", "分类依据 / 状态", 430),
        ):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=70, anchor="w", stretch=(key in ("name", "reason")))
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        edit_row = ttk.Frame(outer)
        edit_row.pack(fill="x", pady=(12, 8))
        ttk.Label(edit_row, text="修改选中文件的分类").pack(side="left", padx=(0, 10))
        self.edit_combo = ttk.Combobox(edit_row, textvariable=self.edit_var, state="readonly", width=18)
        self.edit_combo.pack(side="left", padx=(0, 8))
        self.edit_button = ttk.Button(edit_row, text="应用修改", command=self.edit_selected)
        self.edit_button.pack(side="left")
        ttk.Label(edit_row, text="分类不确定时可保留为“待确认”。", style="Sub.TLabel").pack(side="left", padx=15)

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(6, 0))
        self.execute_button = ttk.Button(bottom, text="确认并移动文件", style="Accent.TButton", command=self.execute)
        self.execute_button.pack(side="left")
        self.undo_button = ttk.Button(bottom, text="撤销上次移动", command=self.undo)
        self.undo_button.pack(side="left", padx=8)
        self.discard_button = ttk.Button(bottom, text="放弃上次撤销记录", command=self.discard_history)
        self.discard_button.pack(side="left")

        ttk.Label(outer, textvariable=self.status_var, style="Sub.TLabel").pack(anchor="w", pady=(13, 0))

    def _set_busy(self, busy: bool):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (self.browse_button, self.scan_button, self.plan_button,
                       self.execute_button, self.undo_button, self.edit_button, self.discard_button):
            button.configure(state=state)

    def choose_folder(self):
        chosen = filedialog.askdirectory(title="选择要整理的文件夹")
        if not chosen:
            return
        self.folder = Path(chosen).resolve()
        self.folder_var.set(str(self.folder))
        self.scan()

    def scan(self):
        if self.folder is None:
            messagebox.showinfo("选择文件夹", "请先选择要整理的文件夹。")
            return
        try:
            self.files = scan_folder(self.folder)
        except (OSError, ValueError) as exc:
            messagebox.showerror("扫描失败", str(exc))
            return
        self.plan = []
        self._refresh_table()
        try:
            history = read_history(self.folder)
            undo_text = "；上次移动仍可撤销" if history and history.get("pending_undo") else ""
        except (OSError, ValueError) as exc:
            undo_text = f"；撤销记录不可用：{exc}"
        self.status_var.set(f"扫描到 {len(self.files)} 个顶层文件{undo_text}。子文件夹和符号链接已跳过。")

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        planned = {item.name: item for item in self.plan}
        for file in self.files:
            plan = planned.get(file.name)
            self.tree.insert("", "end", iid=file.name,
                             values=(file.name, pretty_size(file.size),
                                     plan.category if plan else "未规划",
                                     plan.reason if plan else ""))

    def generate_plan(self):
        if self.folder is None:
            messagebox.showinfo("选择文件夹", "请先选择要整理的文件夹。")
            return
        try:
            self.categories = parse_categories(self.categories_var.get())
            self.files = scan_folder(self.folder)
            if not self.files:
                raise ValueError("这个文件夹的顶层没有可整理的文件")
            if len(self.files) > 100:
                raise ValueError("一次最多处理 100 个文件，请先拆分文件夹")
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法规划", str(exc))
            return
        self.edit_combo.configure(values=(*self.categories, UNCERTAIN))
        self.plan = []
        self._refresh_table()
        self.status_var.set("正在生成方案；此时不会移动文件。")
        if self.mode_var.get() == "本地规则":
            self._show_plan(local_plan(self.files, self.categories), "本地规则")
            return

        folder_snapshot = self.folder
        files_snapshot = list(self.files)
        categories_snapshot = self.categories
        goal_snapshot = self.goal_var.get().strip()
        self._set_busy(True)

        def worker():
            try:
                plan = deepseek_plan(files_snapshot, categories_snapshot, goal_snapshot)
                self.after(0, lambda: self._model_done(plan, folder_snapshot))
            except Exception as exc:
                self.after(0, lambda error=str(exc): self._model_failed(error))

        threading.Thread(target=worker, daemon=True).start()

    def _model_done(self, plan, folder_snapshot):
        self._set_busy(False)
        if self.folder != folder_snapshot:
            return
        self._show_plan(plan, "DeepSeek")

    def _model_failed(self, error):
        self._set_busy(False)
        messagebox.showerror("模型规划失败", error + "\n\n文件未移动。你可以改用“本地规则”。")
        self.status_var.set("模型规划失败；文件未移动。")

    def _show_plan(self, plan: list[PlanItem], source: str):
        self.plan = plan
        self._refresh_table()
        uncertain = sum(item.category == UNCERTAIN for item in plan)
        self.status_var.set(f"{source}已生成 {len(plan)} 条建议，其中 {uncertain} 条待确认。请检查并修改后再执行。")

    def _on_select(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        selected = selection[0]
        item = next((row for row in self.plan if row.name == selected), None)
        self.edit_var.set(item.category if item else UNCERTAIN)

    def edit_selected(self):
        selection = self.tree.selection()
        if not selection or not self.plan:
            messagebox.showinfo("修改分类", "请先生成方案，再选中一行。")
            return
        name, category = selection[0], self.edit_var.get()
        if category not in (*self.categories, UNCERTAIN):
            messagebox.showerror("无效分类", "请从允许的分类中选择。")
            return
        self.plan = [PlanItem(row.name, category, "人工指定") if row.name == name else row
                     for row in self.plan]
        self._refresh_table()
        self.tree.selection_set(name)
        self.status_var.set(f"已将 {name} 的目标分类改为 {category}；尚未移动文件。")

    def execute(self):
        if self.folder is None or not self.plan:
            messagebox.showinfo("生成方案", "请先扫描并生成分类方案。")
            return
        moving = [row for row in self.plan if row.category != UNCERTAIN]
        if not moving:
            messagebox.showinfo("没有待移动文件", "所有文件都标为“待确认”，请先人工修改分类。")
            return
        try:
            history = read_history(self.folder)
            if history and history.get("pending_undo"):
                if not messagebox.askyesno("上次操作仍可撤销", "执行新方案将放弃上次操作的撤销机会。继续吗？"):
                    return
                replace_history = True
            else:
                replace_history = False
        except (OSError, ValueError) as exc:
            messagebox.showerror("撤销记录异常", str(exc))
            return
        if not messagebox.askyesno(
            "确认移动", f"将尝试移动 {len(moving)} 个文件到当前文件夹下的分类子文件夹。\n"
            "原文件不会删除；遇到同名文件会跳过。\n\n确认执行吗？",
        ):
            return
        try:
            if replace_history:
                discard_undo(self.folder)
            result = execute_plan(self.folder, self.plan, self.categories)
        except (OSError, ValueError) as exc:
            messagebox.showerror("执行停止", str(exc))
            self.scan()
            return
        self.scan()
        details = "\n".join(f"{name}：{reason}" for name, reason in result["skipped"][:8])
        messagebox.showinfo("执行结果", f"已移动 {len(result['moved'])} 个文件；跳过 {len(result['skipped'])} 个。"
                            + ("\n\n跳过原因：\n" + details if details else ""))

    def undo(self):
        if self.folder is None:
            return
        if not messagebox.askyesno("撤销上次移动", "将上次移动的文件放回原位置。若原位置已有同名文件，会跳过。继续吗？"):
            return
        try:
            result = undo_last(self.folder)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法撤销", str(exc))
            return
        self.scan()
        messagebox.showinfo("撤销结果", f"已放回 {len(result['restored'])} 个文件；跳过 {len(result['skipped'])} 个。")

    def discard_history(self):
        if self.folder is None:
            return
        if not messagebox.askyesno("放弃撤销记录", "这会取消通过本程序撤销上次移动的机会；文件不会再移动。继续吗？"):
            return
        try:
            discard_undo(self.folder)
            self.scan()
        except (OSError, ValueError) as exc:
            messagebox.showerror("操作失败", str(exc))


if __name__ == "__main__":
    FileAgentApp().mainloop()
