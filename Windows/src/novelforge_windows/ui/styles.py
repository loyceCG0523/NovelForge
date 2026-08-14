"""Application-wide Qt stylesheet."""

APP_STYLESHEET = """
QMainWindow, QWidget {
    background: #f5f7f9;
    color: #1f2933;
    font-family: "Microsoft YaHei UI";
    font-size: 13px;
}
QListWidget#Navigation {
    background: #152a28;
    color: #dce8e5;
    border: none;
    padding: 12px 8px;
    outline: none;
}
QListWidget#Navigation::item {
    border-radius: 6px;
    margin: 3px 0;
    padding: 11px 14px;
}
QListWidget#Navigation::item:selected {
    background: #2d6a61;
    color: white;
}
QFrame#Card, QGroupBox {
    background: white;
    border: 1px solid #dde4e8;
    border-radius: 8px;
}
QGroupBox {
    margin-top: 12px;
    padding: 14px 12px 12px 12px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 5px;
}
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: white;
    border: 1px solid #cfd9df;
    border-radius: 5px;
    padding: 6px 8px;
    selection-background-color: #3a7d72;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QComboBox:focus {
    border-color: #3a7d72;
}
QPushButton {
    background: #e8efed;
    border: 1px solid #c6d5d1;
    border-radius: 5px;
    padding: 7px 14px;
}
QPushButton:hover { background: #dbe9e5; }
QPushButton:pressed { background: #cddfda; }
QPushButton:disabled { color: #98a3aa; background: #eef1f2; }
QPushButton[primary="true"] {
    background: #2d6a61;
    color: white;
    border-color: #2d6a61;
}
QPushButton[primary="true"]:hover { background: #245a52; }
QPushButton[danger="true"] {
    color: #a33a3a;
    background: #fff4f4;
    border-color: #e6bcbc;
}
QListWidget:not(#Navigation) {
    background: white;
    border: 1px solid #d7e0e4;
    border-radius: 6px;
    outline: none;
}
QListWidget:not(#Navigation)::item { padding: 8px; }
QListWidget:not(#Navigation)::item:selected {
    background: #dcece8;
    color: #183d37;
}
QLabel#PageTitle { font-size: 23px; font-weight: 700; color: #183d37; }
QLabel#Muted { color: #6b7780; }
QStatusBar { background: white; border-top: 1px solid #dde4e8; }
"""

