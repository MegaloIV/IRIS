from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QApplication
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont


class TerminalOutputUI(QWidget):
    """Overlay that shows short status messages in the bottom-left corner."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        layout = QVBoxLayout()
        self.label = QLabel("")
        self.label.setFont(QFont("Consolas", 10))
        self.label.setWordWrap(True)
        self.label.setFixedWidth(300)
        self.label.setStyleSheet("""
            QLabel {
                background-color: rgba(0, 0, 0, 230);
                color: #00FF00;
                border: 2px solid #333;
                border-radius: 10px;
                padding: 15px;
            }
        """)
        layout.addWidget(self.label)
        self.setLayout(layout)
        self.setVisible(False)

        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(50, screen.height() - 400, 320, 300)

    def show_message(self, text: str):
        self.label.setText(text)
        self.setVisible(True)
        QTimer.singleShot(7000, lambda: self.setVisible(False))
