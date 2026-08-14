"""Local project management page."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from novelforge_windows.domain.models import Project
from novelforge_windows.services.container import ServiceContainer


class ProjectsPage(QWidget):
    projects_changed = Signal()
    message = Signal(str)

    def __init__(self, services: ServiceContainer) -> None:
        super().__init__()
        self.services = services
        self.current_project_id: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 22)
        title = QLabel("作品管理")
        title.setObjectName("PageTitle")
        subtitle = QLabel("作品与章节只保存在当前电脑，不需要账号。")
        subtitle.setObjectName("Muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        splitter = QSplitter()
        self.project_list = QListWidget()
        self.project_list.setMinimumWidth(260)
        self.project_list.currentItemChanged.connect(self._on_selection_changed)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(18, 0, 0, 0)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.title_edit = QLineEdit()
        self.author_edit = QLineEdit()
        self.genre_edit = QLineEdit()
        self.brief_edit = QTextEdit()
        self.brief_edit.setMinimumHeight(180)
        self.target_words = QSpinBox()
        self.target_words.setRange(1_000, 10_000_000)
        self.target_words.setSingleStep(10_000)
        self.chapter_min = QSpinBox()
        self.chapter_min.setRange(300, 50_000)
        self.chapter_min.setSingleStep(100)
        self.chapter_max = QSpinBox()
        self.chapter_max.setRange(300, 50_000)
        self.chapter_max.setSingleStep(100)
        form.addRow("作品名", self.title_edit)
        form.addRow("作者", self.author_edit)
        form.addRow("类型", self.genre_edit)
        form.addRow("起始需求", self.brief_edit)
        form.addRow("目标总字数", self.target_words)
        form.addRow("单章最少字数", self.chapter_min)
        form.addRow("单章最多字数", self.chapter_max)
        editor_layout.addLayout(form)

        buttons = QHBoxLayout()
        self.new_button = QPushButton("新建")
        self.save_button = QPushButton("保存")
        self.save_button.setProperty("primary", True)
        self.delete_button = QPushButton("删除")
        self.delete_button.setProperty("danger", True)
        self.new_button.clicked.connect(self.new_project)
        self.save_button.clicked.connect(self.save_project)
        self.delete_button.clicked.connect(self.delete_project)
        buttons.addWidget(self.new_button)
        buttons.addStretch(1)
        buttons.addWidget(self.delete_button)
        buttons.addWidget(self.save_button)
        editor_layout.addLayout(buttons)
        editor_layout.addStretch(1)

        splitter.addWidget(self.project_list)
        splitter.addWidget(editor)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.refresh_projects()

    def refresh_projects(self, preferred_id: str | None = None) -> None:
        selected_id = preferred_id or self.current_project_id
        self.project_list.blockSignals(True)
        self.project_list.clear()
        selected_row = -1
        for index, project in enumerate(self.services.projects.list_projects()):
            item = QListWidgetItem(project.title)
            item.setData(Qt.ItemDataRole.UserRole, project.id)
            item.setToolTip(project.genre or "未设置类型")
            self.project_list.addItem(item)
            if project.id == selected_id:
                selected_row = index
        self.project_list.blockSignals(False)
        if selected_row >= 0:
            self.project_list.setCurrentRow(selected_row)
        elif self.project_list.count():
            self.project_list.setCurrentRow(0)
        else:
            self.new_project()

    def _on_selection_changed(self, current: QListWidgetItem | None) -> None:
        if current is None:
            return
        project_id = str(current.data(Qt.ItemDataRole.UserRole))
        self._load_project(self.services.projects.get_project(project_id))

    def _load_project(self, project: Project) -> None:
        self.current_project_id = project.id
        self.title_edit.setText(project.title)
        self.author_edit.setText(project.author)
        self.genre_edit.setText(project.genre)
        self.brief_edit.setPlainText(project.brief)
        self.target_words.setValue(project.target_words)
        self.chapter_min.setValue(project.chapter_min_words)
        self.chapter_max.setValue(project.chapter_max_words)
        self.delete_button.setEnabled(True)

    def new_project(self) -> None:
        self.current_project_id = None
        self.project_list.clearSelection()
        self.title_edit.clear()
        self.author_edit.clear()
        self.genre_edit.clear()
        self.brief_edit.clear()
        self.target_words.setValue(200_000)
        self.chapter_min.setValue(2_000)
        self.chapter_max.setValue(3_500)
        self.delete_button.setEnabled(False)
        self.title_edit.setFocus()

    def save_project(self) -> None:
        values = {
            "title": self.title_edit.text(),
            "author": self.author_edit.text(),
            "genre": self.genre_edit.text(),
            "brief": self.brief_edit.toPlainText(),
            "target_words": self.target_words.value(),
            "chapter_min_words": self.chapter_min.value(),
            "chapter_max_words": self.chapter_max.value(),
        }
        try:
            if self.current_project_id:
                project = self.services.projects.update_project(
                    self.current_project_id, **values
                )
            else:
                project = self.services.projects.create_project(**values)
        except Exception as exc:
            QMessageBox.warning(self, "无法保存作品", str(exc))
            return
        self.current_project_id = project.id
        self.refresh_projects(project.id)
        self.projects_changed.emit()
        self.message.emit(f"已保存作品：{project.title}")

    def delete_project(self) -> None:
        if not self.current_project_id:
            return
        answer = QMessageBox.question(
            self,
            "删除作品",
            "确定删除当前作品及其全部章节吗？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.services.projects.delete_project(self.current_project_id)
        except Exception as exc:
            QMessageBox.warning(self, "无法删除作品", str(exc))
            return
        self.current_project_id = None
        self.refresh_projects()
        self.projects_changed.emit()
        self.message.emit("作品已删除。")

