"""Main desktop window and navigation."""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from novelforge_windows.services.container import ServiceContainer
from novelforge_windows.ui.pages.chapters_page import ChaptersPage
from novelforge_windows.ui.pages.projects_page import ProjectsPage
from novelforge_windows.ui.pages.samples_page import SamplesPage
from novelforge_windows.ui.pages.settings_page import SettingsPage
from novelforge_windows.ui.pages.workbench_page import WorkbenchPage


class MainWindow(QMainWindow):
    def __init__(self, services: ServiceContainer) -> None:
        super().__init__()
        self.services = services
        self.setWindowTitle("NovelForge Windows")
        self.resize(1280, 820)
        self.setMinimumSize(QSize(980, 680))

        central = QWidget()
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        sidebar = QWidget()
        sidebar.setFixedWidth(205)
        sidebar.setStyleSheet("background: #152a28;")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 22, 12, 12)
        brand = QLabel("NovelForge")
        brand.setStyleSheet(
            "color: white; font-size: 20px; font-weight: 700; "
            "padding: 4px 10px 16px 10px;"
        )
        edition = QLabel("Windows 本地版")
        edition.setStyleSheet("color: #9fbbb5; padding: 0 10px 10px 10px;")
        self.navigation = QListWidget()
        self.navigation.setObjectName("Navigation")
        self.navigation.setSpacing(1)
        sidebar_layout.addWidget(brand)
        sidebar_layout.addWidget(edition)
        sidebar_layout.addWidget(self.navigation, 1)

        self.stack = QStackedWidget()
        self.workbench_page = WorkbenchPage(services)
        self.projects_page = ProjectsPage(services)
        self.chapters_page = ChaptersPage(services)
        self.samples_page = SamplesPage(services)
        self.settings_page = SettingsPage(services)
        pages = [
            ("创作工作台", self.workbench_page),
            ("作品管理", self.projects_page),
            ("章节管理", self.chapters_page),
            ("本地样本库", self.samples_page),
            ("模型设置", self.settings_page),
        ]
        for label, page in pages:
            self.navigation.addItem(QListWidgetItem(label))
            self.stack.addWidget(page)
            if hasattr(page, "message"):
                page.message.connect(self.show_message)

        self.navigation.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.navigation.setCurrentRow(0)
        self.projects_page.projects_changed.connect(self.workbench_page.refresh_projects)
        self.projects_page.projects_changed.connect(self.chapters_page.refresh_projects)

        root_layout.addWidget(sidebar)
        root_layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage(f"本地数据：{services.paths.data_dir}")

    def show_message(self, message: str) -> None:
        self.statusBar().showMessage(message, 6000)
