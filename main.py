"""
main.py
Entry point de Iris con UI flotante en la derecha.
"""

import sys
import warnings
import signal
import threading
from PyQt6.QtWidgets import QApplication

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)

from ui.avatar import IrisAvatarUI, IrisSignals

def main():
    print("=" * 50)
    print("  IRIS — Iniciando sistema...")
    print("=" * 50)

    app = QApplication(sys.argv)
    ui_signals = IrisSignals()
    avatar_window = IrisAvatarUI(ui_signals)
    avatar_window.show()

    from core.agent import IrisAgent
    iris = IrisAgent()

    # Interceptamos solo para actualizar el estado de ánimo (las caras) ANTES de hablar
    original_chat_stream = iris.chat_stream_voice
    
    def hooked_chat_stream(user_input, on_sentence):
        changes = iris.personality.analyze_input(user_input)
        if changes.get("mood"):
            ui_signals.mood_updated.emit(changes["mood"].value)
        return original_chat_stream(user_input, on_sentence)

    iris.chat_stream_voice = hooked_chat_stream

    def shutdown(sig=None, frame=None):
        print("\n\n[Iris] Guardando memorias antes de cerrar...")
        iris.shutdown()
        print("[Iris] Hasta luego.")
        QApplication.quit()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print("[Voice] Iniciando sistema de voz...")
    try:
        # Aquí inyectamos la señal para que actualice la UI al mismo tiempo que el TTS
        iris.start_voice(on_speaking_sentence=ui_signals.text_updated.emit)
        print("[Voice] Listo — presiona el botón Copilot para hablar con Iris.")
    except Exception as e:
        print(f"[Voice] Error: {e}")
        print("[Voice] Continuando en modo texto.")

    stats = iris.memory.get_stats()
    print(f"\n[Sistema listo] — {iris.personality.get_status_summary()}")
    print("-" * 50)

    def terminal_loop():
        while True:
            try:
                user_input = input("\nTú: ").strip()
                if not user_input: continue
                if user_input.startswith("/"):
                    _handle_command(user_input, iris)
                    continue

                print("\nIris: ", end="", flush=True)
                
                # Si le escribes por teclado, también actualizamos su cara
                ui_signals.mood_updated.emit(iris.personality.state.mood.value) 
                
                response = iris.chat(user_input)
                print(response)

                # Si hablas por texto, mostramos la respuesta en la UI un par de segundos
                ui_signals.text_updated.emit(response)
                threading.Timer(5.0, lambda: ui_signals.text_updated.emit("")).start()
                
                ui_signals.mood_updated.emit(iris.personality.state.mood.value)

            except KeyboardInterrupt:
                QApplication.instance().quit()
                break
            except Exception as e:
                print(f"\n[Error] {e}")

    threading.Thread(target=terminal_loop, daemon=True).start()
    sys.exit(app.exec())


def _handle_command(cmd: str, iris):
    parts   = cmd.strip().split()
    command = parts[0].lower()

    match command:
        case "/status":
            s = iris.get_status()
            print(f"\n  Mood:         {s['mood']}")
            print(f"  Trust:        {s['trust_level']:.1f}/100 ({s['trust_stage']})")
            print(f"  Te llama:     {s['owner_address']}")
            print(f"  Memorias LTM: {s['total_memories']}")
            print(f"  Mensajes DB:  {s['total_messages']}")
            print(f"  STM cargado:  {s['stm_loaded']} mensajes")
            print(f"  Sesión:       {s['session_messages']} mensajes en buffer")
            print(f"  Voz activa:   {s['voice_active']}")

        case "/memoria":
            memories = iris.memory.get_all_memories()
            if not memories:
                print("\n[Memoria] Sin memorias almacenadas todavía.")
            else:
                print(f"\n[Memoria] {len(memories)} memorias:\n")
                for i, m in enumerate(memories, 1):
                    importance = "⭐" * m.get("importance", 1)
                    category   = m.get("category", "?")
                    date_label = m.get("date_label", "")
                    date_str   = f" — {date_label}" if date_label else ""
                    print(f"  {i}. [{category}] {m['content']}{date_str} {importance}")

        case "/guardar":
            print("[Memoria] Forzando extracción...")
            iris.memory.force_close_session()
            print("[Memoria] Listo. Usa /memoria para ver qué extrajo.")

        case "/reset":
            iris.reset_conversation()
            print("[Sistema] Conversación reiniciada.")

        case "/trust":
            if len(parts) >= 2:
                try:
                    amount = float(parts[1])
                    iris.personality.adjust_trust(amount, "ajuste manual")
                    print(f"[Debug] Trust → {iris.personality.state.trust_level:.1f}")
                except ValueError:
                    print("[Error] Uso: /trust +10 o /trust -5")

        case "/salir":
            print("\n[Iris] Guardando memorias...")
            iris.shutdown()
            print("[Iris] Hasta luego.")
            QApplication.instance().quit() # Cerrar la interfaz correctamente

        case _:
            print(f"[Sistema] Comando desconocido: {command}")


if __name__ == "__main__":
    main()