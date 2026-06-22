"""Tkinter GUI for Duplicate Finder with improved UX and progress reporting."""
from __future__ import annotations

import threading
import queue
import os
import time
import logging
import traceback
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox
from . import scanner
from . import thumbnail

# permanent delete behavior is preferred for this app


def _get_debug_logger() -> logging.Logger:
    logger = logging.getLogger("duplicate_finder.debug")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    log_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "duplicate_finder_debug.log"))
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    logger.debug("debug logger initialized")
    return logger


class DuplicateFinderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self._debug_logger = _get_debug_logger()
        self.title("Duplicate Finder")
        self.geometry("1200x700")
        self.minsize(980, 640)

        self.max_paths = 6
        self.initial_visible = 1
        self.path_vars = [tk.StringVar() for _ in range(self.max_paths)]
        self.path_entries: list[tk.Entry | None] = [None] * self.max_paths
        self.path_frames: list[tk.Frame | None] = [None] * self.max_paths
        self.path_remove_buttons: list[ttk.Button | None] = [None] * self.max_paths
        self.path_trace_registered = [False] * self.max_paths
        self.visible_count = 0

        # nicer label font (falls back if unavailable)
        try:
            self.label_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        except Exception:
            self.label_font = None

        self._scan_thread = None
        self._queue = None
        self._scan_generation = 0
        self._current_preview_entries: tuple[FileEntry | None, FileEntry | None] = (None, None)
        self.report_callback_exception = self._report_tk_callback_exception

        self._build_ui()

        self.result_groups = []
        self.conflict_rows = []
        self.thumb_cache = {}
        self.conflict_thumb_cache = {}
        self.active_scan_paths: list[str] = []

        # progress counting for determinate bar
        self._progress_count = 0

    def _report_tk_callback_exception(self, exc, val, tb):
        self._debug_logger.error("tk callback exception: %s", val)
        self._debug_logger.error("%s", "".join(traceback.format_exception(exc, val, tb)))
        tk.Tk.report_callback_exception(self, exc, val, tb)

    def _build_ui(self):
        top = ttk.LabelFrame(self, text=f"Folders to scan (max {self.max_paths})")
        top.pack(fill="x", padx=8, pady=6)

        self.paths_container = ttk.Frame(top)
        self.paths_container.pack(fill="x", padx=2, pady=2)

        # create initial visible rows
        for i in range(self.initial_visible):
            self._create_path_row(i)

        # Add button (eye-catching)
        add_btn = tk.Button(top, text="Add folder", bg="#28a745", fg="white", activebackground="#218838", width=12, command=self._add_path)
        add_btn.pack(anchor="e", padx=6, pady=(2, 6))

        # control buttons
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=8)
        self.scan_btn = tk.Button(ctrl, text="Scan", bg="#007bff", fg="white", width=8, command=self.start_scan)
        self.scan_btn.pack(side="left")
        self.clear_btn = tk.Button(ctrl, text="Clear", width=8, command=self._clear_paths)
        self.clear_btn.pack(side="left", padx=6)
        ttk.Button(ctrl, text="Quit", command=self.destroy).pack(side="right")

        # status + progress
        self.status = tk.StringVar(value="Ready")
        self.pbar = ttk.Progressbar(self, mode="indeterminate")
        self.pbar.pack(fill="x", padx=8, pady=4)
        ttk.Label(self, textvariable=self.status).pack(fill="x", padx=8)

        main = ttk.PanedWindow(self, orient="horizontal")
        self.main_pane = main
        main.pack(fill="both", expand=True, padx=8, pady=6)

        # Left: groups
        left = ttk.Frame(main)
        self.left_panel = left
        main.add(left, weight=1)
        ttk.Label(left, text="Duplicate Groups").pack(anchor="w")
        grp_wrap = ttk.Frame(left)
        grp_wrap.pack(fill="both", expand=True)
        grp_wrap.grid_rowconfigure(0, weight=1)
        grp_wrap.grid_columnconfigure(0, weight=1)
        self.grp_list = tk.Listbox(grp_wrap, height=20)
        self.grp_list_scrollbar = ttk.Scrollbar(grp_wrap, orient="vertical", command=self.grp_list.yview)
        self.grp_list.configure(yscrollcommand=self.grp_list_scrollbar.set)
        self.grp_list.grid(row=0, column=0, sticky="nsew")
        self.grp_list_scrollbar.grid(row=0, column=1, sticky="ns")
        self.grp_list.bind("<<ListboxSelect>>", self._on_group_select)

        # Middle: files in selected group
        mid = ttk.Frame(main)
        self.mid_panel = mid
        main.add(mid, weight=3)
        ttk.Label(mid, text="Files in group").pack(anchor="w")
        cols = ("selected", "name", "path", "size", "modified", "recommended")
        # first column is a checkbox indicator for bulk operations
        tree_wrap = ttk.Frame(mid)
        tree_wrap.pack(fill="both", expand=True)
        tree_wrap.grid_rowconfigure(0, weight=1)
        tree_wrap.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("selected", text="")
        self.tree.heading("name", text="Name")
        self.tree.heading("path", text="Path")
        self.tree.heading("size", text="Size")
        self.tree.heading("modified", text="Modified")
        self.tree.heading("recommended", text="Recommended")
        self.tree.column("selected", width=40, anchor="center")
        self.tree.column("name", width=260)
        self.tree.column("path", width=420)
        self.tree.column("size", width=80, anchor="e")
        self.tree.column("modified", width=140)
        self.tree.column("recommended", width=100, anchor="center")
        self.tree_vbar = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree_hbar = ttk.Scrollbar(tree_wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=self.tree_vbar.set, xscrollcommand=self.tree_hbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree_vbar.grid(row=0, column=1, sticky="ns")
        self.tree_hbar.grid(row=1, column=0, sticky="ew")
        # clicking the first column toggles the checkbox; selection shows side-by-side preview
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_file_select)
        self.tree.bind("<Control-c>", self._on_tree_copy_paths)
        self.tree.bind("<Button-3>", self._show_tree_copy_menu)

        btns = ttk.Frame(mid)
        btns.pack(fill="x")
        ttk.Button(btns, text="Delete Checked", command=self._delete_selected).pack(side="left")
        ttk.Button(btns, text="Check All", command=self._check_all).pack(side="left", padx=6)
        ttk.Button(btns, text="Uncheck All", command=self._uncheck_all).pack(side="left")
        ttk.Button(btns, text="Keep Suggested / Delete Others", command=self._delete_others).pack(side="left", padx=6)

        # Right: thumbnail preview
        right = ttk.Frame(main)
        self.right_panel = right
        main.add(right, weight=1)
        ttk.Label(right, text="Preview (select up to 2 files to compare)").pack(anchor="w")
        preview_container = ttk.Frame(right)
        self.preview_container = preview_container
        preview_container.pack(fill="both", expand=True)
        left_preview = ttk.Frame(preview_container, borderwidth=1, relief="solid")
        right_preview = ttk.Frame(preview_container, borderwidth=1, relief="solid")
        self.left_preview_frame = left_preview
        self.right_preview_frame = right_preview
        left_preview.pack(side="top", fill="both", expand=True, padx=2, pady=2)
        right_preview.pack(side="top", fill="both", expand=True, padx=2, pady=2)
        self.preview_left_label = ttk.Label(left_preview)
        self.preview_left_label.pack(fill="both", expand=True)
        self.preview_left_text = ttk.Label(left_preview, wraplength=220, justify="center")
        self.preview_left_text.pack(fill="x", pady=4)
        self.preview_right_label = ttk.Label(right_preview)
        self.preview_right_label.pack(fill="both", expand=True)
        self.preview_right_text = ttk.Label(right_preview, wraplength=220, justify="center")
        self.preview_right_text.pack(fill="x", pady=4)

        # Flat Explorer-like conflict table for all duplicate pairs.
        conflicts = ttk.LabelFrame(self, text="All Duplicate Conflicts")
        conflicts.pack(fill="x", expand=False, padx=8, pady=(0, 6))
        conflict_style = ttk.Style(self)
        conflict_style.configure("Conflict.Treeview", rowheight=38)
        src_row = ttk.Frame(conflicts)
        src_row.pack(fill="x", pady=(0, 3))
        self.left_source_var = tk.StringVar(value="")
        self.right_source_var = tk.StringVar(value="")
        ttk.Label(src_row, textvariable=self.left_source_var).pack(side="left", padx=(2, 24))
        ttk.Label(src_row, textvariable=self.right_source_var).pack(side="left")
        ccols = (
            "keep_left",
            "left_name",
            "left_modified",
            "left_size",
            "keep_right",
            "right_name",
            "right_modified",
            "right_size",
            "group",
        )
        conflict_table_host = ttk.Frame(conflicts)
        conflict_table_host.pack(fill="both", expand=True)
        conflict_table_host.grid_rowconfigure(0, weight=1)
        conflict_table_host.grid_columnconfigure(0, weight=1)

        self.conflict_tree = ttk.Treeview(
            conflict_table_host,
            columns=ccols,
            show="headings",
            height=5,
            selectmode="browse",
            style="Conflict.Treeview",
        )
        self.conflict_tree.heading("keep_left", text="Keep")
        self.conflict_tree.heading("left_name", text="Folder A File")
        self.conflict_tree.heading("left_modified", text="Left Modified")
        self.conflict_tree.heading("left_size", text="Left Size")
        self.conflict_tree.heading("keep_right", text="Keep")
        self.conflict_tree.heading("right_name", text="Folder B File")
        self.conflict_tree.heading("right_modified", text="Right Modified")
        self.conflict_tree.heading("right_size", text="Right Size")
        self.conflict_tree.heading("group", text="Group")
        self.conflict_tree.column("keep_left", width=45, anchor="center")
        self.conflict_tree.column("left_name", width=260)
        self.conflict_tree.column("left_modified", width=130)
        self.conflict_tree.column("left_size", width=90, anchor="e")
        self.conflict_tree.column("keep_right", width=45, anchor="center")
        self.conflict_tree.column("right_name", width=260)
        self.conflict_tree.column("right_modified", width=130)
        self.conflict_tree.column("right_size", width=90, anchor="e")
        self.conflict_tree.column("group", width=70, anchor="center")
        self.conflict_tree.bind("<Button-1>", self._on_conflict_click)
        self.conflict_tree.bind("<<TreeviewSelect>>", self._on_conflict_select)
        self.conflict_tree.bind("<Control-c>", self._on_conflict_copy_paths)
        self.conflict_tree.bind("<Button-3>", self._show_conflict_copy_menu)
        conflict_btns = ttk.Frame(conflicts)
        self.conflict_btns = conflict_btns
        conflict_btns.pack(fill="x", pady=(0, 6), before=conflict_table_host)
        self.skip_identical_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            conflict_btns,
            text="Skip identical date+size",
            variable=self.skip_identical_var,
        ).pack(side="left", padx=(0, 10))
        ttk.Button(conflict_btns, text="Keep Left In All", command=lambda: self._set_conflict_keep_side("left")).pack(side="left")
        ttk.Button(conflict_btns, text="Keep Right In All", command=lambda: self._set_conflict_keep_side("right")).pack(side="left", padx=6)
        ttk.Button(conflict_btns, text="Keep Both In All", command=lambda: self._set_conflict_keep_side("both")).pack(side="left")
        ttk.Button(
            conflict_btns,
            text="Delete Unchecked In Conflicts",
            command=self._delete_unchecked_conflicts,
        ).pack(side="left", padx=(10, 0))

        self.conflict_tree_ybar = ttk.Scrollbar(conflict_table_host, orient="vertical", command=self.conflict_tree.yview)
        self.conflict_tree_xbar = ttk.Scrollbar(conflict_table_host, orient="horizontal", command=self.conflict_tree.xview)
        self.conflict_tree.configure(yscrollcommand=self.conflict_tree_ybar.set, xscrollcommand=self.conflict_tree_xbar.set)
        self.conflict_tree.grid(row=0, column=0, sticky="nsew")
        self.conflict_tree_ybar.grid(row=0, column=1, sticky="ns")
        self.conflict_tree_xbar.grid(row=1, column=0, sticky="ew")

        self._build_copy_menus()

    def _build_copy_menus(self):
        self.tree_copy_menu = tk.Menu(self, tearoff=0)
        self.tree_copy_menu.add_command(label="Copy file name(s)", command=lambda: self._copy_tree_selection("name"))
        self.tree_copy_menu.add_command(label="Copy path(s)", command=lambda: self._copy_tree_selection("path"))
        self.tree_copy_menu.add_command(label="Copy name + path", command=lambda: self._copy_tree_selection("both"))

        self.conflict_copy_menu = tk.Menu(self, tearoff=0)
        self.conflict_copy_menu.add_command(label="Copy left file name", command=lambda: self._copy_conflict_selection("left_name"))
        self.conflict_copy_menu.add_command(label="Copy left path", command=lambda: self._copy_conflict_selection("left_path"))
        self.conflict_copy_menu.add_command(label="Copy right file name", command=lambda: self._copy_conflict_selection("right_name"))
        self.conflict_copy_menu.add_command(label="Copy right path", command=lambda: self._copy_conflict_selection("right_path"))
        self.conflict_copy_menu.add_separator()
        self.conflict_copy_menu.add_command(label="Copy both paths", command=lambda: self._copy_conflict_selection("both_paths"))

    def _copy_text(self, text: str, status_msg: str) -> None:
        if not text:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
            self.status.set(status_msg)
        except Exception:
            pass

    def _copy_tree_selection(self, mode: str) -> None:
        group_idx = self.grp_list.curselection()
        if not group_idx:
            return
        g = self.result_groups[group_idx[0]]
        sel = self.tree.selection()
        if not sel:
            return

        lines = []
        for iid in sel:
            try:
                idx = int(iid)
            except Exception:
                continue
            if idx < 0 or idx >= len(g["files"]):
                continue
            path = g["files"][idx].path
            name = os.path.basename(path)
            if mode == "name":
                lines.append(name)
            elif mode == "path":
                lines.append(path)
            else:
                lines.append(f"{name} | {path}")

        if not lines:
            return
        label = "name(s)" if mode == "name" else "path(s)" if mode == "path" else "entry/entries"
        self._copy_text("\n".join(lines), f"Copied {len(lines)} {label}")

    def _show_tree_copy_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        if not self.tree.selection():
            return
        try:
            self.tree_copy_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.tree_copy_menu.grab_release()

    def _on_tree_copy_paths(self, _event):
        self._copy_tree_selection("path")
        return "break"

    def _copy_conflict_selection(self, mode: str) -> None:
        sel = self.conflict_tree.selection()
        if not sel:
            return
        left, right = self._conflict_entries_from_iid(sel[0])
        if not left or not right:
            return

        if mode == "left_name":
            text = os.path.basename(left.path)
            msg = "Copied left file name"
        elif mode == "left_path":
            text = left.path
            msg = "Copied left path"
        elif mode == "right_name":
            text = os.path.basename(right.path)
            msg = "Copied right file name"
        elif mode == "right_path":
            text = right.path
            msg = "Copied right path"
        else:
            text = f"{left.path}\n{right.path}"
            msg = "Copied both paths"
        self._copy_text(text, msg)

    def _show_conflict_copy_menu(self, event):
        row = self.conflict_tree.identify_row(event.y)
        if row and row not in self.conflict_tree.selection():
            self.conflict_tree.selection_set(row)
        if not self.conflict_tree.selection():
            return
        try:
            self.conflict_copy_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.conflict_copy_menu.grab_release()

    def _update_conflict_column_headings(self, left_path: Optional[str] = None, right_path: Optional[str] = None) -> None:
        if left_path:
            left_label = self._folder_label_for_path(left_path)
            self.conflict_tree.heading("left_name", text=f"{left_label} File")
        else:
            self.conflict_tree.heading("left_name", text="Folder A File")
        if right_path:
            right_label = self._folder_label_for_path(right_path)
            self.conflict_tree.heading("right_name", text=f"{right_label} File")
        else:
            self.conflict_tree.heading("right_name", text="Folder B File")

    def _on_conflict_copy_paths(self, _event):
        self._copy_conflict_selection("both_paths")
        return "break"

    def _on_global_mousewheel(self, event):
        return None

    def _is_conflict_widget(self, widget) -> bool:
        return False

    def _on_conflict_mousewheel(self, event):
        delta = 0
        if getattr(event, "delta", 0):
            delta = -int(event.delta / 120)
            if delta == 0:
                delta = -1 if event.delta > 0 else 1
        elif getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1

        if delta != 0:
            self.conflict_list_canvas.yview_scroll(delta, "units")
            return "break"
        return None

    def _create_path_row(self, idx: int):
        # create a single path row; can be called to add more rows dynamically
        if self.path_frames[idx] is not None:
            return
        frm = ttk.Frame(self.paths_container)
        frm.pack(fill="x", padx=2, pady=2)
        lbl_text = f"Folder {idx+1}"
        lbl = tk.Label(frm, text=lbl_text, font=self.label_font, fg="#222")
        lbl.pack(side="left", padx=(0, 6))
        e = tk.Entry(frm, textvariable=self.path_vars[idx])
        e.pack(side="left", fill="x", expand=True)
        ttk.Button(frm, text="Browse", command=lambda v=self.path_vars[idx]: self._browse(v)).pack(side="left", padx=4)
        remove_btn = ttk.Button(frm, text="Remove", command=lambda i=idx: self._remove_path(i))
        remove_btn.pack(side="left", padx=(0, 2))
        if idx == 0:
            remove_btn.config(state="disabled")

        self.path_frames[idx] = frm
        self.path_entries[idx] = e
        self.path_remove_buttons[idx] = remove_btn
        if getattr(self, "default_entry_bg", None) is None:
            try:
                self.default_entry_bg = e.cget("bg")
            except Exception:
                self.default_entry_bg = "white"

        if not self.path_trace_registered[idx]:
            try:
                self.path_vars[idx].trace_add("write", lambda *a, i=idx: self._on_path_change(i))
            except Exception:
                self.path_vars[idx].trace("w", lambda *a, i=idx: self._on_path_change(i))
            self.path_trace_registered[idx] = True

        self.visible_count += 1
        self._on_path_change(idx)

    def _add_path(self):
        if self.visible_count >= self.max_paths:
            messagebox.showinfo("Limit reached", f"Maximum of {self.max_paths} folders")
            return
        for idx, frame in enumerate(self.path_frames):
            if frame is None:
                self._create_path_row(idx)
                return

    def _remove_path(self, idx: int):
        if idx == 0:
            return
        frame = self.path_frames[idx]
        if frame is None:
            return
        self.path_vars[idx].set("")
        frame.destroy()
        self.path_frames[idx] = None
        self.path_entries[idx] = None
        self.path_remove_buttons[idx] = None
        self.visible_count = max(0, self.visible_count - 1)
        self._update_scan_button_state()

    def _browse(self, var: tk.StringVar):
        d = filedialog.askdirectory()
        if d:
            var.set(d)

    def _clear_paths(self):
        self._scan_generation += 1
        for idx, v in enumerate(self.path_vars):
            v.set("")
            if idx > 0 and self.path_frames[idx] is not None:
                self._remove_path(idx)
        self._reset_results()
        self._update_scan_button_state()

    def _reset_results(self):
        self.grp_list.delete(0, "end")
        self.tree.delete(*self.tree.get_children())
        self.conflict_tree.delete(*self.conflict_tree.get_children())
        self._clear_preview()
        self._set_conflict_heading_labels()
        self.result_groups = []
        self.conflict_rows = []
        self.thumb_cache.clear()
        self.conflict_thumb_cache.clear()
        self.active_scan_paths = []
        self._queue = None
        self._scan_thread = None
        try:
            self.pbar.stop()
        except Exception:
            pass
        try:
            self.pbar.config(mode="indeterminate", value=0)
        except Exception:
            pass
        self.status.set("Ready")

    def start_scan(self):
        # require at least one valid folder path
        cnt = sum(1 for v in self.path_vars if v.get().strip())
        if cnt < 1:
            if self.path_entries[0] is not None:
                try:
                    self.path_entries[0].config(bg="#fff0f0")
                except Exception:
                    pass
            messagebox.showwarning("Path required", "Please enter at least one folder to scan.")
            return

        paths = [v.get().strip() for v in self.path_vars if v.get().strip()]
        if not paths:
            messagebox.showwarning("No paths", "Please add at least one folder to scan")
            return

        # validate paths exist before starting scan
        bad_paths = [p for p in paths if not os.path.isdir(p)]
        if bad_paths:
            messagebox.showerror(
                "Invalid folder(s)",
                "The following paths do not exist or are not folders:\n\n" + "\n".join(bad_paths)
            )
            return

        # disable UI
        self._scan_generation += 1
        generation = self._scan_generation
        self.scan_btn.config(state="disabled")
        self.pbar.config(mode="indeterminate")
        self.pbar.start()
        self.status.set("Scanning...")
        self._reset_results()
        self.status.set("Scanning...")
        self.scan_btn.config(state="disabled")
        self.pbar.config(mode="indeterminate")
        self.pbar.start()
        self.active_scan_paths = list(paths)

        self._queue = queue.Queue()
        t = threading.Thread(target=self._scan_worker, args=(paths, self._queue, generation), daemon=True)
        self._scan_thread = t
        t.start()
        self.after(100, self._process_queue)

    def _on_path_change(self, idx: int):
        # called when a path variable changes; update visual validation and scan button state
        val = self.path_vars[idx].get().strip()
        e = self.path_entries[idx]
        if idx == 0 and e is not None:
            try:
                e.config(bg=self.default_entry_bg if val else "#fff0f0")
            except Exception:
                pass
        elif e is not None:
            try:
                e.config(bg=self.default_entry_bg)
            except Exception:
                pass
        self._update_scan_button_state()

    def _update_scan_button_state(self):
        cnt = sum(1 for v in self.path_vars if v.get().strip())
        try:
            if cnt >= 1:
                self.scan_btn.config(state="normal")
            else:
                self.scan_btn.config(state="disabled")
        except Exception:
            pass

    def _scan_worker(self, paths, q: queue.Queue, generation: int):
        def cb(msg):
            # structured messages from scanner are dicts with a 'type' key
            if isinstance(msg, dict) and "type" in msg:
                q.put((generation, msg["type"], msg))
            else:
                q.put((generation, "status", str(msg)))

        try:
            # report how many files were found before hashing starts
            file_list = scanner._collect_files(paths)
            q.put((generation, "status", {"type": "status", "text": f"Collected {len(file_list):,} files — analysing for duplicates..."}))
            res = scanner.find_duplicates(paths, workers=None, progress_callback=cb)
            q.put((generation, "done", res))
        except Exception as exc:
            q.put((generation, "error", {"text": str(exc)}))

    def _fmt_size(self, size: int) -> str:
        return f"{size:,}"

    def _fmt_mtime(self, mtime: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))

    def _set_conflict_keep_value(self, iid: str, side: str, keep: bool) -> None:
        vals = list(self.conflict_tree.item(iid, "values"))
        if not vals:
            return
        idx = 0 if side == "left" else 4
        vals[idx] = "☑" if keep else "☐"
        self.conflict_tree.item(iid, values=vals)
        self._render_conflict_rows()

    def _render_conflict_rows(self) -> None:
        return

    def _file_with_folder_subtitle(self, path: str) -> str:
        folder = self._folder_label_for_path(path)
        return f"{os.path.basename(path)}\n{folder}"

    def _set_conflict_heading_labels(self, left_path: Optional[str] = None, right_path: Optional[str] = None) -> None:
        if left_path:
            self.left_source_var.set(f"Folder {self._folder_label_for_path(left_path)}")
        else:
            self.left_source_var.set("")
        if right_path:
            self.right_source_var.set(f"Folder {self._folder_label_for_path(right_path)}")
        else:
            self.right_source_var.set("")
        self._update_conflict_column_headings(left_path, right_path)

    def _rebuild_conflict_list(self) -> None:
        self.conflict_rows = []
        self.conflict_tree.delete(*self.conflict_tree.get_children())

        for gi, group in enumerate(self.result_groups):
            files = group.get("files", [])
            if len(files) < 2:
                continue
            suggested = group.get("suggested", 0)
            if suggested < 0 or suggested >= len(files):
                suggested = 0

            for ri, _entry in enumerate(files):
                if ri == suggested:
                    continue
                left = files[suggested]
                right = files[ri]
                row = {"group": gi, "left": suggested, "right": ri}
                self.conflict_rows.append(row)
                iid = f"c{len(self.conflict_rows) - 1}"
                self.conflict_tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=(
                        "☑",
                        self._file_with_folder_subtitle(left.path),
                        self._fmt_mtime(left.mtime),
                        self._fmt_size(left.size),
                        "☐",
                        self._file_with_folder_subtitle(right.path),
                        self._fmt_mtime(right.mtime),
                        self._fmt_size(right.size),
                        f"G{gi + 1}",
                    ),
                )

        first = self.conflict_tree.get_children("")
        if first:
            self.conflict_tree.selection_set(first[0])
            left, right = self._conflict_entries_from_iid(first[0])
            if left and right:
                self._set_conflict_heading_labels(left.path, right.path)
        else:
            self._set_conflict_heading_labels()
        self._render_conflict_rows()

    def _conflict_entries_from_iid(self, iid: str):
        try:
            cidx = int(iid[1:])
            row = self.conflict_rows[cidx]
            gi = row["group"]
            li = row["left"]
            ri = row["right"]
            group = self.result_groups[gi]
            left = group["files"][li]
            right = group["files"][ri]
            return left, right
        except Exception:
            return None, None

    def _on_conflict_click(self, event):
        region = self.conflict_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.conflict_tree.identify_column(event.x)
        row = self.conflict_tree.identify_row(event.y)
        if not row:
            return
        self._update_conflict_thumbnail_cards(row)
        if col in ("#1", "#5"):
            vals = list(self.conflict_tree.item(row, "values"))
            if not vals:
                return "break"
            idx = 0 if col == "#1" else 4
            vals[idx] = "☑" if vals[idx] != "☑" else "☐"
            self.conflict_tree.item(row, values=vals)
            return "break"

    def _on_conflict_select(self, _evt):
        sel = self.conflict_tree.selection()
        if not sel:
            self._set_conflict_heading_labels()
            self._render_conflict_rows()
            return
        left, right = self._conflict_entries_from_iid(sel[0])
        if not left or not right:
            self._set_conflict_heading_labels()
            self._render_conflict_rows()
            return
        self._set_conflict_heading_labels(left.path, right.path)
        self._clear_preview()
        self._show_preview_entry(left, self.preview_left_label, self.preview_left_text)
        self._show_preview_entry(right, self.preview_right_label, self.preview_right_text)
        self._render_conflict_rows()

    def _folder_label_for_path(self, path: str) -> str:
        # Map a file path to the closest user-selected scan root for friendly labels.
        norm = os.path.normcase(os.path.abspath(path))
        best = ""
        for root in self.active_scan_paths:
            abs_root = os.path.normcase(os.path.abspath(root))
            if norm == abs_root or norm.startswith(abs_root + os.sep):
                if len(abs_root) > len(best):
                    best = abs_root
        if best:
            tail = os.path.basename(best.rstrip("\\/"))
            return tail if tail else best
        parent = os.path.dirname(path)
        tail = os.path.basename(parent.rstrip("\\/"))
        return tail if tail else parent

    def _set_conflict_keep_side(self, side: str) -> None:
        for iid in self.conflict_tree.get_children(""):
            vals = list(self.conflict_tree.item(iid, "values"))
            if not vals:
                continue
            if side == "left":
                vals[0], vals[4] = "☑", "☐"
            elif side == "right":
                vals[0], vals[4] = "☐", "☑"
            else:
                vals[0], vals[4] = "☑", "☑"
            self.conflict_tree.item(iid, values=vals)

    def _delete_unchecked_conflicts(self):
        rows = self.conflict_tree.get_children("")
        if not rows:
            messagebox.showinfo("No conflicts", "No duplicate conflicts available")
            return

        keep_paths = set()
        delete_candidates = set()
        skipped_rows = 0
        skipped_identical = 0

        for iid in rows:
            left, right = self._conflict_entries_from_iid(iid)
            if not left or not right:
                continue
            vals = self.conflict_tree.item(iid, "values")
            keep_left = bool(vals and vals[0] == "☑")
            keep_right = bool(vals and vals[4] == "☑")

            if self.skip_identical_var.get() and left.size == right.size and int(left.mtime) == int(right.mtime):
                keep_paths.add(left.path)
                keep_paths.add(right.path)
                skipped_identical += 1
                continue

            if keep_left:
                keep_paths.add(left.path)
            else:
                delete_candidates.add(left.path)

            if keep_right:
                keep_paths.add(right.path)
            else:
                delete_candidates.add(right.path)

            if not keep_left and not keep_right:
                skipped_rows += 1

        to_delete = sorted(delete_candidates - keep_paths)
        if not to_delete:
            messagebox.showinfo("Nothing to delete", "Every file is marked to keep")
            return

        warn = ""
        if skipped_rows:
            warn = f"\n\nNote: {skipped_rows} row(s) had both sides unchecked; files kept elsewhere were protected."
        if skipped_identical:
            warn += f"\n\nSkipped {skipped_identical} row(s) because date+size were identical."
        ok = messagebox.askyesno(
            "Confirm delete",
            f"Permanently delete {len(to_delete)} unchecked file(s)?{warn}",
        )
        if not ok:
            return

        deleted = 0
        for path in to_delete:
            try:
                os.remove(path)
                deleted += 1
            except FileNotFoundError:
                pass
            except OSError as exc:
                msg = str(exc).lower()
                if "cannot find the path" in msg or "no such file" in msg:
                    pass
                else:
                    messagebox.showerror("Delete failed", f"{path}: {exc}")

        if deleted:
            # Purge deleted files from all groups and remove groups with fewer than 2 files.
            kept_groups = []
            for g in self.result_groups:
                g["files"] = [e for e in g["files"] if e.path not in to_delete]
                if len(g["files"]) > 1:
                    suggested = g.get("suggested", 0)
                    if suggested >= len(g["files"]):
                        g["suggested"] = 0
                    kept_groups.append(g)
            self.result_groups = kept_groups

            self.grp_list.delete(0, "end")
            for i, g in enumerate(self.result_groups):
                self.grp_list.insert("end", f"Group {i+1}: {len(g['files'])} files ({g['type']})")
            self.tree.delete(*self.tree.get_children())
            self._clear_preview()
            self._rebuild_conflict_list()
            extra = f", skipped {skipped_identical} identical row(s)" if skipped_identical else ""
            self.status.set(f"Deleted {deleted} file(s){extra}")

    def _process_queue(self):
        if self._queue is None:
            return
        try:
            while not self._queue.empty():
                generation, typ, payload = self._queue.get_nowait()
                if generation != self._scan_generation:
                    continue
                if typ == "status":
                    # payload may be dict or string
                    if isinstance(payload, dict):
                        text = payload.get("text", "")
                        self.status.set(text)
                    else:
                        self.status.set(str(payload))
                elif typ == "total":
                    # set determinate progress bar for partial hashing stage
                    total = payload.get("total") if isinstance(payload, dict) else int(payload)
                    self.pbar.config(mode="determinate", maximum=max(1, total), value=0)
                    self._progress_count = 0
                elif typ == "file":
                    # payload expected: {'type':'file','path':..., 'stage':'partial'|'full'}
                    path = payload.get("path") if isinstance(payload, dict) else str(payload)
                    stage = payload.get("stage", "") if isinstance(payload, dict) else ""
                    name = os.path.basename(path)
                    self.status.set(f"{stage.title()} {name}")
                    if isinstance(payload, dict) and payload.get("stage") == "partial":
                        self._progress_count += 1
                        try:
                            self.pbar['value'] = self._progress_count
                        except Exception:
                            pass
                elif typ == "done":
                    self._on_scan_done(payload)
                elif typ == "error":
                    msg = payload.get("text") if isinstance(payload, dict) else str(payload)
                    messagebox.showerror("Scan error", msg)
                    self._finish_scan()
        except queue.Empty:
            pass

        # continue polling while the scan thread is running or there are queued messages
        if self._queue and (not self._queue.empty() or (self._scan_thread and self._scan_thread.is_alive())):
            self.after(200, self._process_queue)

    def _on_scan_done(self, groups):
        self.result_groups = groups
        self.grp_list.delete(0, "end")
        for i, g in enumerate(groups):
            txt = f"Group {i+1}: {len(g['files'])} files ({g['type']})"
            self.grp_list.insert("end", txt)
        self._rebuild_conflict_list()
        if groups:
            self.status.set(
                f"Found {len(groups)} duplicate group(s), {len(self.conflict_rows)} conflict row(s)"
            )
        else:
            self.status.set("No duplicates found in the scanned folder(s)")
        self._finish_scan()

    def _finish_scan(self):
        try:
            self.pbar.stop()
        except Exception:
            pass
        try:
            self.pbar.config(mode="indeterminate", value=0)
        except Exception:
            pass
        self.scan_btn.config(state="normal")

    def _on_group_select(self, evt):
        sel = self.grp_list.curselection()
        if not sel:
            return
        idx = sel[0]
        group = self.result_groups[idx]
        self._debug_logger.debug(
            "group select idx=%s type=%s file_count=%s suggested=%s",
            idx,
            group.get("type"),
            len(group.get("files", [])),
            group.get("suggested", 0),
        )
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(group["files"]):
            name = os.path.basename(e.path)
            path = e.path
            size = f"{e.size:,}"
            mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.mtime))
            recommended = "YES" if i == group.get("suggested", -1) else ""
            # initial checkbox state unchecked
            self.tree.insert("", "end", iid=str(i), values=("☐", name, path, size, mtime, recommended))

        self._clear_preview()
        self.tree.selection_set(str(group.get("suggested", 0)))
        self._update_preview(group, [str(group.get("suggested", 0))])

    def _on_file_select(self, evt):
        group_idx = self.grp_list.curselection()
        if not group_idx:
            return
        group = self.result_groups[group_idx[0]]
        sel = self.tree.selection()
        self._debug_logger.debug("file select group_idx=%s sel=%s", group_idx[0], list(sel))
        self._update_preview(group, sel)

    def _show_preview_entry(self, entry: FileEntry, image_label: ttk.Label, text_label: ttk.Label):
        size = self._preview_thumbnail_size(image_label)
        cache_key = (entry.path, size)
        self._debug_logger.debug("show preview path=%s size=%s cache_hit=%s", entry.path, size, cache_key in self.thumb_cache)
        img = self.thumb_cache.get(cache_key)
        if img is None:
            img = thumbnail.make_thumbnail(entry.path, size=size)
            self.thumb_cache[cache_key] = img
        if img is not None:
            image_label.config(image=img, text="", anchor="center")
            image_label.image = img
        else:
            image_label.config(image="", text=os.path.basename(entry.path), anchor="center")
            image_label.image = None
        text_label.config(text=os.path.basename(entry.path))

    def _preview_thumbnail_size(self, image_label: ttk.Label) -> tuple[int, int]:
        width = image_label.winfo_width()
        height = image_label.winfo_height()
        if width <= 1:
            width = 220
        if height <= 1:
            height = 180

        max_w = max(160, min(240, width - 16))
        max_h = max(120, min(220, height - 24))
        return max_w, max_h

    def _refresh_current_preview_images(self) -> None:
        left_entry, right_entry = self._current_preview_entries
        if left_entry is None and right_entry is None:
            return
        for lbl in (self.preview_left_label, self.preview_right_label):
            lbl.config(image="", text="")
            lbl.image = None
        self.preview_left_text.config(text="")
        self.preview_right_text.config(text="")
        if left_entry is not None:
            self._show_preview_entry(left_entry, self.preview_left_label, self.preview_left_text)
        if right_entry is not None:
            self._show_preview_entry(right_entry, self.preview_right_label, self.preview_right_text)

    def _clear_preview(self):
        for lbl in (self.preview_left_label, self.preview_right_label):
            lbl.config(image="", text="")
            lbl.image = None
        self.preview_left_text.config(text="")
        self.preview_right_text.config(text="")
        self._current_preview_entries = (None, None)

    def _update_preview(self, group: dict, sel_ids: list[str]) -> None:
        self._debug_logger.debug(
            "update preview type=%s total_files=%s sel_ids=%s suggested=%s",
            group.get("type"),
            len(group.get("files", [])),
            list(sel_ids),
            group.get("suggested", 0),
        )
        selected = []
        for iid in sel_ids:
            try:
                idx = int(iid)
                if 0 <= idx < len(group["files"]):
                    selected.append(idx)
            except Exception:
                continue
        if not selected:
            selected = [group.get("suggested", 0)]
        self._clear_preview()
        left_entry = None
        right_entry = None
        if selected:
            left_entry = group["files"][selected[0]]
            self._show_preview_entry(left_entry, self.preview_left_label, self.preview_left_text)
        if len(selected) > 1:
            right_entry = group["files"][selected[1]]
            self._show_preview_entry(right_entry, self.preview_right_label, self.preview_right_text)
        elif len(group["files"]) > 1:
            other_idx = 1 if selected[0] == 0 else 0
            right_entry = group["files"][other_idx]
            self._show_preview_entry(right_entry, self.preview_right_label, self.preview_right_text)
        self._current_preview_entries = (left_entry, right_entry)
        self._debug_logger.debug(
            "preview entries left=%s right=%s",
            getattr(left_entry, "path", None),
            getattr(right_entry, "path", None),
        )

    def _on_tree_click(self, event):
        # detect clicks on the checkbox column (#1)
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if col == "#1":
            # toggle checkbox state
            vals = list(self.tree.item(row, "values"))
            if not vals:
                return
            cur = vals[0]
            vals[0] = "☑" if cur != "☑" else "☐"
            self.tree.item(row, values=vals)
            return "break"

    def _check_all(self):
        for iid in self.tree.get_children(""):
            vals = list(self.tree.item(iid, "values"))
            if vals:
                vals[0] = "☑"
                self.tree.item(iid, values=vals)

    def _uncheck_all(self):
        for iid in self.tree.get_children(""):
            vals = list(self.tree.item(iid, "values"))
            if vals:
                vals[0] = "☐"
                self.tree.item(iid, values=vals)

    def _delete_selected(self):
        # Use checked boxes to determine which files to delete within the selected group
        group_idx = self.grp_list.curselection()
        if not group_idx:
            messagebox.showinfo("No group", "Select a duplicate group first")
            return
        g = self.result_groups[group_idx[0]]
        # collect iids that are checked (☑)
        checked = []
        for iid in self.tree.get_children(""):
            vals = self.tree.item(iid, "values")
            if vals and vals[0] == "☑":
                try:
                    checked.append(int(iid))
                except Exception:
                    pass
        if not checked:
            messagebox.showinfo("No files checked", "Check files to delete using the checkbox column")
            return
        checked_paths = []
        for idx in checked:
            try:
                checked_paths.append(g["files"][idx].path)
            except Exception:
                pass
        preview_paths = checked_paths[:8]
        more = len(checked_paths) - len(preview_paths)
        details = "\n".join(preview_paths)
        if more > 0:
            details += f"\n... and {more} more"
        # confirm
        ok = messagebox.askyesno(
            "Confirm delete",
            f"Permanently delete {len(checked)} checked file(s)?\n\n{details}",
        )
        if not ok:
            return
        to_remove = sorted(checked, reverse=True)
        for idx in to_remove:
            entry = g["files"][idx]
            try:
                os.remove(entry.path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                msg = str(exc)
                if "cannot find the path" in msg.lower() or "no such file" in msg.lower():
                    pass
                else:
                    messagebox.showerror("Delete failed", f"{entry.path}: {exc}")
                    continue
            del g["files"][idx]
        # refresh view
        if len(g["files"]) < 2:
            del self.result_groups[group_idx[0]]
            self.grp_list.delete(group_idx[0])
            self.tree.delete(*self.tree.get_children())
            self._clear_preview()
        else:
            self._on_group_select(None)
        self._rebuild_conflict_list()

    def _delete_others(self):
        group_idx = self.grp_list.curselection()
        if not group_idx:
            messagebox.showinfo("No group", "Select a duplicate group first")
            return
        gi = group_idx[0]
        g = self.result_groups[gi]
        keep_index = g.get("suggested", 0)
        keep = g["files"][keep_index]
        ok = messagebox.askyesno("Confirm", f"Permanently delete all files in group except:\n{keep.path}")
        if not ok:
            return
        for entry in list(g["files"]):
            if entry.path == keep.path:
                continue
            try:
                os.remove(entry.path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                msg = str(exc)
                if "cannot find the path" in msg.lower() or "no such file" in msg.lower():
                    pass
                else:
                    messagebox.showerror("Delete failed", f"{entry.path}: {exc}")
        # keep only the kept file
        g["files"] = [keep]
        # remove group from lists since no duplicates remain
        del self.result_groups[gi]
        self.grp_list.delete(gi)
        self.tree.delete(*self.tree.get_children())
        self._clear_preview()
        self._rebuild_conflict_list()


def main():
    app = DuplicateFinderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
