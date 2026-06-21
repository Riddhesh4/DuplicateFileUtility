"""Tkinter GUI for Duplicate Finder with improved UX and progress reporting."""
from __future__ import annotations

import threading
import queue
import os
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox
from . import scanner
from . import thumbnail

# permanent delete behavior is preferred for this app


class DuplicateFinderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Duplicate Finder")
        self.geometry("1000x650")

        self.max_paths = 6
        self.initial_visible = 2
        self.path_vars = [tk.StringVar() for _ in range(self.max_paths)]
        self.path_entries: list[tk.Entry | None] = [None] * self.max_paths
        self.path_frames: list[tk.Frame] = []
        self.visible_count = 0

        # nicer label font (falls back if unavailable)
        try:
            self.label_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        except Exception:
            self.label_font = None

        self._scan_thread = None
        self._queue = None

        self._build_ui()

        self.result_groups = []
        self.thumb_cache = {}

        # progress counting for determinate bar
        self._progress_count = 0

    def _build_ui(self):
        top = ttk.LabelFrame(self, text=f"Folders to scan (max {self.max_paths})")
        top.pack(fill="x", padx=8, pady=6)

        self.paths_container = ttk.Frame(top)
        self.paths_container.pack(fill="x", padx=2, pady=2)

        # create initial visible rows
        for i in range(self.initial_visible):
            self._create_path_row(i)

        # Add button (eye-catching)
        add_btn = tk.Button(top, text="Add folder", bg="#28a745", fg="white", activebackground="#218838", command=self._add_path)
        add_btn.pack(anchor="e", padx=6, pady=(2, 6))

        # control buttons
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=8)
        self.scan_btn = tk.Button(ctrl, text="Scan", bg="#007bff", fg="white", command=self.start_scan)
        self.scan_btn.pack(side="left")
        ttk.Button(ctrl, text="Clear", command=self._clear_paths).pack(side="left", padx=6)
        ttk.Button(ctrl, text="Quit", command=self.destroy).pack(side="right")

        # status + progress
        self.status = tk.StringVar(value="Ready")
        self.pbar = ttk.Progressbar(self, mode="indeterminate")
        self.pbar.pack(fill="x", padx=8, pady=4)
        ttk.Label(self, textvariable=self.status).pack(fill="x", padx=8)

        main = ttk.PanedWindow(self, orient="horizontal")
        main.pack(fill="both", expand=True, padx=8, pady=6)

        # Left: groups
        left = ttk.Frame(main)
        main.add(left, weight=1)
        ttk.Label(left, text="Duplicate Groups").pack(anchor="w")
        self.grp_list = tk.Listbox(left, height=20)
        self.grp_list.pack(fill="both", expand=True)
        self.grp_list.bind("<<ListboxSelect>>", self._on_group_select)

        # Middle: files in selected group
        mid = ttk.Frame(main)
        main.add(mid, weight=3)
        ttk.Label(mid, text="Files in group").pack(anchor="w")
        cols = ("selected", "name", "size", "modified", "recommended")
        # first column is a checkbox indicator for bulk operations
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("selected", text="")
        self.tree.heading("name", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("modified", text="Modified")
        self.tree.heading("recommended", text="Recommended")
        self.tree.column("selected", width=40, anchor="center")
        self.tree.column("name", width=400)
        self.tree.column("size", width=80, anchor="e")
        self.tree.column("modified", width=140)
        self.tree.column("recommended", width=100, anchor="center")
        self.tree.pack(fill="both", expand=True)
        # clicking the first column toggles the checkbox; selection shows side-by-side preview
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_file_select)

        btns = ttk.Frame(mid)
        btns.pack(fill="x")
        ttk.Button(btns, text="Delete Checked", command=self._delete_selected).pack(side="left")
        ttk.Button(btns, text="Check All", command=self._check_all).pack(side="left", padx=6)
        ttk.Button(btns, text="Uncheck All", command=self._uncheck_all).pack(side="left")
        ttk.Button(btns, text="Keep Suggested / Delete Others", command=self._delete_others).pack(side="left", padx=6)

        # Right: thumbnail preview
        right = ttk.Frame(main)
        main.add(right, weight=1)
        ttk.Label(right, text="Preview (select up to 2 files to compare)").pack(anchor="w")
        preview_container = ttk.Frame(right)
        preview_container.pack(fill="both", expand=True)
        left_preview = ttk.Frame(preview_container, borderwidth=1, relief="solid")
        right_preview = ttk.Frame(preview_container, borderwidth=1, relief="solid")
        left_preview.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        right_preview.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        self.preview_left_label = ttk.Label(left_preview)
        self.preview_left_label.pack(fill="both", expand=True)
        self.preview_left_text = ttk.Label(left_preview, wraplength=200, justify="center")
        self.preview_left_text.pack(fill="x", pady=4)
        self.preview_right_label = ttk.Label(right_preview)
        self.preview_right_label.pack(fill="both", expand=True)
        self.preview_right_text = ttk.Label(right_preview, wraplength=200, justify="center")
        self.preview_right_text.pack(fill="x", pady=4)

    def _create_path_row(self, idx: int):
        # create a single path row; can be called to add more rows dynamically
        frm = ttk.Frame(self.paths_container)
        frm.pack(fill="x", padx=2, pady=2)
        # nicer label and entry (first two are required)
        lbl_text = f"Folder {idx+1}"
        lbl = tk.Label(frm, text=lbl_text, font=self.label_font, fg="#222")
        lbl.pack(side="left", padx=(0, 6))
        e = tk.Entry(frm, textvariable=self.path_vars[idx])
        e.pack(side="left", fill="x", expand=True)
        b = ttk.Button(frm, text="Browse", command=lambda v=self.path_vars[idx]: self._browse(v))
        b.pack(side="left", padx=4)

        # store widgets for validation and styling
        self.path_frames.append(frm)
        self.path_entries[idx] = e
        if getattr(self, "default_entry_bg", None) is None:
            try:
                self.default_entry_bg = e.cget("bg")
            except Exception:
                self.default_entry_bg = "white"

        # watch for changes to update validation state
        try:
            self.path_vars[idx].trace_add("write", lambda *a, i=idx: self._on_path_change(i))
        except Exception:
            # older tkinter fallback
            self.path_vars[idx].trace("w", lambda *a, i=idx: self._on_path_change(i))

        self.visible_count += 1

        # ensure initial validation state
        self._on_path_change(idx)

    def _add_path(self):
        if self.visible_count >= self.max_paths:
            messagebox.showinfo("Limit reached", f"Maximum of {self.max_paths} folders")
            return
        self._create_path_row(self.visible_count)

    def _browse(self, var: tk.StringVar):
        d = filedialog.askdirectory()
        if d:
            var.set(d)

    def _clear_paths(self):
        for v in self.path_vars:
            v.set("")
        for e in self.path_entries:
            if e is not None:
                try:
                    e.config(bg=getattr(self, "default_entry_bg", "white"))
                except Exception:
                    pass
        self._update_scan_button_state()

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

        paths = [v.get() for v in self.path_vars if v.get().strip()]
        if not paths:
            messagebox.showwarning("No paths", "Please add at least one folder to scan")
            return

        # disable UI
        self.scan_btn.config(state="disabled")
        self.pbar.config(mode="indeterminate")
        self.pbar.start()
        self.status.set("Scanning...")
        self.grp_list.delete(0, "end")
        self.tree.delete(*self.tree.get_children())
        self._clear_preview()
        self.result_groups = []
        self.thumb_cache.clear()

        self._queue = queue.Queue()
        t = threading.Thread(target=self._scan_worker, args=(paths, self._queue), daemon=True)
        self._scan_thread = t
        t.start()
        self.after(100, self._process_queue)

    def _on_path_change(self, idx: int):
        # called when a path variable changes; update visual validation and scan button state
        val = self.path_vars[idx].get().strip()
        e = self.path_entries[idx]
        if idx < 2 and e is not None:
            try:
                e.config(bg=self.default_entry_bg if val else "#fff0f0")
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

    def _scan_worker(self, paths, q: queue.Queue):
        def cb(msg):
            # structured messages from scanner are dicts with a 'type' key
            if isinstance(msg, dict) and "type" in msg:
                q.put((msg["type"], msg))
            else:
                q.put(("status", str(msg)))

        try:
            res = scanner.find_duplicates(paths, workers=None, progress_callback=cb)
            q.put(("done", res))
        except Exception as exc:
            q.put(("error", {"text": str(exc)}))

    def _process_queue(self):
        try:
            while not self._queue.empty():
                typ, payload = self._queue.get_nowait()
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
        self.status.set(f"Found {len(groups)} duplicate groups")
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
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(group["files"]):
            name = os.path.basename(e.path)
            size = f"{e.size:,}"
            mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.mtime))
            recommended = "YES" if i == group.get("suggested", -1) else ""
            # initial checkbox state unchecked
            self.tree.insert("", "end", iid=str(i), values=("☐", name, size, mtime, recommended))

        self._clear_preview()
        self.tree.selection_set(str(group.get("suggested", 0)))
        self._update_preview(group, [str(group.get("suggested", 0))])

    def _on_file_select(self, evt):
        group_idx = self.grp_list.curselection()
        if not group_idx:
            return
        group = self.result_groups[group_idx[0]]
        sel = self.tree.selection()
        self._update_preview(group, sel)

    def _show_preview_entry(self, entry: FileEntry, image_label: ttk.Label, text_label: ttk.Label):
        img = self.thumb_cache.get(entry.path)
        if img is None:
            img = thumbnail.make_thumbnail(entry.path, size=(320, 320))
            self.thumb_cache[entry.path] = img
        if img is not None:
            image_label.config(image=img, text="")
            image_label.image = img
        else:
            image_label.config(image="", text=os.path.basename(entry.path))
            image_label.image = None
        text_label.config(text=f"{os.path.basename(entry.path)}\n{entry.path}")

    def _clear_preview(self):
        for lbl in (self.preview_left_label, self.preview_right_label):
            lbl.config(image="", text="")
            lbl.image = None
        self.preview_left_text.config(text="")
        self.preview_right_text.config(text="")

    def _update_preview(self, group: dict, sel_ids: list[str]) -> None:
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
        if selected:
            self._show_preview_entry(group["files"][selected[0]], self.preview_left_label, self.preview_left_text)
        if len(selected) > 1:
            self._show_preview_entry(group["files"][selected[1]], self.preview_right_label, self.preview_right_text)
        elif len(group["files"]) > 1:
            other_idx = 1 if selected[0] == 0 else 0
            self._show_preview_entry(group["files"][other_idx], self.preview_right_label, self.preview_right_text)

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
        # confirm
        ok = messagebox.askyesno("Confirm delete", "Permanently delete checked files?")
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


def main():
    app = DuplicateFinderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
