"""
ui/avatar.py
Interfaz flotante de Iris con terminal independiente a la izquierda.
"""

import os
import re
import random
import time as _time

def _ts():
    return _time.strftime("%H:%M:%S") + f".{int((_time.time()%1)*1000):03d}"

from PyQt6.QtWidgets import (QWidget, QLabel, QHBoxLayout, QVBoxLayout, QApplication,
                             QGraphicsOpacityEffect, QLineEdit, QPushButton)
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QPropertyAnimation, QEasingCurve, QTimer, QPoint
from PyQt6.QtGui import QPixmap, QFont


class IrisSignals(QObject):
    text_updated           = pyqtSignal(str)
    mood_updated           = pyqtSignal(str)
    user_text_submitted    = pyqtSignal(str)
    terminal_output_updated = pyqtSignal(str)
    voice_mode_changed     = pyqtSignal(bool)  # True = TTS habilitado


# ── Shared style constants ─────────────────────────────────────────────────────

_BUBBLE_STYLE = """
    QLabel {
        background-color: #FFFFFF;
        color: #000000;
        border-radius: 12px;
        padding: 10px 12px;
        border: 2px solid #000000;
        margin-right: 12px;
    }
"""

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


# ── Bubble renderer ────────────────────────────────────────────────────────────

class BubbleRenderer(QWidget):
    """
    Per-sentence cycle in the SAME bubble:
        clear → type letter by letter → hold → fade out → clear → next.
    Sentences NEVER accumulate; each replaces the previous one.
    """

    _HOLD_AFTER_TYPING_MS = 350
    _FADE_DURATION_MS     = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(255)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        self._bubble:        QLabel | None = None
        self._pending:       list[str]     = []
        self._current_text:  str           = ""
        self._typed_chars:   int           = 0
        self._is_animating:  bool          = False  # True from start of typing through end of fade
        self._in_response:   bool          = False  # True between first sentence and sentinel
        self._fade_on_done:  bool          = False  # finalize once the queue drains
        self._anim_start_ts: float         = 0.0

        self._type_timer = QTimer(self)
        self._type_timer.timeout.connect(self._type_next_char)

        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(1.0)

        self._fade_anim = QPropertyAnimation(self._opacity, b"opacity")
        self._fade_anim.setDuration(self._FADE_DURATION_MS)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.finished.connect(self._on_sentence_faded)

        self.setVisible(False)

    # ── Public ─────────────────────────────────────────────────────────────────

    def show_text(self, text: str):
        """
        Queue *text* for animated display. Each sentence runs its own
        clear→type→fade cycle in the same bubble. Empty / sentinel string =
        end-of-response → bubble fades out once the queue drains.
        """
        print(f"[{_ts()}][Bubble.show_text] ENTRADA estado: in_response={self._in_response} "
              f"is_animating={self._is_animating} pending_count={len(self._pending)} "
              f"fade_on_done={self._fade_on_done} visible={self.isVisible()}")

        if not text or text == "...":
            print(f"[{_ts()}][Bubble.show_text] sentinel/empty → marca fade_on_done=True y retorna")
            self._in_response  = False
            self._fade_on_done = True
            return

        print(f"[{_ts()}][Bubble] show_text recibió: {repr(text[:200])}")
        sentences = [s for s in re.split(r'(?<=[.!?])\s+', text.strip()) if s.strip()]
        print(f"[{_ts()}][Bubble] split resultado ({len(sentences)} sentencias): {sentences}")

        if not sentences:
            print(f"[{_ts()}][Bubble.show_text] split vacío → return")
            return

        if not self._in_response:
            # Fresh response — wipe any leftover state and build a new bubble
            self._stop_all()
            self._in_response  = True
            self._fade_on_done = False
            self._create_bubble()

        for i, s in enumerate(sentences):
            print(f"[{_ts()}][Bubble.show_text]   ENQUEUE[{i}] → {repr(s)}")
        self._pending.extend(sentences)
        print(f"[{_ts()}][Bubble.show_text] cola tras enqueue: total_pending={len(self._pending)} "
              f"is_animating={self._is_animating}")

        if not self._is_animating:
            print(f"[{_ts()}][Bubble.show_text] no animando → _start_next_sentence")
            self._start_next_sentence()
        else:
            print(f"[{_ts()}][Bubble.show_text] ya animando → se encolan para drenar")

    # ── Internals ──────────────────────────────────────────────────────────────

    def _create_bubble(self):
        self.setVisible(True)
        self._opacity.setOpacity(1.0)
        self._bubble = QLabel("")
        self._bubble.setFont(QFont("Comic Sans MS", 10, QFont.Weight.Bold))
        self._bubble.setWordWrap(True)
        self._bubble.setMaximumWidth(240)
        self._bubble.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._bubble.setStyleSheet(_BUBBLE_STYLE)
        self._layout.addWidget(self._bubble)

    def _start_next_sentence(self):
        print(f"[{_ts()}][Bubble._start_next_sentence] pending_count={len(self._pending)} "
              f"fade_on_done={self._fade_on_done}")
        if not self._pending:
            self._is_animating = False
            if self._fade_on_done:
                print(f"[{_ts()}][Bubble._start_next_sentence] cola vacía + fade_on_done → finalize")
                self._stop_all()
            else:
                print(f"[{_ts()}][Bubble._start_next_sentence] cola vacía → idle, espero más sentencias")
            return

        self._is_animating  = True
        self._current_text  = self._pending.pop(0)
        self._typed_chars   = 0
        self._anim_start_ts = _time.time()

        # Reset bubble for the new sentence: no carry-over, full opacity
        self._fade_anim.stop()
        if self._bubble is None:
            self._create_bubble()
        self._bubble.setText("")
        self._opacity.setOpacity(1.0)

        print(f"[{_ts()}][Bubble] iniciando animación ({len(self._current_text)} chars): {repr(self._current_text)}")
        self._type_timer.start(random.randint(28, 45))

    def _type_next_char(self):
        if self._bubble is None:
            self._type_timer.stop()
            return
        if self._typed_chars < len(self._current_text):
            self._typed_chars += 1
            self._bubble.setText(self._current_text[: self._typed_chars])
            self._type_timer.setInterval(random.randint(22, 52))
            if self._typed_chars == 1 or self._typed_chars % 10 == 0:
                print(f"[{_ts()}][Bubble._type_next_char] TICK char {self._typed_chars}/{len(self._current_text)}")
        else:
            elapsed_ms = int((_time.time() - self._anim_start_ts) * 1000)
            print(f"[{_ts()}][Bubble._type_next_char] FIN tipeo ({len(self._current_text)} chars en {elapsed_ms} ms) → hold {self._HOLD_AFTER_TYPING_MS}ms")
            self._type_timer.stop()
            QTimer.singleShot(self._HOLD_AFTER_TYPING_MS, self._begin_sentence_fade)

    def _begin_sentence_fade(self):
        if self._bubble is None:
            return
        print(f"[{_ts()}][Bubble._begin_sentence_fade] iniciando fade (dur={self._FADE_DURATION_MS}ms)")
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._opacity.opacity())
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()

    def _on_sentence_faded(self):
        print(f"[{_ts()}][Bubble._on_sentence_faded] fade completo pending={len(self._pending)} "
              f"fade_on_done={self._fade_on_done}")
        # Wipe content; restore opacity so the next sentence is visible
        if self._bubble is not None:
            self._bubble.setText("")
        self._opacity.setOpacity(1.0)

        if self._pending:
            self._start_next_sentence()
        elif self._fade_on_done:
            print(f"[{_ts()}][Bubble._on_sentence_faded] sin más pendientes + fade_on_done → finalize")
            self._stop_all()
        else:
            # Idle: keep the bubble around invisibly until the next sentence
            self._is_animating = False
            print(f"[{_ts()}][Bubble._on_sentence_faded] sin más pendientes (idle, esperando)")

    def _stop_all(self):
        """Full reset: stop timers, drop queue, destroy bubble, hide widget."""
        self._type_timer.stop()
        self._fade_anim.stop()
        self._pending.clear()
        self._current_text = ""
        self._typed_chars  = 0
        self._is_animating = False
        self._in_response  = False
        self._fade_on_done = False
        if self._bubble is not None:
            self._layout.removeWidget(self._bubble)
            self._bubble.deleteLater()
            self._bubble = None
        self._opacity.setOpacity(1.0)
        self.setVisible(False)


