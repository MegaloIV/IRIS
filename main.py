"""
main.py
Entry point de Iris.
"""

import logging
import os
import sys
import warnings
import signal
import threading
from PyQt6.QtWidgets import QApplication

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)


def _setup_logging():
    """
    Sin esto, el nivel raíz de Python es WARNING y TODOS los logging.info del
    proyecto son invisibles: que Supabase conectó, cuántas memorias hay, qué
    preferencias se formaron, si el grafo tiene algo dentro. Los fallos se ven,
    los aciertos no — así es como el grafo estuvo meses sin existir sin que
    nadie se enterara.

    IRIS_LOG_LEVEL=DEBUG para ver también los ajustes de trust y energía.
    """
    level = getattr(logging, os.getenv("IRIS_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        # force: basicConfig no hace nada si algo ya configuró el root logger, y
        # varias librerías lo hacen al importarse. Sin esto, el arreglo entero
        # es un no-op según qué se importe antes.
        force=True,
    )
    # Librerías que hablan demasiado y no dicen nada nuestro
    for noisy in ("httpx", "httpcore", "urllib3", "huggingface_hub", "filelock",
                  "sentence_transformers", "transformers", "groq", "telegram",
                  "apscheduler", "asyncio", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_setup_logging()

from config.settings import settings


def run_server():
    """
    Modo servidor: sin UI, sin voz, sin ventanas. Solo el cerebro.
    Lo que necesita estar en el portátil se lo pide al agente por WebSocket.
    """
    from core.agent import IrisAgent
    from core.startup import setup_proactive, run_api_server

    print("=" * 50)
    print("  IRIS — modo SERVIDOR")
    print("=" * 50)

    iris = IrisAgent()

    class _NoUI:
        """El motor proactivo espera señales de UI que aquí no existen."""
        def __getattr__(self, _):
            class _Sig:
                @staticmethod
                def emit(*a, **k): pass
            return _Sig()

    setup_proactive(iris, _NoUI(), telegram_active=settings.telegram.enabled)
    try:
        run_api_server(iris)
    finally:
        iris.shutdown()


from ui.avatar import IrisAvatarUI
from ui.signals import IrisSignals
from ui.terminal_overlay import TerminalOutputUI
from core.startup import setup_telegram, setup_proactive, setup_voice, setup_agent_link
from core.commands import handle_command, dispatch_input, is_command


def main():
    if settings.mode.mode == "server":
        return run_server()

    print("=" * 50)
    print(f"  IRIS — Iniciando sistema... (modo {settings.mode.mode})")
    print("=" * 50)

    app = QApplication(sys.argv)
    ui_signals = IrisSignals()

    avatar_window   = IrisAvatarUI(ui_signals)
    terminal_window = TerminalOutputUI()
    avatar_window.show()
    ui_signals.terminal_output_updated.connect(terminal_window.show_message)

    if settings.mode.mode == "client":
        # El cerebro está en el servidor. Instanciar IrisAgent aquí crearía un
        # segundo agente escribiendo la misma fila de estado y el mismo
        # historial que el del servidor: dos personalidades pisándose.
        from core.remote_iris import RemoteIris
        iris = RemoteIris()
        print(f"[Iris] Cerebro en {settings.mode.server_url} — aquí solo interfaz, voz y capacidades.")
    else:
        from core.agent import IrisAgent
        iris = IrisAgent()

    # Inyecta el emit de mood en el stream de voz sin modificar el agente
    _original_stream = iris.chat_stream_voice
    def _hooked_stream(user_input, on_sentence):
        result = _original_stream(user_input, on_sentence)
        ui_signals.mood_updated.emit(iris.personality.state.mood.value)
        return result
    iris.chat_stream_voice = _hooked_stream

    # En modo cliente el cerebro está en el servidor: aquí solo se ofrece
    # Claude y el escritorio, y Telegram/proactivo los lleva el otro lado.
    agent_link = setup_agent_link()

    if settings.mode.mode == "client":
        cf_proc, telegram_active, proactive_engine = None, False, None
    else:
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
        # is_command, no startswith("/"): así una barra que no sea un comando le
        # llega a Iris como texto, igual que por Telegram. Los tres canales
        # reconocen exactamente la misma lista.
        if is_command(user_input):
            out = (iris.run_command(user_input) if settings.mode.mode == "client"
                   else handle_command(user_input, iris))
            ui_signals.terminal_output_updated.emit(out)
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
                if is_command(user_input):
                    print(iris.run_command(user_input) if settings.mode.mode == "client"
                          else handle_command(user_input, iris))
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
        if agent_link:
            agent_link.stop()
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
