"""Chapter editor and local export page."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from novelforge_windows.domain.models import Chapter
from novelforge_windows.services.container import ServiceContainer
from novelforge_windows.services.project_service import safe_export_name


class ChaptersPage(QWidget):
    message = Signal(str)

    def __init__(self, services: ServiceContainer) -> None:
        super().__init__()
        self.services = services
        self.current_chapter_id: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 22)
        title = QLabel("章节管理")
        title.setObjectName("PageTitle")
        subtitle = QLabel("本地编辑章节，并将整部作品导出为 UTF-8 Markdown。")
        subtitle.setObjectName("Muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        toolbar = QHBoxLayout()
        self.project_combo = QComboBox()
        self.project_combo.setMinimumWidth(300)
        self.project_combo.currentIndexChanged.connect(self.refresh_chapters)
        self.new_button = QPushButton("新建章节")
        self.export_button = QPushButton("导出整部作品")
        self.new_button.clicked.connect(self.new_chapter)
        self.export_button.clicked.connect(self.export_project)
        toolbar.addWidget(QLabel("作品"))
        toolbar.addWidget(self.project_combo)
        toolbar.addStretch(1)
        toolbar.addWidget(self.new_button)
        toolbar.addWidget(self.export_button)
        layout.addLayout(toolbar)

        splitter = QSplitter()
        self.chapter_list = QListWidget()
        self.chapter_list.setMinimumWidth(270)
        self.chapter_list.currentItemChanged.connect(self._on_selection_changed)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(18, 0, 0, 0)
        form = QFormLayout()
        self.sequence_no = QSpinBox()
        self.sequence_no.setRange(1, 1_000_000)
        self.chapter_title = QLineEdit()
        self.status_combo = QComboBox()
        self.status_combo.addItem("草稿", "draft")
        self.status_combo.addItem("已生成", "generated")
        self.status_combo.addItem("已完成", "completed")
        self.summary = QTextEdit()
        self.summary.setMaximumHeight(95)
        form.addRow("章节序号", self.sequence_no)
        form.addRow("标题", self.chapter_title)
        form.addRow("状态", self.status_combo)
        form.addRow("摘要", self.summary)
        editor_layout.addLayout(form)
        self.content = QTextEdit()
        self.content.setPlaceholderText("在这里编写或修改章节正文……")
        self.content.textChanged.connect(self._update_character_count)
        editor_layout.addWidget(self.content, 1)

        bottom = QHBoxLayout()
        self.character_count = QLabel("0 字")
        self.character_count.setObjectName("Muted")
        self.delete_button = QPushButton("删除章节")
        self.delete_button.setProperty("danger", True)
        self.save_button = QPushButton("保存章节")
        self.save_button.setProperty("primary", True)
        self.delete_button.clicked.connect(self.delete_chapter)
        self.save_button.clicked.connect(self.save_chapter)
        bottom.addWidget(self.character_count)
        bottom.addStretch(1)
        bottom.addWidget(self.delete_button)
        bottom.addWidget(self.save_button)
        editor_layout.addLayout(bottom)

        splitter.addWidget(self.chapter_list)
        splitter.addWidget(editor)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.refresh_projects()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.refresh_chapters()

    def refresh_projects(self) -> None:
        selected_id = self.project_combo.currentData()
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        selected_index = -1
        for index, project in enumerate(self.services.projects.list_projects()):
            self.project_combo.addItem(project.title, project.id)
            if project.id == selected_id:
                selected_index = index
        self.project_combo.blockSignals(False)
        if selected_index >= 0:
            self.project_combo.setCurrentIndex(selected_index)
        elif self.project_combo.count():
            self.project_combo.setCurrentIndex(0)
        self.refresh_chapters()

    def refresh_chapters(self, preferred_id: str | None = None) -> None:
        project_id = self.project_combo.currentData()
        self.new_button.setEnabled(bool(project_id))
        self.export_button.setEnabled(bool(project_id))
        selected_id = preferred_id if isinstance(preferred_id, str) else self.current_chapter_id
        self.chapter_list.blockSignals(True)
        self.chapter_list.clear()
        selected_row = -1
        if project_id:
            chapters = self.services.chapters.list_chapters(str(project_id))
            for index, chapter in enumerate(chapters):
                item = QListWidgetItem(f"第{chapter.sequence_no}章  {chapter.title}")
                item.setData(Qt.ItemDataRole.UserRole, chapter.id)
                self.chapter_list.addItem(item)
                if chapter.id == selected_id:
                    selected_row = index
        self.chapter_list.blockSignals(False)
        if selected_row >= 0:
            self.chapter_list.setCurrentRow(selected_row)
        elif self.chapter_list.count():
            self.chapter_list.setCurrentRow(0)
        else:
            self.new_chapter()

    def _on_selection_changed(self, current: QListWidgetItem | None) -> None:
        if current is None:
            return
        chapter = self.services.chapters.get_chapter(
            str(current.data(Qt.ItemDataRole.UserRole))
        )
        self._load_chapter(chapter)

    def _load_chapter(self, chapter: Chapter) -> None:
        self.current_chapter_id = chapter.id
        self.sequence_no.setValue(chapter.sequence_no)
        self.chapter_title.setText(chapter.title)
        status_index = self.status_combo.findData(chapter.status)
        self.status_combo.setCurrentIndex(max(0, status_index))
        self.summary.setPlainText(chapter.summary)
        self.content.setPlainText(chapter.content)
        self.delete_button.setEnabled(True)

    def new_chapter(self) -> None:
        project_id = self.project_combo.currentData()
        self.current_chapter_id = None
        self.chapter_list.clearSelection()
        next_sequence = (
            self.services.chapters.next_sequence_no(str(project_id)) if project_id else 1
        )
        self.sequence_no.setValue(next_sequence)
        self.chapter_title.setText(f"第{next_sequence}章")
        self.status_combo.setCurrentIndex(0)
        self.summary.clear()
        self.content.clear()
        self.delete_button.setEnabled(False)

    def save_chapter(self) -> None:
        project_id = self.project_combo.currentData()
        if not project_id:
            QMessageBox.information(self, "没有作品", "请先创建作品。")
            return
        try:
            chapter = self.services.chapters.save_chapter(
                project_id=str(project_id),
                chapter_id=self.current_chapter_id,
                sequence_no=self.sequence_no.value(),
                title=self.chapter_title.text(),
                summary=self.summary.toPlainText(),
                content=self.content.toPlainText(),
                status=str(self.status_combo.currentData()),
            )
        except Exception as exc:
            QMessageBox.warning(self, "无法保存章节", str(exc))
            return
        self.current_chapter_id = chapter.id
        self.refresh_chapters(chapter.id)
        self.message.emit(f"第 {chapter.sequence_no} 章已保存。")

    def delete_chapter(self) -> None:
        if not self.current_chapter_id:
            return
        answer = QMessageBox.question(
            self,
            "删除章节",
            "确定删除当前章节吗？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.services.chapters.delete_chapter(self.current_chapter_id)
        except Exception as exc:
            QMessageBox.warning(self, "无法删除章节", str(exc))
            return
        self.current_chapter_id = None
        self.refresh_chapters()
        self.message.emit("章节已删除。")

    def export_project(self) -> None:
        project_id = self.project_combo.currentData()
        if not project_id:
            return
        project = self.services.projects.get_project(str(project_id))
        initial = self.services.paths.exports_dir / f"{safe_export_name(project.title)}.md"
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "导出整部作品",
            str(initial),
            "Markdown (*.md)",
        )
        if not filename:
            return
        try:
            target = self.services.chapters.export_project_markdown(
                str(project_id), Path(filename)
            )
        except Exception as exc:
            QMessageBox.warning(self, "无法导出", str(exc))
            return
        self.message.emit(f"已导出到：{target}")

    def _update_character_count(self) -> None:
        count = len("".join(self.content.toPlainText().split()))
        self.character_count.setText(f"{count:,} 字")

