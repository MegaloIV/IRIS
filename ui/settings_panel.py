from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QPushButton
from PyQt6.QtCore import Qt

_BTN_OPTION = """
    QPushButton {
        background-color: rgba(52, 52, 58, 210);
        color: #CCCCCC;
        border: 1px solid #505055;
        border-radius: 6px;
        padding: 5px 10px;
        font-size: 12px;
        text-align: left;
    }
    QPushButton:checked {
        background-color: rgba(55, 95, 170, 215);
        border-color: #4070BB;
        color: #FFFFFF;
    }
    QPushButton:hover:!checked { background-color: rgba(68, 68, 75, 210); }
"""


class SettingsPanel(QWidget):
    """Floating config panel opened from the gear button."""

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._build()
        self.hide()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        panel = QWidget()
        panel.setObjectName("panel")
        panel.setStyleSheet("""
            QWidget#panel {
                background-color: rgba(18, 18, 22, 238);
                border: 1px solid #3a3a3a;
                border-radius: 10px;
            }
        """)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(4)

        title = QLabel("⚙  Configuración")
        title.setStyleSheet(
            "color: #BBBBBB; font-size: 11px; font-weight: bold; padding-bottom: 2px;"
        )
        layout.addWidget(title)

        divider = QWidget()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background-color: #3a3a3a; margin: 2px 0;")
        layout.addWidget(divider)

        section_lbl = QLabel("MODO DE RESPUESTA")
        section_lbl.setStyleSheet(
            "color: #666; font-size: 9px; letter-spacing: 1px;"
            " margin-top: 6px; margin-bottom: 3px;"
        )
        layout.addWidget(section_lbl)

        self.btn_voice = QPushButton("🔊  Voz")
        self.btn_voice.setCheckable(True)
        self.btn_voice.setChecked(True)
        self.btn_voice.setStyleSheet(_BTN_OPTION)
        self.btn_voice.setCursor(Qt.CursorShape.PointingHandCursor)

        self.btn_text_mode = QPushButton("💬  Solo texto")
        self.btn_text_mode.setCheckable(True)
        self.btn_text_mode.setStyleSheet(_BTN_OPTION)
        self.btn_text_mode.setCursor(Qt.CursorShape.PointingHandCursor)

        layout.addWidget(self.btn_voice)
        layout.addWidget(self.btn_text_mode)

        outer.addWidget(panel)
        self.setFixedWidth(190)
        self.adjustSize()

    def set_mode(self, voice: bool):
        self.btn_voice.setChecked(voice)
        self.btn_text_mode.setChecked(not voice)
