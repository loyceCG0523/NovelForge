"""Personal local sample library."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from novelforge_windows.services.container import ServiceContainer


class SamplesPage(QWidget):
    message = Signal(str)

    def __init__(self, services: ServiceContainer) -> None:
        super().__init__()
        self.services = services
        self.current_sample_id: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 22)
        title = QLabel("本地样本库")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "样本复制到本机应用数据目录；公共发布、投票和举报功能不进入本地版。"
        )
        subtitle.setObjectName("Muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        toolbar = QHBoxLayout()
        import_button = QPushButton("导入 TXT/MD")
        import_button.setProperty("primary", True)
        self.delete_button = QPushButton("删除样本")
        self.delete_button.setProperty("danger", True)
        import_button.clicked.connect(self.import_samples)
        self.delete_button.clicked.connect(self.delete_sample)
        toolbar.addWidget(import_button)
        toolbar.addWidget(self.delete_button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        splitter = QSplitter()
        self.sample_list = QListWidget()
        self.sample_list.setMinimumWidth(300)
        self.sample_list.currentItemChanged.connect(self._on_selection_changed)
        preview_widget = QWidget()
        preview_layout = QVBoxLayout(preview_widget)
        preview_layout.setContentsMargins(18, 0, 0, 0)
        self.metadata = QLabel("尚未选择样本")
        self.metadata.setObjectName("Muted")
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("选择样本后显示前 20,000 字。")
        preview_layout.addWidget(self.metadata)
        preview_layout.addWidget(self.preview, 1)
        splitter.addWidget(self.sample_list)
        splitter.addWidget(preview_widget)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.refresh_samples()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.refresh_samples()

    def refresh_samples(self, preferred_id: str | None = None) -> None:
        selected_id = preferred_id or self.current_sample_id
        self.sample_list.blockSignals(True)
        self.sample_list.clear()
        selected_row = -1
        for index, sample in enumerate(self.services.samples.list_samples()):
            item = QListWidgetItem(f"{sample.title}\n{sample.character_count:,} 字")
            item.setData(Qt.ItemDataRole.UserRole, sample.id)
            self.sample_list.addItem(item)
            if sample.id == selected_id:
                selected_row = index
        self.sample_list.blockSignals(False)
        if selected_row >= 0:
            self.sample_list.setCurrentRow(selected_row)
        elif self.sample_list.count():
            self.sample_list.setCurrentRow(0)
        else:
            self.current_sample_id = None
            self.preview.clear()
            self.metadata.setText("尚未导入样本")
            self.delete_button.setEnabled(False)

    def _on_selection_changed(self, current: QListWidgetItem | None) -> None:
        if current is None:
            return
        sample_id = str(current.data(Qt.ItemDataRole.UserRole))
        sample = next(
            (item for item in self.services.samples.list_samples() if item.id == sample_id),
            None,
        )
        if sample is None:
            return
        self.current_sample_id = sample.id
        self.metadata.setText(
            f"原文件：{sample.source_name} · {sample.character_count:,} 字 · 本地副本：{sample.local_path}"
        )
        try:
            self.preview.setPlainText(self.services.samples.preview(sample.id))
        except Exception as exc:
            self.preview.setPlainText(str(exc))
        self.delete_button.setEnabled(True)

    def import_samples(self) -> None:
        filenames, _ = QFileDialog.getOpenFileNames(
            self,
            "导入小说样本",
            "",
            "文本文件 (*.txt *.md)",
        )
        if not filenames:
            return
        imported = []
        failures = []
        for filename in filenames:
            try:
                imported.append(self.services.samples.import_document(Path(filename)))
            except Exception as exc:
                failures.append(f"{Path(filename).name}：{exc}")
        if imported:
            self.current_sample_id = imported[-1].id
            self.refresh_samples(imported[-1].id)
            self.message.emit(f"已导入 {len(imported)} 个本地样本。")
        if failures:
            QMessageBox.warning(self, "部分样本导入失败", "\n".join(failures))

    def delete_sample(self) -> None:
        if not self.current_sample_id:
            return
        answer = QMessageBox.question(
            self,
            "删除样本",
            "确定删除当前样本及其本地副本吗？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.services.samples.delete_sample(self.current_sample_id)
        except Exception as exc:
            QMessageBox.warning(self, "无法删除样本", str(exc))
            return
        self.current_sample_id = None
        self.refresh_samples()
        self.message.emit("本地样本已删除。")

