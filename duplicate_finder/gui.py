"""Tkinter GUI for Duplicate Finder with improved UX and progress reporting."""
from __future__ import annotations

import threading
import queue
import os
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from . import scanner
from . import thumbnail

try:
    from send2trash import send2trash
except Exception:
    send2trash = None


class DuplicateFinderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Duplicate Finder")
        self.geometry("1000x650")

        self.max_paths = 6
        self.initial_visible = 2
        self.path_vars = [tk.StringVar() for _ in range(self.max_paths)]
        self.path_frames: list[tk.Frame] = []
        self.visible_count = 0

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

        # Instructions / required marker
        instr = tk.Label(top, text="* Required (at least two folders)", fg="red")
        instr.pack(anchor="w", padx=6, pady=(4, 2))

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
        cols = ("name", "size", "modified", "recommended")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        for c in cols:
            self.tree.heading(c, text=c.title())
        self.tree.column("name", width=400)
        self.tree.column("size", width=80, anchor="e")
        self.tree.column("modified", width=140)
        self.tree.column("recommended", width=100, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_file_select)

        btns = ttk.Frame(mid)
        btns.pack(fill="x")
        ttk.Button(btns, text="Delete Selected", command=self._delete_selected).pack(side="left")
        ttk.Button(btns, text="Keep Suggested / Delete Others", command=self._delete_others).pack(side="left", padx=6)

        # Right: thumbnail
        right = ttk.Frame(main)
        main.add(right, weight=1)
        ttk.Label(right, text="Preview").pack(anchor="w")
        self.preview_label = ttk.Label(right)
        self.preview_label.pack(fill="both", expand=True)

    def _create_path_row(self, idx: int):
        # create a single path row; can be called to add more rows dynamically
        frm = ttk.Frame(self.paths_container)
        frm.pack(fill="x", padx=2, pady=2)
        # mark first two rows as required
        if idx < 2:
            lbl = tk.Label(frm, text=f"Path {idx+1} *", fg="red")
            lbl.pack(side="left", padx=(0, 6))
        else:
            lbl = tk.Label(frm, text=f"Path {idx+1}")
            lbl.pack(side="left", padx=(0, 6))
        e = ttk.Entry(frm, textvariable=self.path_vars[idx])
        e.pack(side="left", fill="x", expand=True)
        b = ttk.Button(frm, text="Browse", command=lambda v=self.path_vars[idx]: self._browse(v))
        b.pack(side="left", padx=4)
        self.path_frames.append(frm)
        self.visible_count += 1

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

    def start_scan(self):
        # require first two paths
        if not self.path_vars[0].get() or not self.path_vars[1].get():
            messagebox.showwarning("Paths required", "Please provide at least two folders (marked with *)")
            return

        paths = [v.get() for v in self.path_vars if v.get()]
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
        self.preview_label.config(image="", text="")
        self.result_groups = []
        self.thumb_cache.clear()

        self._queue = queue.Queue()
        t = threading.Thread(target=self._scan_worker, args=(paths, self._queue), daemon=True)
        self._scan_thread = t
        t.start()
        self.after(100, self._process_queue)

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
            self.tree.insert("", "end", iid=str(i), values=(name, size, mtime, recommended))

    def _on_file_select(self, evt):
        sel = self.tree.selection()
        if not sel:
            return
        # pick first selected
        iid = sel[0]
        group_idx = self.grp_list.curselection()
        if not group_idx:
            return
        group = self.result_groups[group_idx[0]]
        try:
            entry = group["files"][int(iid)]
        except Exception:
            return
        img = self.thumb_cache.get(entry.path)
        if img is None:
            img = thumbnail.make_thumbnail(entry.path, size=(320, 320))
            self.thumb_cache[entry.path] = img
        if img is not None:
            self.preview_label.config(image=img)
            self.preview_label.image = img
        else:
            self.preview_label.config(text=os.path.basename(entry.path))

    def _delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select files to delete")
            return
        if send2trash is None:
            ok = messagebox.askyesno("Confirm delete", "send2trash is not installed; delete permanently?")
        else:
            ok = messagebox.askyesno("Confirm delete", "Send selected files to Recycle Bin?")
        if not ok:
            return
        group_idx = self.grp_list.curselection()
        if not group_idx:
            return
        g = self.result_groups[group_idx[0]]
        to_remove = sorted([int(iid) for iid in sel], reverse=True)
        for idx in to_remove:
            entry = g["files"][idx]
            try:
                if send2trash:
                    send2trash(entry.path)
                else:
                    os.remove(entry.path)
            except Exception as exc:
                messagebox.showerror("Delete failed", f"{entry.path}: {exc}")
                continue
            # remove from group
            del g["files"][idx]
        # refresh view
        if len(g["files"]) < 2:
            # remove the group entirely
            del self.result_groups[group_idx[0]]
            self.grp_list.delete(group_idx[0])
            self.tree.delete(*self.tree.get_children())
            self.preview_label.config(image="", text="")
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
        ok = messagebox.askyesno("Confirm", f"Delete all files in group except:\n{keep.path}")
        if not ok:
            return
        for i, entry in enumerate(list(g["files"])):
            if entry.path == keep.path:
                continue
            try:
                if send2trash:
                    send2trash(entry.path)
                else:
                    os.remove(entry.path)
            except Exception as exc:
                messagebox.showerror("Delete failed", f"{entry.path}: {exc}")
        # keep only the kept file
        g["files"] = [keep]
        # remove group from lists since no duplicates remain
        del self.result_groups[gi]
        self.grp_list.delete(gi)
        self.tree.delete(*self.tree.get_children())
        self.preview_label.config(image="", text="")


def main():
    app = DuplicateFinderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
