"""Render the source SVG to PNG and Windows ICO assets."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    svg_path = project_root / "assets" / "novelforge.svg"
    png_path = project_root / "assets" / "novelforge.png"
    ico_path = project_root / "assets" / "novelforge.ico"
    application = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        raise RuntimeError(f"无法读取图标源文件：{svg_path}")
    image = QImage(512, 512, QImage.Format.Format_ARGB32)
    image.fill(QColor(Qt.GlobalColor.transparent))
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    if not image.save(str(png_path), "PNG"):
        raise RuntimeError(f"无法生成 PNG：{png_path}")
    if not image.save(str(ico_path), "ICO"):
        raise RuntimeError(f"无法生成 ICO：{ico_path}")
    print(ico_path)
    _ = application


if __name__ == "__main__":
    main()

