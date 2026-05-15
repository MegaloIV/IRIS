"""
ui/avatar.py
Ventana principal del avatar flotante de Iris.
"""

import os
import logging

from PyQt6.QtWidgets import (QWidget, QLabel, QHBoxLayout, QVBoxLayout,
                             QApplication, QLineEdit, QPushButton)
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui import QPixmap

from .signals import IrisSignals
from .bubble import BubbleRenderer
from .settings_panel import SettingsPanel

logger = logging.getLogger(__name__)


class IrisAvatarUI(QWidget):
    def __init__(self, signals: IrisSignals):
        super().__init__()
        self.signals     = signals
        self.avatar_path = "assets/avatars/"

        self.settings_panel = SettingsPanel()
        self._init_ui()

        self.signals.text_updated.connect(self.update_subtitles)
        self.signals.mood_updated.connect(self.update_avatar)

    def _init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent; border: none;")

        main_layout = QVBoxLayout()
        main_layout.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        main_layout.setContentsMargins(0, 0, 0, 0)

        upper_row = QHBoxLayout()
        upper_row.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        upper_row.setContentsMargins(0, 0, 0, 0)
        upper_row.setSpacing(0)

        # Bubble: fixed 255 px, never shifts the avatar
        self.bubble_renderer = BubbleRenderer()

        # Avatar: fixed 143 px, always anchored to the right
        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(143, 150)
        self.update_avatar("neutral")

        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setFixedSize(28, 22)
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.setToolTip("Configuración")
        self.settings_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(40, 40, 40, 200);
                color: #AAAAAA;
                border: 1px solid #555;
                border-radius: 6px;
                font-size: 13px;
                padding: 0px;
            }
            QPushButton:hover   { background-color: rgba(65, 65, 65, 220); color: #EEE; }
            QPushButton:pressed { background-color: rgba(80, 80, 80, 220); }
        """)
        self.settings_btn.clicked.connect(self._toggle_settings)

        gear_row = QHBoxLayout()
        gear_row.setContentsMargins(0, 0, 0, 2)
        gear_row.addStretch()
        gear_row.addWidget(self.settings_btn)

        avatar_container = QWidget()
        avatar_container.setFixedWidth(143)
        avatar_container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        avatar_inner = QVBoxLayout(avatar_container)
        avatar_inner.setContentsMargins(0, 0, 0, 0)
        avatar_inner.setSpacing(0)
        avatar_inner.addLayout(gear_row)
        avatar_inner.addWidget(self.avatar_label)

        self.settings_panel.btn_voice.clicked.connect(lambda: self._on_mode_set(True))
        self.settings_panel.btn_text_mode.clicked.connect(lambda: self._on_mode_set(False))

        upper_row.addWidget(self.bubble_renderer)
        upper_row.addWidget(avatar_container)

        terminal_layout = QHBoxLayout()
        terminal_layout.setAlignment(Qt.AlignmentFlag.AlignRight)

        self.terminal_input = QLineEdit()
        self.terminal_input.setFixedWidth(180)
        self.terminal_input.setPlaceholderText("Comando o texto...")
        self.terminal_input.setStyleSheet("""
            QLineEdit {
                background-color: rgba(30, 30, 30, 220);
                border: 2px solid #555;
                border-radius: 8px;
                padding: 4px;
                color: #FFF;
                font-family: 'Consolas', 'Monospace';
                font-weight: bold;
            }
        """)
        self.terminal_input.setVisible(False)
        self.terminal_input.returnPressed.connect(self._on_terminal_submit)

        self.terminal_btn = QPushButton(">_")
        self.terminal_btn.setFixedSize(30, 30)
        self.terminal_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.terminal_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(50, 50, 50, 220);
                color: white;
                border: 2px solid #555;
                border-radius: 15px;
                font-family: 'Consolas';
                font-weight: bold;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #444; border-color: #888; }
        """)
        self.terminal_btn.clicked.connect(self._toggle_terminal)

        terminal_layout.addWidget(self.terminal_input)
        terminal_layout.addWidget(self.terminal_btn)

        main_layout.addLayout(upper_row)
        main_layout.addLayout(terminal_layout)
        self.setLayout(main_layout)

        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen.width() - 440, screen.height() - 430, 420, 400)

    # ── Settings ──────────────────────────────────────────────────────────────

    def _toggle_settings(self):
        if self.settings_panel.isVisible():
            self.settings_panel.hide()
            return
        self.settings_panel.adjustSize()
        btn_global = self.settings_btn.mapToGlobal(QPoint(0, 0))
        panel_w    = self.settings_panel.width()
        panel_h    = self.settings_panel.height()
        x = btn_global.x() + self.settings_btn.width() - panel_w
        y = btn_global.y() - panel_h - 4
        if y < 0:
            y = btn_global.y() + self.settings_btn.height() + 4
        self.settings_panel.move(x, y)
        self.settings_panel.show()
        self.settings_panel.raise_()

    def _on_mode_set(self, voice: bool):
        self.settings_panel.set_mode(voice)
        self.signals.voice_mode_changed.emit(voice)
        self.settings_panel.hide()

    # ── Terminal ──────────────────────────────────────────────────────────────

    def _toggle_terminal(self):
        self.terminal_input.setVisible(not self.terminal_input.isVisible())
        if self.terminal_input.isVisible():
            self.terminal_input.setFocus()

    def _on_terminal_submit(self):
        text = self.terminal_input.text().strip()
        if text:
            self.signals.user_text_submitted.emit(text)
            self.terminal_input.clear()
        self.terminal_input.setVisible(False)

    # ── Avatar & subtitles ────────────────────────────────────────────────────

    def update_avatar(self, mood: str):
        img_path = os.path.join(self.avatar_path, f"{mood}.png")
        if not os.path.exists(img_path):
            img_path = os.path.join(self.avatar_path, "neutral.png")
        if os.path.exists(img_path):
            pixmap = QPixmap(img_path).scaled(
                self.avatar_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.avatar_label.setPixmap(pixmap)

    def update_subtitles(self, text: str):
        logger.debug("update_subtitles: type=%s len=%d repr=%r",
                     type(text).__name__, len(text) if text else 0,
                     (text or "")[:80])
        self.bubble_renderer.show_text(text)
