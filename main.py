"""
main.py
Entry point de Iris.
"""

import sys
import warnings
import signal

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)


def main():
    print("=" * 50)
    print("  IRIS — Iniciando sistema...")
    print("=" * 50)

    from core.agent import IrisAgent
    iris = IrisAgent()

    def shutdown(sig=None, frame=None):
        print("\n\n[Iris] Guardando memorias antes de cerrar...")
        iris.shutdown()
        print("[Iris] Hasta luego.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Iniciar voz
    print("[Voice] Iniciando sistema de voz...")
    try:
        iris.start_voice()
        print("[Voice] Listo — presiona el botón Copilot para hablar con Iris.")
    except Exception as e:
        print(f"[Voice] Error: {e}")
        import traceback
        traceback.print_exc()
        print("[Voice] Continuando en modo texto.")

    stats = iris.memory.get_stats()
    print(f"\n[Sistema listo] — {iris.personality.get_status_summary()}")
    print(f"[Memoria] {stats['total_memories']} hechos | {stats['total_messages']} mensajes | STM: {iris.get_status()['stm_loaded']} cargados")
    print("\nComandos: /status | /memoria | /guardar | /reset | /trust +N | /salir\n")
    print("-" * 50)

    while True:
        try:
            user_input = input("\nTú: ").strip()
            if not user_input:
                continue

            if user_input.startswith("/"):
                _handle_command(user_input, iris)
                continue

            print("\nIris: ", end="", flush=True)
            response = iris.chat(user_input)
            print(response)

        except KeyboardInterrupt:
            shutdown()
        except Exception as e:
            print(f"\n[Error] {e}")
            import traceback
            traceback.print_exc()


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
            sys.exit(0)

        case _:
            print(f"[Sistema] Comando desconocido: {command}")


if __name__ == "__main__":
    main()