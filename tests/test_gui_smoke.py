import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication

from duplicate_finder import gui


def _app():
    return QApplication.instance() or QApplication([])


def test_gui_starts_and_stops():
    _ = _app()
    win = gui.DuplicateFinderWindow()
    win.show()
    win.close()


def test_group_population_and_clear_search():
    _ = _app()
    win = gui.DuplicateFinderWindow()
    now = time.time()

    win._on_done(
        [
            {
                "type": "exact",
                "hash": "abc",
                "suggested": 0,
                "files": [
                    SimpleNamespace(path=r"D:\A\file1.mp4", size=123, mtime=now, full_hash="abc", fast_hash="f1"),
                    SimpleNamespace(path=r"D:\B\file1-copy.mp4", size=123, mtime=now, full_hash="abc", fast_hash="f1"),
                ],
            }
        ]
    )

    assert win.results_tree.topLevelItemCount() == 1
    assert win.results_tree.topLevelItem(0).childCount() == 2

    win.clear_search()
    assert win.results_tree.topLevelItemCount() == 0

    win.close()


def test_folder_constraints():
    _ = _app()
    win = gui.DuplicateFinderWindow()

    for _i in range(win.max_paths - 1):
        win._add_path()

    assert len(win.path_rows) == 6
    win._remove_path(win.path_rows[-1].container)
    assert len(win.path_rows) == 5

    win.close()


def test_progress_text_uses_counts_not_stuck_percent():
    _ = _app()
    win = gui.DuplicateFinderWindow()

    win._on_total("fast", 2000, "Fast hash pass")
    win._on_file_progress("fast", r"D:\A\video1.mp4")
    win._on_file_progress("fast", r"D:\A\video2.mp4")

    assert win.progress.format() == "Fast hash: 2 / 2,000"
    assert win.progress.value() == 2

    win.close()
