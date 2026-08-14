"""Local model configuration page."""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from novelforge_windows.domain.models import ModelSettings
from novelforge_windows.services.container import ServiceContainer
from novelforge_windows.ui.async_task import TaskRunner


class SettingsPage(QWidget):
    message = Signal(str)

    def __init__(self, services: ServiceContainer) -> None:
        super().__init__()
        self.services = services
        self.runner = TaskRunner()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 22)
        title = QLabel("模型设置")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "Windows 版直接连接模型接口；API Key 使用当前 Windows 用户的 DPAPI 加密。"
        )
        subtitle.setObjectName("Muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        form = QFormLayout()
        form.setContentsMargins(0, 24, 0, 0)
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("https://api.openai.com/v1")
        self.model = QLineEdit()
        self.model.setPlaceholderText("模型名称")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("本地模型通常可以留空")
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0, 2)
        self.temperature.setDecimals(2)
        self.temperature.setSingleStep(0.05)
        form.addRow("Base URL", self.base_url)
        form.addRow("模型", self.model)
        form.addRow("API Key", self.api_key)
        form.addRow("Temperature", self.temperature)
        layout.addLayout(form)

        hint = QLabel(
            "Base URL 可以填写 OpenAI-compatible 的 /v1 地址或完整 /chat/completions 地址。\n"
            "应用不会把密钥写入日志、章节上下文或导出文件。"
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        self.test_button = QPushButton("测试连接")
        self.save_button = QPushButton("保存设置")
        self.save_button.setProperty("primary", True)
        self.test_button.clicked.connect(self.test_connection)
        self.save_button.clicked.connect(self.save_settings)
        buttons.addStretch(1)
        buttons.addWidget(self.test_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        layout.addStretch(1)
        self.load_settings()

    def _form_value(self) -> ModelSettings:
        return ModelSettings(
            base_url=self.base_url.text(),
            model=self.model.text(),
            api_key=self.api_key.text(),
            temperature=self.temperature.value(),
        )

    def load_settings(self) -> None:
        try:
            settings = self.services.model_settings.load()
        except Exception as exc:
            QMessageBox.warning(self, "无法读取模型设置", str(exc))
            settings = ModelSettings()
        self.base_url.setText(settings.base_url)
        self.model.setText(settings.model)
        self.api_key.setText(settings.api_key)
        self.temperature.setValue(settings.temperature)

    def save_settings(self) -> None:
        try:
            saved = self.services.model_settings.save(self._form_value())
        except Exception as exc:
            QMessageBox.warning(self, "无法保存设置", str(exc))
            return
        self.base_url.setText(saved.base_url)
        self.model.setText(saved.model)
        self.api_key.setText(saved.api_key)
        self.message.emit("模型设置已保存到本机。")

    def _set_busy(self, busy: bool) -> None:
        self.test_button.setEnabled(not busy)
        self.save_button.setEnabled(not busy)
        if busy:
            self.message.emit("正在测试模型连接……")

    def test_connection(self) -> None:
        settings = self._form_value()
        if not settings.model.strip():
            QMessageBox.information(self, "缺少模型", "请先填写模型名称。")
            return
        self._set_busy(True)
        self.runner.submit(
            partial(self.services.llm.test, settings),
            on_success=self._test_succeeded,
            on_error=self._test_failed,
            on_finished=lambda: self._set_busy(False),
        )

    def _test_succeeded(self, result: object) -> None:
        QMessageBox.information(self, "连接成功", str(result))
        self.message.emit("模型连接测试成功。")

    def _test_failed(self, message: str) -> None:
        QMessageBox.warning(self, "连接失败", message)

