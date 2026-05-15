from PyQt6.QtCore import pyqtSignal, QObject


class IrisSignals(QObject):
    text_updated            = pyqtSignal(str)
    mood_updated            = pyqtSignal(str)
    user_text_submitted     = pyqtSignal(str)
    terminal_output_updated = pyqtSignal(str)
    voice_mode_changed      = pyqtSignal(bool)
    listening_changed       = pyqtSignal(bool)
    claude_thinking_changed = pyqtSignal(bool)
