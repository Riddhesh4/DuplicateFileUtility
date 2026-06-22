import threading
import time
from types import SimpleNamespace

import pytest

from duplicate_finder import gui


def test_gui_starts_and_stops():
    # instantiate the GUI briefly and destroy without running full mainloop
    app = gui.DuplicateFinderApp()
    # perform a single update cycle and then destroy
    app.update_idletasks()
    app.destroy()


def test_group_selection_populates_preview_and_conflicts(monkeypatch):
    monkeypatch.setattr(gui.thumbnail, "make_thumbnail", lambda path, size=(256, 256): None)

    app = gui.DuplicateFinderApp()
    now = time.time()
    app.result_groups = [
        {
            "type": "exact",
            "suggested": 0,
            "files": [
                SimpleNamespace(path=r"D:\A\file1.mp4", size=123, mtime=now),
                SimpleNamespace(path=r"D:\B\file1-copy.mp4", size=123, mtime=now),
            ],
        },
        {
            "type": "exact",
            "suggested": 0,
            "files": [
                SimpleNamespace(path=r"D:\A\file2.mp4", size=456, mtime=now),
                SimpleNamespace(path=r"D:\B\file2-copy.mp4", size=456, mtime=now),
            ],
        },
    ]
    app.active_scan_paths = [r"D:\A", r"D:\B"]

    app._on_scan_done(app.result_groups)
    app.update_idletasks()
    app.grp_list.selection_clear(0, "end")
    app.grp_list.selection_set(0)
    app._on_group_select(None)
    app.update_idletasks()

    assert app.tree.get_children() == ("0", "1")
    assert app.conflict_tree.get_children()
    assert app.preview_left_text.cget("text")
    assert app.preview_right_text.cget("text")

    app.destroy()


def test_clear_paths_also_clears_results(monkeypatch):
    monkeypatch.setattr(gui.thumbnail, "make_thumbnail", lambda path, size=(256, 256): None)

    app = gui.DuplicateFinderApp()
    now = time.time()
    app.result_groups = [
        {
            "type": "exact",
            "suggested": 0,
            "files": [
                SimpleNamespace(path=r"D:\A\file1.mp4", size=123, mtime=now),
                SimpleNamespace(path=r"D:\B\file1-copy.mp4", size=123, mtime=now),
            ],
        }
    ]
    app.active_scan_paths = [r"D:\A", r"D:\B"]
    app._on_scan_done(app.result_groups)
    app.path_vars[0].set(r"D:\A")
    app.update_idletasks()

    app._clear_paths()
    app.update_idletasks()

    assert app.grp_list.size() == 0
    assert app.tree.get_children() == ()
    assert app.conflict_tree.get_children() == ()
    assert app.status.get() == "Ready"
    assert app.active_scan_paths == []

    app.destroy()


def test_clear_keeps_required_empty_path_highlight(monkeypatch):
    monkeypatch.setattr(gui.thumbnail, "make_thumbnail", lambda path, size=(256, 256): None)

    app = gui.DuplicateFinderApp()
    app.path_vars[0].set(r"D:\A")
    app.update_idletasks()

    app._clear_paths()
    app.update_idletasks()

    assert app.path_vars[0].get() == ""
    assert app.path_entries[0].cget("bg") == "#fff0f0"

    app.destroy()


def test_optional_second_path_stays_default_color_when_empty(monkeypatch):
    monkeypatch.setattr(gui.thumbnail, "make_thumbnail", lambda path, size=(256, 256): None)

    app = gui.DuplicateFinderApp()
    app._add_path()
    app.path_vars[0].set(r"D:\A")
    app.path_vars[1].set(r"D:\B")
    app.update_idletasks()

    app._clear_paths()
    app.update_idletasks()

    assert app.path_vars[1].get() == ""
    assert app.path_entries[1] is None
    assert app.path_frames[1] is None
    assert app.path_entries[0].cget("bg") == "#fff0f0"

    app.destroy()
