"""PyQt6 GUI for DuplicateFinder with a glass-style interface and threaded scanning."""
from __future__ import annotations

import os
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QLinearGradient
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsBlurEffect,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QFileDialog,
)

from . import scanner

APP_NAME = "DuplicateFinder"
APP_VERSION = "v1.0.0"


@dataclass
class PathRow:
    container: QWidget
    line_edit: QLineEdit
    browse_btn: QPushButton
    remove_btn: QPushButton


class GradientBackdrop(QWidget):
    """Paint a soft gradient background so blur/frost effects remain visible."""

    def paintEvent(self, event):  # noqa: N802 (Qt naming)
        _ = event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        grad = QLinearGradient(0, 0, self.width(), self.height())
        grad.setColorAt(0.0, QColor(16, 28, 44))
        grad.setColorAt(0.45, QColor(23, 52, 76))
        grad.setColorAt(1.0, QColor(21, 34, 52))
        painter.fillRect(self.rect(), grad)


class ScanWorker(QObject):
    status = pyqtSignal(str)
    total = pyqtSignal(str, int, str)  # stage, total, text
    file_progress = pyqtSignal(str, str)  # stage, path
    done = pyqtSignal(list)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, paths: list[str]):
        super().__init__()
        self.paths = paths

    def _emit_progress(self, msg):
        if not isinstance(msg, dict):
            self.status.emit(str(msg))
            return
        kind = msg.get("type", "")
        if kind == "status":
            self.status.emit(msg.get("text", ""))
        elif kind == "total":
            self.total.emit(msg.get("stage", ""), int(msg.get("total", 0)), msg.get("text", ""))
        elif kind == "file":
            self.file_progress.emit(msg.get("stage", ""), msg.get("path", ""))

    def run(self):
        try:
            files = scanner._collect_files(self.paths)
            self.status.emit(f"Collected {len(files):,} files - analyzing content...")
            groups = scanner.find_duplicates(
                self.paths,
                workers=None,
                progress_callback=self._emit_progress,
                files=files,
            )
            self.done.emit(groups)
        except Exception as exc:
            self.error.emit(f"{exc}\n\n{traceback.format_exc()}")
        finally:
            self.finished.emit()


class DuplicateFinderWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 760)
        self.setMinimumSize(980, 640)

        self.max_paths = 6
        self.path_rows: list[PathRow] = []
        self.result_groups = []

        self.scan_thread: Optional[QThread] = None
        self.scan_worker: Optional[ScanWorker] = None
        self.progress_stage = ""
        self.progress_count = 0

        self._build_ui()
        self._create_path_row(0)
        self._update_controls_state()

    def _build_ui(self):
        root = GradientBackdrop()
        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(20, 20, 20, 16)
        outer.setSpacing(14)

        # Decorative blur layer to provide a frosted feel.
        blur_layer = QLabel(root)
        blur_layer.setFixedHeight(150)
        blur_layer.setStyleSheet(
            "background: qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            "stop:0 rgba(255,255,255,55), stop:1 rgba(255,255,255,10));"
            "border-radius: 24px;"
        )
        blur_effect = QGraphicsBlurEffect(self)
        blur_effect.setBlurRadius(24)
        blur_layer.setGraphicsEffect(blur_effect)
        outer.addWidget(blur_layer)

        self.card = QFrame(root)
        self.card.setObjectName("GlassCard")
        self.card.setStyleSheet(
            "QFrame#GlassCard {"
            "  background: rgba(245, 250, 255, 0.14);"
            "  border: 1px solid rgba(255, 255, 255, 0.30);"
            "  border-radius: 20px;"
            "}"
            "QLabel#HeaderTitle { color: #f6fbff; font-size: 30px; font-weight: 700; }"
            "QLabel#HeaderSub { color: rgba(240,248,255,0.82); font-size: 13px; }"
            "QLabel#Footer { color: rgba(230,240,255,0.78); font-size: 12px; }"
            "QPushButton {"
            "  background: rgba(255,255,255,0.16);"
            "  color: #f5f9ff; border: 1px solid rgba(255,255,255,0.34);"
            "  border-radius: 12px; padding: 8px 14px;"
            "}"
            "QPushButton:hover { background: rgba(255,255,255,0.24); }"
            "QPushButton:disabled { color: rgba(220,230,245,0.50); background: rgba(255,255,255,0.08); }"
            "QLineEdit {"
            "  color: #f7fbff; background: rgba(13,21,34,0.38);"
            "  border: 1px solid rgba(255,255,255,0.26); border-radius: 10px; padding: 8px;"
            "}"
            "QProgressBar {"
            "  color: #f5f9ff; text-align: center; background: rgba(10,18,30,0.30);"
            "  border: 1px solid rgba(255,255,255,0.22); border-radius: 8px;"
            "}"
            "QProgressBar::chunk {"
            "  border-radius: 7px;"
            "  background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "   stop:0 rgba(92,196,255,0.88), stop:1 rgba(70,138,255,0.88));"
            "}"
            "QTreeWidget {"
            "  color: #eff7ff; background: rgba(10,18,30,0.30);"
            "  border: 1px solid rgba(255,255,255,0.22); border-radius: 12px;"
            "  outline: none;"
            "}"
            "QHeaderView::section {"
            "  background: rgba(255,255,255,0.10); color: #f6fbff;"
            "  border: none; padding: 6px;"
            "}"
        )
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 16)
        shadow.setColor(QColor(0, 0, 0, 140))
        self.card.setGraphicsEffect(shadow)
        outer.addWidget(self.card, 1)

        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(18, 14, 18, 12)
        layout.setSpacing(12)

        header = QWidget(self.card)
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        title = QLabel(APP_NAME)
        title.setObjectName("HeaderTitle")
        subtitle = QLabel("Content-based duplicate detection for large media files")
        subtitle.setObjectName("HeaderSub")
        title_col = QVBoxLayout()
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header_row.addLayout(title_col)
        header_row.addStretch(1)
        layout.addWidget(header)

        path_panel = QFrame(self.card)
        path_panel.setStyleSheet(
            "QFrame { background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.16); border-radius: 14px; }"
        )
        path_layout = QVBoxLayout(path_panel)
        path_layout.setContentsMargins(12, 12, 12, 12)
        path_layout.setSpacing(8)

        paths_head = QHBoxLayout()
        head_label = QLabel("Folders to scan (max 6, Folder 1 required)")
        head_label.setStyleSheet("color: #f2f8ff; font-weight: 600;")
        self.add_btn = QPushButton("Add Folder")
        self.add_btn.clicked.connect(self._add_path)
        paths_head.addWidget(head_label)
        paths_head.addStretch(1)
        paths_head.addWidget(self.add_btn)
        path_layout.addLayout(paths_head)

        self.paths_holder = QVBoxLayout()
        self.paths_holder.setSpacing(6)
        path_layout.addLayout(self.paths_holder)

        layout.addWidget(path_panel)

        controls = QHBoxLayout()
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(self.start_scan)
        self.clear_search_btn = QPushButton("Clear Search")
        self.clear_search_btn.clicked.connect(self.clear_search)
        self.delete_btn = QPushButton("Delete Selected")
        self.delete_btn.clicked.connect(self.delete_selected)
        controls.addWidget(self.scan_btn)
        controls.addWidget(self.clear_search_btn)
        controls.addWidget(self.delete_btn)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #e8f2ff;")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress)

        self.results_tree = QTreeWidget()
        self.results_tree.setColumnCount(6)
        self.results_tree.setHeaderLabels(["Keep", "Group", "Name", "Path", "Size", "Hash"])
        self.results_tree.setAlternatingRowColors(True)
        self.results_tree.setRootIsDecorated(True)
        self.results_tree.setUniformRowHeights(True)
        self.results_tree.header().setStretchLastSection(False)
        self.results_tree.header().resizeSection(0, 52)
        self.results_tree.header().resizeSection(1, 80)
        self.results_tree.header().resizeSection(2, 230)
        self.results_tree.header().resizeSection(3, 520)
        self.results_tree.header().resizeSection(4, 110)
        self.results_tree.header().resizeSection(5, 290)
        layout.addWidget(self.results_tree, 1)

        footer = QLabel(f"{APP_NAME} {APP_VERSION} | Build-ready for Windows 10/11")
        footer.setObjectName("Footer")
        footer.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(footer)

    def _create_path_row(self, index: int):
        row_widget = QWidget(self.card)
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        title = QLabel(f"Folder {index + 1}")
        title.setStyleSheet("color: #edf5ff; font-weight: 600;")
        title.setFixedWidth(72)

        line_edit = QLineEdit()
        line_edit.setPlaceholderText("Select folder path...")
        line_edit.textChanged.connect(self._update_controls_state)

        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(lambda: self._browse_folder(line_edit))
        browse_btn.setFixedWidth(88)

        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(lambda: self._remove_path(row_widget))
        remove_btn.setFixedWidth(88)
        if index == 0:
            remove_btn.setEnabled(False)

        row.addWidget(title)
        row.addWidget(line_edit, 1)
        row.addWidget(browse_btn)
        row.addWidget(remove_btn)
        self.paths_holder.addWidget(row_widget)

        self.path_rows.append(
            PathRow(
                container=row_widget,
                line_edit=line_edit,
                browse_btn=browse_btn,
                remove_btn=remove_btn,
            )
        )
        self._update_controls_state()

    def _remove_path(self, widget: QWidget):
        if len(self.path_rows) <= 1:
            return
        for idx, row in enumerate(self.path_rows):
            if row.container is widget:
                self.path_rows.pop(idx)
                row.container.setParent(None)
                row.container.deleteLater()
                break
        self._reindex_path_labels()
        self._update_controls_state()

    def _reindex_path_labels(self):
        for idx, row in enumerate(self.path_rows):
            title = row.container.layout().itemAt(0).widget()
            if isinstance(title, QLabel):
                title.setText(f"Folder {idx + 1}")
            row.remove_btn.setEnabled(idx != 0)

    def _browse_folder(self, target_line: QLineEdit):
        path = QFileDialog.getExistingDirectory(self, "Select Folder")
        if path:
            target_line.setText(path)

    def _add_path(self):
        if len(self.path_rows) >= self.max_paths:
            QMessageBox.information(self, "Limit reached", f"Maximum of {self.max_paths} folders is allowed.")
            return
        self._create_path_row(len(self.path_rows))

    def _collect_paths(self) -> list[str]:
        return [row.line_edit.text().strip() for row in self.path_rows if row.line_edit.text().strip()]

    def _update_controls_state(self):
        first_ok = bool(self.path_rows and self.path_rows[0].line_edit.text().strip())
        self.scan_btn.setEnabled(first_ok and self.scan_thread is None)
        self.add_btn.setEnabled(len(self.path_rows) < self.max_paths)

    def clear_search(self):
        self.result_groups = []
        self.results_tree.clear()
        self.progress_stage = ""
        self.progress_count = 0
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status_label.setText("Ready")

    def start_scan(self):
        if not self.path_rows[0].line_edit.text().strip():
            QMessageBox.warning(self, "Path required", "Folder 1 is required.")
            self.path_rows[0].line_edit.setFocus()
            return

        paths = self._collect_paths()
        invalid = [p for p in paths if not os.path.isdir(p)]
        if invalid:
            QMessageBox.critical(self, "Invalid folder(s)", "These paths are invalid:\n\n" + "\n".join(invalid))
            return

        self.clear_search()
        self.scan_btn.setEnabled(False)
        self.status_label.setText("Starting scan...")
        self.progress.setRange(0, 0)  # indeterminate until first total arrives

        self.scan_worker = ScanWorker(paths)
        self.scan_thread = QThread(self)
        self.scan_worker.moveToThread(self.scan_thread)

        self.scan_thread.started.connect(self.scan_worker.run)
        self.scan_worker.status.connect(self._on_status)
        self.scan_worker.total.connect(self._on_total)
        self.scan_worker.file_progress.connect(self._on_file_progress)
        self.scan_worker.done.connect(self._on_done)
        self.scan_worker.error.connect(self._on_error)
        self.scan_worker.finished.connect(self._on_scan_finished)
        self.scan_worker.finished.connect(self.scan_thread.quit)
        self.scan_worker.finished.connect(self.scan_worker.deleteLater)
        self.scan_thread.finished.connect(self.scan_thread.deleteLater)

        self.scan_thread.start()

    def _on_status(self, text: str):
        self.status_label.setText(text or "Scanning...")

    def _on_total(self, stage: str, total: int, text: str):
        self.progress_stage = stage
        self.progress_count = 0
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(0)
        if text:
            self.status_label.setText(text)

    def _on_file_progress(self, stage: str, path: str):
        if not self.progress_stage or stage == self.progress_stage:
            self.progress_count += 1
            self.progress.setValue(self.progress_count)
        name = os.path.basename(path) if path else ""
        if name:
            self.status_label.setText(f"{stage.title()} {name}")

    def _on_done(self, groups: list):
        self.result_groups = groups
        self._populate_results(groups)
        if groups:
            self.status_label.setText(f"Found {len(groups)} duplicate group(s)")
        else:
            self.status_label.setText("No duplicates found")

    def _on_error(self, text: str):
        QMessageBox.critical(self, "Scan error", text)
        self.status_label.setText("Scan failed")

    def _on_scan_finished(self):
        self.progress_stage = ""
        if self.progress.maximum() > 0:
            self.progress.setValue(min(self.progress.maximum(), self.progress.value()))
        self.scan_thread = None
        self.scan_worker = None
        self._update_controls_state()

    def _populate_results(self, groups: list):
        self.results_tree.clear()
        for gi, group in enumerate(groups, start=1):
            group_label = f"Group {gi} ({group.get('type', 'exact')})"
            top = QTreeWidgetItem(["", group_label, "", "", str(len(group.get("files", []))), group.get("hash") or ""])
            top.setFirstColumnSpanned(False)
            top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self.results_tree.addTopLevelItem(top)

            for file_entry in group.get("files", []):
                name = os.path.basename(file_entry.path)
                size = f"{file_entry.size:,}"
                hash_value = file_entry.full_hash or file_entry.fast_hash or group.get("hash") or "-"
                child = QTreeWidgetItem(["", group_label, name, file_entry.path, size, hash_value])
                child.setCheckState(0, Qt.CheckState.Unchecked)
                child.setData(0, Qt.ItemDataRole.UserRole, file_entry.path)
                top.addChild(child)

            top.setExpanded(True)

    def delete_selected(self):
        to_delete: list[str] = []
        for i in range(self.results_tree.topLevelItemCount()):
            group_item = self.results_tree.topLevelItem(i)
            for j in range(group_item.childCount()):
                child = group_item.child(j)
                if child.checkState(0) == Qt.CheckState.Checked:
                    path = child.data(0, Qt.ItemDataRole.UserRole)
                    if path:
                        to_delete.append(path)

        if not to_delete:
            QMessageBox.information(self, "Nothing selected", "Check files in the result list to delete them.")
            return

        preview = "\n".join(to_delete[:8])
        if len(to_delete) > 8:
            preview += f"\n... and {len(to_delete) - 8} more"

        ok = QMessageBox.question(
            self,
            "Confirm delete",
            f"Permanently delete {len(to_delete)} selected file(s)?\n\n{preview}",
        )
        if ok != QMessageBox.StandardButton.Yes:
            return

        deleted = 0
        for path in sorted(set(to_delete)):
            try:
                os.remove(path)
                deleted += 1
            except FileNotFoundError:
                pass
            except OSError as exc:
                QMessageBox.warning(self, "Delete failed", f"{path}\n\n{exc}")

        if deleted:
            self.status_label.setText(f"Deleted {deleted} file(s)")
            self._remove_deleted_from_results(set(to_delete))

    def _remove_deleted_from_results(self, deleted_paths: set[str]):
        updated = []
        for group in self.result_groups:
            remaining = [f for f in group.get("files", []) if f.path not in deleted_paths]
            if len(remaining) > 1:
                ng = dict(group)
                ng["files"] = remaining
                if ng.get("suggested", 0) >= len(remaining):
                    ng["suggested"] = 0
                updated.append(ng)
        self.result_groups = updated
        self._populate_results(self.result_groups)


# Backwards-compatible alias name used by older code/tests.
DuplicateFinderApp = DuplicateFinderWindow


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    win = DuplicateFinderWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
