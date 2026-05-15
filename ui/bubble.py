import re
import random
import logging

from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QGraphicsOpacityEffect
from PyQt6.QtCore import Qt, QPropertyAnimation, QEasingCurve, QTimer
from PyQt6.QtGui import QFont

logger = logging.getLogger(__name__)

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


class BubbleRenderer(QWidget):
    """
    Per-sentence cycle in the SAME bubble:
        clear → type letter by letter → hold → fade out → clear → next.
    Sentences never accumulate; each replaces the previous one.
    """

    _HOLD_AFTER_TYPING_MS    = 450
    _FADE_DURATION_MS        = 700
    _INTER_SENTENCE_DELAY_MS = 700

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(255)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        self._bubble:       QLabel | None = None
        self._pending:      list[str]     = []
        self._current_text: str           = ""
        self._typed_chars:  int           = 0
        self._is_animating: bool          = False
        self._in_response:  bool          = False
        self._fade_on_done: bool          = False

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

    def show_text(self, text: str):
        """Queue text for animated display. Empty string / '...' = end-of-response sentinel."""
        logger.debug("show_text: %r  in_response=%s is_animating=%s pending=%d",
                     (text or "")[:80], self._in_response, self._is_animating, len(self._pending))

        if not text or text == "...":
            self._in_response  = False
            self._fade_on_done = True
            # Sentinel arrived while idle (voice flow): nothing will trigger _stop_all,
            # so finalize immediately instead of leaving a blank bubble on screen.
            if not self._is_animating:
                self._stop_all()
            return

        sentences = [s for s in re.split(r'(?<=[.!?])\s+', text.strip()) if s.strip()]
        if not sentences:
            return

        if not self._in_response:
            self._stop_all()
            self._in_response  = True
            self._fade_on_done = False
            self._create_bubble()

        self._pending.extend(sentences)
        logger.debug("queued %d sentence(s), total pending=%d", len(sentences), len(self._pending))

        if not self._is_animating:
            self._start_next_sentence()

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
        if not self._pending:
            self._is_animating = False
            if self._fade_on_done:
                self._stop_all()
            return

        self._is_animating  = True
        self._current_text  = self._pending.pop(0)
        self._typed_chars   = 0

        self._fade_anim.stop()
        if self._bubble is None:
            self._create_bubble()
        self._bubble.setText("")
        self._opacity.setOpacity(1.0)
        logger.debug("typing sentence (%d chars): %r", len(self._current_text), self._current_text[:60])
        self._type_timer.start(random.randint(28, 45))

    def _type_next_char(self):
        if self._bubble is None:
            self._type_timer.stop()
            return
        if self._typed_chars < len(self._current_text):
            self._typed_chars += 1
            self._bubble.setText(self._current_text[: self._typed_chars])
            self._type_timer.setInterval(random.randint(22, 52))
        else:
            self._type_timer.stop()
            QTimer.singleShot(self._HOLD_AFTER_TYPING_MS, self._begin_sentence_fade)

    def _begin_sentence_fade(self):
        if self._bubble is None:
            return
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._opacity.opacity())
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()

    def _on_sentence_faded(self):
        if self._bubble is not None:
            self._bubble.setText("")
        # Do NOT reset opacity here — keep it at 0 so no blank box is visible
        # while waiting for the next sentence or the end-of-response sentinel.
        # Opacity is restored in _start_next_sentence / _create_bubble / _stop_all.

        if self._pending:
            QTimer.singleShot(self._INTER_SENTENCE_DELAY_MS, self._start_next_sentence)
        elif self._fade_on_done:
            self._stop_all()
        else:
            self._is_animating = False

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
