"""
main.py
Entry point de Iris.
"""

import sys
import warnings
import signal
import threading
from PyQt6.QtWidgets import QApplication

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)

from ui.avatar import IrisAvatarUI
from ui.signals import IrisSignals
from ui.terminal_overlay import TerminalOutputUI
from core.startup import setup_telegram, setup_proactive, setup_voice
from core.commands import handle_command, dispatch_input


def main():
    print("=" * 50)
    print("  IRIS — Iniciando sistema...")
    print("=" * 50)

    app = QApplication(sys.argv)
    ui_signals = IrisSignals()

    avatar_window   = IrisAvatarUI(ui_signals)
    terminal_window = TerminalOutputUI()
    avatar_window.show()
    ui_signals.terminal_output_updated.connect(terminal_window.show_message)

    from core.agent import IrisAgent
    iris = IrisAgent()

    # Inyecta el emit de mood en el stream de voz sin modificar el agente
    _original_stream = iris.chat_stream_voice
    def _hooked_stream(user_input, on_sentence):
        result = _original_stream(user_input, on_sentence)
        ui_signals.mood_updated.emit(iris.personality.state.mood.value)
        return result
    iris.chat_stream_voice = _hooked_stream

    cf_proc, telegram_active = setup_telegram(iris)
    proactive_engine         = setup_proactive(iris, ui_signals, telegram_active)
    setup_voice(iris, ui_signals)

    print(f"\n[Sistema listo] — {iris.personality.get_status_summary()}")
    print("-" * 50)

    tts_enabled       = True
    claude_delegation = True

    # ── UI input ──────────────────────────────────────────────────────────────

    def handle_ui_input(user_input: str, attached_file: str = ""):
        print(f"\nTú (UI): {user_input}")
        if user_input.startswith("/"):
            ui_signals.terminal_output_updated.emit(handle_command(user_input, iris))
            return

        def worker():
            try:
                ui_signals.mood_updated.emit(iris.personality.state.mood.value)
                response = dispatch_input(
                    user_input,
                    attached_file or None,
                    iris,
                    claude_delegation,
                    on_delegating=lambda: ui_signals.claude_thinking_changed.emit(True),
                )
                print(f"Iris: {response}")
                ui_signals.text_updated.emit(response)
                ui_signals.mood_updated.emit(iris.personality.state.mood.value)
                if tts_enabled:
                    iris.speak(response)
            except Exception as e:
                print(f"\n[Error UI Input] {e}")
                ui_signals.text_updated.emit(f"[Error]\n{str(e)}")
            finally:
                ui_signals.claude_thinking_changed.emit(False)

        threading.Thread(target=worker, daemon=True).start()

    def on_voice_mode_changed(enabled: bool):
        nonlocal tts_enabled
        tts_enabled = enabled
        iris.set_tts_enabled(enabled)
        print(f"[Iris] Modo cambiado: {'Voz' if enabled else 'Solo texto'}")

    def on_claude_delegation_changed(enabled: bool):
        nonlocal claude_delegation
        claude_delegation = enabled
        print(f"[Iris] Consultar Claude: {'activado' if enabled else 'desactivado'}")

    ui_signals.user_text_submitted.connect(handle_ui_input)
    ui_signals.voice_mode_changed.connect(on_voice_mode_changed)
    ui_signals.claude_delegation_enabled.connect(on_claude_delegation_changed)

    # ── Terminal loop ─────────────────────────────────────────────────────────

    def terminal_loop():
        while True:
            try:
                user_input = input("\nTú: ").strip()
                if not user_input:
                    continue
                if user_input.startswith("/"):
                    print(handle_command(user_input, iris))
                    continue

                print("\nIris: ", end="", flush=True)
                ui_signals.mood_updated.emit(iris.personality.state.mood.value)

                def _on_delegating():
                    print("[delegando a Claude Code...]")
                    ui_signals.claude_thinking_changed.emit(True)

                response = dispatch_input(
                    user_input, None, iris, claude_delegation,
                    on_delegating=_on_delegating,
                )
                ui_signals.claude_thinking_changed.emit(False)
                print(response)
                ui_signals.text_updated.emit(response)
                ui_signals.mood_updated.emit(iris.personality.state.mood.value)

            except KeyboardInterrupt:
                QApplication.instance().quit()
                break
            except Exception as e:
                print(f"\n[Error] {e}")

    threading.Thread(target=terminal_loop, daemon=True).start()

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def shutdown(sig=None, frame=None):
        print("\n\n[Iris] Guardando memorias antes de cerrar...")
        if proactive_engine:
            proactive_engine.stop()
        iris.shutdown()
        if cf_proc:
            cf_proc.kill()
        print("[Iris] Hasta luego.")
        QApplication.quit()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
