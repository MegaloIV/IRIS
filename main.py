"""
main.py
Entry point de Iris con UI flotante estática y terminal integrada.
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
from config.settings import settings


def _run_telegram_server(iris) -> None:
    """Run the FastAPI webhook server in a dedicated thread with its own event loop."""
    import asyncio
    import uvicorn
    from interfaces.telegram_bot import create_telegram_app

    app  = create_telegram_app(iris)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    config = uvicorn.Config(
        app,
        host=settings.server.host,
        port=settings.server.port,
        loop="none",
        log_level="warning",
    )
    loop.run_until_complete(uvicorn.Server(config).serve())


def _telegram_send_sync(token: str, chat_id: int, text: str) -> bool:
    """Send a Telegram message via HTTP (no async loop needed)."""
    import requests
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        return r.ok
    except Exception as e:
        import logging
        logging.warning(f"[Proactive] Telegram send falló: {e}")
        return False

def main():
    print("=" * 50)
    print("  IRIS — Iniciando sistema...")
    print("=" * 50)

    app = QApplication(sys.argv)
    ui_signals = IrisSignals()
    
    avatar_window = IrisAvatarUI(ui_signals)
    terminal_window = TerminalOutputUI()
    
    avatar_window.show()
    ui_signals.terminal_output_updated.connect(terminal_window.show_message)

    from core.agent import IrisAgent
    iris = IrisAgent()

    original_chat_stream = iris.chat_stream_voice

    def hooked_chat_stream(user_input, on_sentence):
        result = original_chat_stream(user_input, on_sentence)
        ui_signals.mood_updated.emit(iris.personality.state.mood.value)
        return result

    iris.chat_stream_voice = hooked_chat_stream

    # ── Telegram (optional) ──────────────────────────────────────────────────
    _cf_proc        = None
    _telegram_active = False
    if settings.telegram.enabled and settings.telegram.bot_token:
        try:
            from scripts.start_telegram import start_cloudflared, set_webhook
            _cf_proc, public_url = start_cloudflared(settings.server.port)
            print(f"[cloudflared] URL pública: {public_url}")
            set_webhook(settings.telegram.bot_token, public_url)
            threading.Thread(target=_run_telegram_server, args=(iris,), daemon=True).start()
            _telegram_active = True
            print("[Telegram] Bot activo — envía un mensaje al bot para empezar.")
        except Exception as e:
            print(f"[Telegram] Error al iniciar: {e}")
            print("[Telegram] Continuando sin Telegram.")
    # ─────────────────────────────────────────────────────────────────────────

    # ── Motor proactivo ───────────────────────────────────────────────────────
    _proactive_engine = None
    try:
        from core.proactive import ProactiveEngine
        from config.prompts import TELEGRAM_INTERFACE_ADDON

        def _proactive_send(text: str):
            """Intenta Telegram primero; cae en UI de escritorio si no hay."""
            if (
                _telegram_active
                and settings.telegram.bot_token
                and settings.telegram.owner_id
            ):
                sent = _telegram_send_sync(
                    settings.telegram.bot_token,
                    settings.telegram.owner_id,
                    text,
                )
                if sent:
                    return
            # Fallback: UI de escritorio
            ui_signals.text_updated.emit(text)
            if iris._voice:
                iris.speak(text)

        iface_ctx = TELEGRAM_INTERFACE_ADDON if _telegram_active else ""
        _proactive_engine = ProactiveEngine(iris, _proactive_send, interface_context=iface_ctx)
        _proactive_engine.start()
    except Exception as e:
        print(f"[Proactive] No se pudo iniciar el motor proactivo: {e}")
    # ─────────────────────────────────────────────────────────────────────────

    def shutdown(sig=None, frame=None):
        print("\n\n[Iris] Guardando memorias antes de cerrar...")
        if _proactive_engine:
            _proactive_engine.stop()
        iris.shutdown()
        if _cf_proc:
            _cf_proc.kill()
        print("[Iris] Hasta luego.")
        QApplication.quit()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print("[Voice] Iniciando sistema de voz...")
    try:
        iris.start_voice(
            on_speaking_sentence=ui_signals.text_updated.emit,
            on_listening_changed=ui_signals.listening_changed.emit,
        )
        print("[Voice] Listo — presiona el botón Copilot para hablar con Iris.")
    except Exception as e:
        print(f"[Voice] Error: {e}")
        print("[Voice] Continuando en modo texto.")

    stats = iris.memory.get_stats()
    print(f"\n[Sistema listo] — {iris.personality.get_status_summary()}")
    print("-" * 50)

    def handle_ui_input(user_input, attached_file=""):
        print(f"\nTú (UI): {user_input}")

        if user_input.startswith("/"):
            cmd_output = _handle_command(user_input, iris)
            ui_signals.terminal_output_updated.emit(cmd_output)
            return

        def worker():
            try:
                ui_signals.mood_updated.emit(iris.personality.state.mood.value)

                if claude_delegation[0]:
                    response = iris.delegate_to_claude(
                        user_input,
                        attached_file or None,
                        on_delegating=lambda: ui_signals.claude_thinking_changed.emit(True),
                    )
                else:
                    response = iris.chat(user_input)

                print(f"Iris: {response}")
                ui_signals.text_updated.emit(response)
                ui_signals.mood_updated.emit(iris.personality.state.mood.value)
                if tts_enabled[0]:
                    iris.speak(response)
            except Exception as e:
                print(f"\n[Error UI Input] {e}")
                ui_signals.text_updated.emit(f"[Error]\n{str(e)}")
            finally:
                ui_signals.claude_thinking_changed.emit(False)

        threading.Thread(target=worker, daemon=True).start()

    ui_signals.user_text_submitted.connect(handle_ui_input)

    tts_enabled = [True]
    claude_delegation = [True]

    def on_voice_mode_changed(enabled: bool):
        tts_enabled[0] = enabled
        iris.set_tts_enabled(enabled)
        mode_label = "Voz" if enabled else "Solo texto"
        print(f"[Iris] Modo cambiado: {mode_label}")

    def on_claude_delegation_changed(enabled: bool):
        claude_delegation[0] = enabled
        label = "activado" if enabled else "desactivado"
        print(f"[Iris] Consultar Claude: {label}")

    ui_signals.voice_mode_changed.connect(on_voice_mode_changed)
    ui_signals.claude_delegation_enabled.connect(on_claude_delegation_changed)

    def terminal_loop():
        while True:
            try:
                user_input = input("\nTú: ").strip()
                if not user_input: continue
                if user_input.startswith("/"):
                    output = _handle_command(user_input, iris)
                    print(output)
                    continue

                print("\nIris: ", end="", flush=True)
                ui_signals.mood_updated.emit(iris.personality.state.mood.value)

                if claude_delegation[0]:
                    def _on_delegating():
                        print("[delegando a Claude Code...]")
                        ui_signals.claude_thinking_changed.emit(True)

                    response = iris.delegate_to_claude(user_input, on_delegating=_on_delegating)
                    ui_signals.claude_thinking_changed.emit(False)
                else:
                    response = iris.chat(user_input)
                print(response)

                ui_signals.text_updated.emit(response)
                ui_signals.mood_updated.emit(iris.personality.state.mood.value)

            except KeyboardInterrupt:
                QApplication.instance().quit()
                break
            except Exception as e:
                print(f"\n[Error] {e}")

    threading.Thread(target=terminal_loop, daemon=True).start()
    sys.exit(app.exec())

def _handle_command(cmd: str, iris) -> str:
    parts   = cmd.strip().split()
    command = parts[0].lower()
    out = [] 

    match command:
        case "/status":
            s = iris.get_status()
            out.append(f"Mood: {s['mood']}")
            out.append(f"Trust: {s['trust_level']:.1f}/100 ({s['trust_stage']})")
            out.append(f"Energy: {s['energy']:.0f}/100 ({iris.personality.get_energy_stage()})")
            out.append(f"User: {s['owner_address']}")
            out.append(f"DB Msgs: {s['total_messages']}")
            out.append(f"Voz: {s['voice_active']}")

        case "/memoria":
            memories = iris.memory.get_all_memories()
            if not memories:
                out.append("Sin memorias.")
            else:
                out.append(f"{len(memories)} memorias:")
                for i, m in enumerate(memories[-3:], 1):
                    importance = "⭐" * m.get("importance", 1)
                    category   = m.get("category", "?")
                    content = (m['content'][:30] + '..') if len(m['content']) > 30 else m['content']
                    out.append(f" {i}. [{category}] {content} {importance}")

        case "/guardar":
            out.append("Forzando extracción...")
            iris.memory.force_close_session()
            out.append("Listo. Forzado.")

        case "/reset":
            iris.reset_conversation()
            out.append("Conversación reiniciada.")

        case "/trust":
            if len(parts) >= 2:
                try:
                    amount = float(parts[1])
                    iris.personality.adjust_trust(amount, "ajuste manual")
                    iris.personality.save_state()
                    out.append(f"Trust → {iris.personality.state.trust_level:.1f}")
                except ValueError:
                    out.append("Error. Uso: /trust +10")
            else:
                out.append("Error. Uso: /trust +10")

        case "/salir":
            out.append("Guardando y cerrando...")
            iris.shutdown()
            QApplication.instance().quit()

        case _:
            out.append(f"Comando desconocido: {command}")

    return "\n".join(out)

if __name__ == "__main__":
    main()