# ── Settings panel ─────────────────────────────────────────────────────────────

class SettingsPanel(QWidget):
    """Floating config panel — opened from the ⚙ button."""

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


# ── Terminal output overlay ────────────────────────────────────────────────────

class TerminalOutputUI(QWidget):
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

    def show_message(self, text):
        self.label.setText(text)
        self.setVisible(True)
        QTimer.singleShot(7000, lambda: self.setVisible(False))


# ── Main avatar window ─────────────────────────────────────────────────────────

class IrisAvatarUI(QWidget):
    def __init__(self, signals: IrisSignals):
        super().__init__()
        self.signals     = signals
        self.avatar_path = "assets/avatars/"

        self.settings_panel = SettingsPanel()

        self.init_ui()

        self.signals.text_updated.connect(self.update_subtitles)
        self.signals.mood_updated.connect(self.update_avatar)

    def init_ui(self):
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

        # ── Bubble renderer — fixed 255 px, never shifts the avatar ───────────
        self.bubble_renderer = BubbleRenderer()

        # ── Avatar section — fixed 143 px, always anchored to the right ──────
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

        # Wire up settings panel buttons
        self.settings_panel.btn_voice.clicked.connect(lambda: self._on_mode_set(True))
        self.settings_panel.btn_text_mode.clicked.connect(lambda: self._on_mode_set(False))

        upper_row.addWidget(self.bubble_renderer)
        upper_row.addWidget(avatar_container)

        # ── Terminal bar ──────────────────────────────────────────────────────
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
        print(f"[Avatar.update_subtitles] recibido vía signal: type={type(text).__name__} len={len(text) if text else 0}")
        print(f"[Avatar.update_subtitles] repr (primeros 200): {repr(text[:200]) if text else repr(text)}")
        self.bubble_renderer.show_text(text)
