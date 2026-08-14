"""Local generation workbench."""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from novelforge_windows.domain.models import Chapter
from novelforge_windows.services.container import ServiceContainer
from novelforge_windows.ui.async_task import TaskRunner


class WorkbenchPage(QWidget):
    message = Signal(str)

    def __init__(self, services: ServiceContainer) -> None:
        super().__init__()
        self.services = services
        self.runner = TaskRunner()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 22)
        title = QLabel("创作工作台")
        title.setObjectName("PageTitle")
        subtitle = QLabel("模型调用从本机直接发起，生成结果立即写入本地数据库。")
        subtitle.setObjectName("Muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        toolbar = QHBoxLayout()
        self.project_combo = QComboBox()
        self.project_combo.setMinimumWidth(300)
        self.project_combo.currentIndexChanged.connect(self.refresh_current)
        self.bible_button = QPushButton("生成/刷新作品圣经")
        self.chapter_button = QPushButton("生成下一章")
        self.chapter_button.setProperty("primary", True)
        self.bible_button.clicked.connect(self.generate_story_bible)
        self.chapter_button.clicked.connect(self.generate_next_chapter)
        toolbar.addWidget(QLabel("当前作品"))
        toolbar.addWidget(self.project_combo)
        toolbar.addStretch(1)
        toolbar.addWidget(self.bible_button)
        toolbar.addWidget(self.chapter_button)
        layout.addLayout(toolbar)

        self.progress_label = QLabel("尚未选择作品")
        self.progress_label.setObjectName("Muted")
        layout.addWidget(self.progress_label)

        bible_group = QGroupBox("作品圣经")
        bible_layout = QVBoxLayout(bible_group)
        self.story_bible = QTextEdit()
        self.story_bible.setReadOnly(True)
        self.story_bible.setPlaceholderText("生成作品圣经后会显示在这里。")
        bible_layout.addWidget(self.story_bible)
        layout.addWidget(bible_group, 1)

        chapter_group = QGroupBox("最新章节")
        chapter_layout = QVBoxLayout(chapter_group)
        self.latest_chapter_title = QLabel("暂无章节")
        self.latest_chapter = QTextEdit()
        self.latest_chapter.setReadOnly(True)
        self.latest_chapter.setMinimumHeight(180)
        chapter_layout.addWidget(self.latest_chapter_title)
        chapter_layout.addWidget(self.latest_chapter)
        layout.addWidget(chapter_group, 1)
        self.refresh_projects()

    def refresh_projects(self) -> None:
        selected_id = self.project_combo.currentData()
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        target_index = -1
        for index, project in enumerate(self.services.projects.list_projects()):
            self.project_combo.addItem(project.title, project.id)
            if project.id == selected_id:
                target_index = index
        self.project_combo.blockSignals(False)
        if target_index >= 0:
            self.project_combo.setCurrentIndex(target_index)
        elif self.project_combo.count():
            self.project_combo.setCurrentIndex(0)
        self.refresh_current()

    def refresh_current(self) -> None:
        project_id = self.project_combo.currentData()
        has_project = bool(project_id)
        self.bible_button.setEnabled(has_project)
        self.chapter_button.setEnabled(has_project)
        if not project_id:
            self.story_bible.clear()
            self.latest_chapter.clear()
            self.latest_chapter_title.setText("暂无章节")
            self.progress_label.setText("请先在“作品管理”中创建作品。")
            return
        project = self.services.projects.get_project(str(project_id))
        chapters = self.services.chapters.list_chapters(project.id)
        total_characters = sum(item.character_count for item in chapters)
        self.progress_label.setText(
            f"{project.genre or '未分类'} · {len(chapters)} 章 · "
            f"约 {total_characters:,}/{project.target_words:,} 字"
        )
        self.story_bible.setPlainText(project.story_bible)
        if chapters:
            latest = chapters[-1]
            self.latest_chapter_title.setText(
                f"第{latest.sequence_no}章 {latest.title} · {latest.character_count:,} 字"
            )
            self.latest_chapter.setPlainText(latest.content)
        else:
            self.latest_chapter_title.setText("暂无章节")
            self.latest_chapter.clear()

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.project_combo.setEnabled(not busy)
        self.bible_button.setEnabled(not busy and bool(self.project_combo.currentData()))
        self.chapter_button.setEnabled(not busy and bool(self.project_combo.currentData()))
        if message:
            self.message.emit(message)

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self, "生成失败", message)

    def generate_story_bible(self) -> None:
        project_id = self.project_combo.currentData()
        if not project_id:
            return
        self._set_busy(True, "正在生成作品圣经……")
        self.runner.submit(
            partial(self.services.generation.generate_story_bible, str(project_id)),
            on_success=self._story_bible_generated,
            on_error=self._show_error,
            on_finished=lambda: self._set_busy(False),
        )

    def _story_bible_generated(self, content: object) -> None:
        self.story_bible.setPlainText(str(content))
        self.refresh_current()
        self.message.emit("作品圣经已保存到本地。")

    def generate_next_chapter(self) -> None:
        project_id = self.project_combo.currentData()
        if not project_id:
            return
        chapter_no = self.services.chapters.next_sequence_no(str(project_id))
        requested_title, accepted = QInputDialog.getText(
            self,
            "生成下一章",
            f"第 {chapter_no} 章标题（可以留空）",
        )
        if not accepted:
            return
        self._set_busy(True, f"正在生成第 {chapter_no} 章……")
        self.runner.submit(
            partial(
                self.services.generation.generate_next_chapter,
                str(project_id),
                requested_title,
            ),
            on_success=self._chapter_generated,
            on_error=self._show_error,
            on_finished=lambda: self._set_busy(False),
        )

    def _chapter_generated(self, result: object) -> None:
        chapter = result
        if not isinstance(chapter, Chapter):
            self._show_error("生成结果格式错误。")
            return
        self.refresh_current()
        self.message.emit(f"第 {chapter.sequence_no} 章已生成并保存。")

