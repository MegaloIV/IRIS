"""
ui/avatar.py
Interfaz flotante de Iris usando PyQt6.
"""

import os
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QHBoxLayout, QApplication
from PyQt6.QtCore import Qt, pyqtSignal, QObject
from PyQt6.QtGui import QPixmap, QFont

class IrisSignals(QObject):
    # Señales para comunicar el backend con el hilo de la UI
    text_updated = pyqtSignal(str)
    mood_updated = pyqtSignal(str)

class IrisAvatarUI(QWidget):
    def __init__(self, signals: IrisSignals):
        super().__init__()
        self.signals = signals
        self.avatar_path = "assets/avatars/"
        
        self.init_ui()
        
        # Conectar las señales a las funciones que actualizan la UI
        self.signals.text_updated.connect(self.update_subtitles)
        self.signals.mood_updated.connect(self.update_avatar)

    def init_ui(self):
        # Configurar ventana: Sin bordes, siempre arriba, no aparece en la barra de tareas
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | 
            Qt.WindowType.WindowStaysOnTopHint | 
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Layout alineado a la derecha
        main_layout = QHBoxLayout()
        main_layout.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)

        # 1. Los Subtítulos (Globo de texto estilo cómic)
        self.text_label = QLabel("...")
        self.text_label.setFont(QFont("Comic Sans MS", 12, QFont.Weight.Bold))
        self.text_label.setStyleSheet("""
            QLabel {
                background-color: #FFFFFF;
                color: #000000;
                border-radius: 20px;
                padding: 15px;
                border: 3px solid #000000;
                margin-right: 15px;
                margin-bottom: 50px; /* Eleva un poco el globo para que apunte a su boca */
            }
        """)
        self.text_label.setWordWrap(True)
        self.text_label.setFixedWidth(250)
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text_label.setVisible(False)

        # 2. El Avatar (Tamaño reducido)
        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(180, 252) # Reducido para no estorbar
        self.avatar_label.setScaledContents(True)
        self.update_avatar("neutral") 

        # Añadir al layout (Primero el texto, luego el avatar, para que ella quede pegada a la derecha)
        main_layout.addWidget(self.text_label)
        main_layout.addWidget(self.avatar_label)
        
        self.setLayout(main_layout)

        # Posicionar en la esquina inferior derecha
        screen = QApplication.primaryScreen().geometry()
        window_width = 460
        window_height = 300
        # Margen de 20px a la derecha y 50px abajo
        self.setGeometry(screen.width() - window_width - 20, screen.height() - window_height - 50, window_width, window_height) 

    def update_avatar(self, mood: str):
        import random
        if mood == "neutral":
            variante = random.randint(1, 3)
            img_name = f"neutral_{variante}.png"
        else:
            img_name = f"{mood}.png"
            
        img_path = os.path.join(self.avatar_path, img_name)
        if not os.path.exists(img_path):
            img_path = os.path.join(self.avatar_path, "neutral_1.png")
            
        if os.path.exists(img_path):
            pixmap = QPixmap(img_path)
            self.avatar_label.setPixmap(pixmap)

    def update_subtitles(self, text: str):
        if not text or text == "...":
            self.text_label.setVisible(False)
        else:
            self.text_label.setText(text)
            self.text_label.setVisible(True)