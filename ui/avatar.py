"""
ui/avatar.py
Interfaz flotante de Iris con terminal independiente a la izquierda.
"""

import os
from PyQt6.QtWidgets import (QWidget, QLabel, QHBoxLayout, QVBoxLayout, QApplication, 
                             QGraphicsOpacityEffect, QLineEdit, QPushButton)
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QPropertyAnimation, QEasingCurve, QTimer
from PyQt6.QtGui import QPixmap, QFont

class IrisSignals(QObject):
    text_updated = pyqtSignal(str)
    mood_updated = pyqtSignal(str)
    user_text_submitted = pyqtSignal(str)
    terminal_output_updated = pyqtSignal(str)

class TerminalOutputUI(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
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

    def show_message(self, text):
        self.label.setText(text)
        self.setVisible(True)
        QTimer.singleShot(7000, lambda: self.setVisible(False))

class IrisAvatarUI(QWidget):
    def __init__(self, signals: IrisSignals):
        super().__init__()
        self.signals = signals
        self.avatar_path = "assets/avatars/"
        
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide_subtitles)

        self.init_ui()
        
        self.signals.text_updated.connect(self.update_subtitles)
        self.signals.mood_updated.connect(self.update_avatar)

    def init_ui(self):
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent; border: none;")

        main_layout = QVBoxLayout()
        main_layout.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        main_layout.setContentsMargins(0, 0, 0, 0)

        upper_row = QHBoxLayout()
        upper_row.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)

        self.text_label = QLabel("...")
        self.text_label.setFont(QFont("Comic Sans MS", 10, QFont.Weight.Bold))
        # Ajustes de CSS: más padding para que el texto respire
        self.text_label.setStyleSheet("""
            QLabel {
                background-color: #FFFFFF;
                color: #000000;
                border-radius: 15px;
                padding: 15px; 
                border: 2px solid #000000;
                margin-right: 15px;
                margin-bottom: 20px;
            }
        """)
        self.text_label.setWordWrap(True)
        # Límites más naturales para evitar el efecto "columna estrecha"
        self.text_label.setMinimumWidth(150)
        self.text_label.setMaximumWidth(280)
        # Alineado a la izquierda como un chat normal
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.text_label.setVisible(False)
        
        self.opacity_effect = QGraphicsOpacityEffect(self.text_label)
        self.text_label.setGraphicsEffect(self.opacity_effect)
        self.opacity_effect.setOpacity(0.0)
        
        self.fade_animation = QPropertyAnimation(self.opacity_effect, b"opacity")
        self.fade_animation.setDuration(400)
        self.fade_animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.fade_animation.finished.connect(self._on_fade_finished)

        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(143, 150)
        self.update_avatar("neutral") 

        upper_row.addWidget(self.text_label)
        upper_row.addWidget(self.avatar_label)

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

    def _toggle_terminal(self):
        self.terminal_input.setVisible(not self.terminal_input.isVisible())
        if self.terminal_input.isVisible(): self.terminal_input.setFocus()

    def _on_terminal_submit(self):
        text = self.terminal_input.text().strip()
        if text:
            self.signals.user_text_submitted.emit(text)
            self.terminal_input.clear()
        self.terminal_input.setVisible(False)

    def update_avatar(self, mood: str):
        img_path = os.path.join(self.avatar_path, f"{mood}.png")
        if not os.path.exists(img_path): img_path = os.path.join(self.avatar_path, "neutral.png")
        if os.path.exists(img_path):
            pixmap = QPixmap(img_path).scaled(self.avatar_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.avatar_label.setPixmap(pixmap)

    def update_subtitles(self, text: str):
        self.fade_animation.stop()
        if not text or text == "...":
            self.hide_subtitles()
        else:
            self.opacity_effect.setOpacity(1.0)
            self.text_label.setText(text)
            self.text_label.setVisible(True)
            self.hide_timer.start(6000)

    def hide_subtitles(self):
        if self.opacity_effect.opacity() > 0:
            self.fade_animation.setStartValue(self.opacity_effect.opacity())
            self.fade_animation.setEndValue(0.0)
            self.fade_animation.start()

    def _on_fade_finished(self):
        if self.opacity_effect.opacity() == 0.0: self.text_label.setVisible(False)