"""
core/commands.py
Dispatcher de input y comandos de terminal para Iris.
"""


def dispatch_input(user_input: str, file_path, iris, use_delegation: bool, on_delegating=None) -> str:
    if use_delegation:
        return iris.delegate_to_claude(user_input, file_path, on_delegating=on_delegating)
    return iris.chat(user_input)


def handle_command(cmd: str, iris) -> str:
    parts   = cmd.strip().split()
    command = parts[0].lower()
    out     = []

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
                    content    = (m['content'][:30] + '..') if len(m['content']) > 30 else m['content']
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

        case "/energy":
            if len(parts) >= 2:
                try:
                    amount = float(parts[1])
                    old = iris.personality.state.energy
                    iris.personality.state.energy = max(0.0, min(100.0, old + amount))
                    iris.personality.save_state()
                    out.append(f"Energy → {iris.personality.state.energy:.1f} ({iris.personality.get_energy_stage()})")
                except ValueError:
                    out.append("Error. Uso: /energy +20")
            else:
                out.append("Error. Uso: /energy +20")

        case "/salir":
            out.append("Guardando y cerrando...")
            iris.shutdown()
            from PyQt6.QtWidgets import QApplication
            QApplication.instance().quit()

        case _:
            out.append(f"Comando desconocido: {command}")

    return "\n".join(out)
