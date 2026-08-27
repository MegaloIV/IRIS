"""
core/commands.py
Dispatcher de input y comandos de Iris.

Los comandos son los mismos se escriban donde se escriban — terminal, ventana o
Telegram. Lo único que cambia por canal es si se permite apagar el proceso.
"""

COMMANDS = frozenset({
    "/status", "/memoria", "/gustos", "/coste",
    "/guardar", "/reset", "/trust", "/energy", "/salir",
})


def is_command(text: str) -> bool:
    """
    ¿Es una de las órdenes de Iris?

    Se comprueba contra la lista, no con un `startswith("/")`, porque en Telegram
    la barra es parte de la interfaz: `/start` y `/help` los manda el propio
    cliente. Lo que no esté aquí tiene que llegarle a Iris como texto normal, no
    rebotar con «comando desconocido».
    """
    partes = text.strip().split()
    return bool(partes) and partes[0].lower() in COMMANDS


def dispatch_input(user_input: str, file_path, iris, use_delegation: bool, on_delegating=None) -> str:
    if use_delegation:
        return iris.delegate_to_claude(user_input, file_path, on_delegating=on_delegating)
    return iris.chat(user_input)


def handle_command(cmd: str, iris, allow_shutdown: bool = True) -> str:
    """
    allow_shutdown: False para los canales remotos. Un `/salir` escrito sin
    querer desde el móvil no debe tumbar el servidor donde vive Iris.
    """
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

        case "/gustos":
            # Ventana a las preferencias que se están formando. Ya influyen en
            # cómo responde Iris (personality.build_system_prompt); esto sirve
            # para calibrar el umbral y ver qué se está asentando.
            out.append(iris.preferences.summary())

        case "/coste":
            # Lo que costaría la delegación si se pagara por token. Hoy va contra
            # la suscripción, pero es la cifra que decide si algún día compensa
            # mover Claude al servidor y dejar de depender del portátil.
            from core.claude_delegate import ledger
            out.append(ledger.summary())

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
            if not allow_shutdown:
                out.append("Desde aquí no me apago — tendrías que hacerlo en la máquina donde corro.")
                return "\n".join(out)
            out.append("Guardando y cerrando...")
            iris.shutdown()
            # Cerrar la ventana solo tiene sentido donde hay ventana. En modo
            # servidor este comando llega por POST /command y aquí no hay Qt
            # instalado siquiera: importarlo a ciegas convertía un /salir en un
            # ImportError con pinta de "no pude alcanzar el servidor".
            try:
                from PyQt6.QtWidgets import QApplication
            except ImportError:
                pass
            else:
                if app := QApplication.instance():
                    app.quit()

        case _:
            out.append(f"Comando desconocido: {command}")

    return "\n".join(out)
