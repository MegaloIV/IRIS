"""
core/claude_delegate.py
Detección y delegación de tareas complejas a Claude Code.

Flujo:
  1. IntentAgent.analyze() — Groq entiende el intent real y genera un prompt técnico
  2. ClaudeDelegator.run_sync() — llama a Claude Code con ese prompt y con la
     personalidad de Iris en --append-system-prompt, así que lo que vuelve ya es
     la respuesta de Iris y no hay que reescribirla con un tercer modelo.

Se invoca con --output-format json: de ahí salen el texto, si hubo error,
el session_id para --resume y el total_cost_usd que mide lo que costaría
esto si se pagara por token.
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


# ─── Pre-filtro heurístico ────────────────────────────────────────────────────

_DELEGATION_PATTERNS = [
    re.compile(
        r'\b(crea?r?|escrib[ei]r?|genera?r?|busca?r?|lee?r?|analiza?r?|'
        r'resum[ei]r?|guarda?r?|abre?r?|ejecuta?r?|modifica?r?|edita?r?|lista?r?)\b',
        re.I,
    ),
    re.compile(
        r'\.(py|txt|pdf|docx?|xlsx?|jpg|jpeg|png|gif|webp|csv|json|md|html?|js|ts|cpp?|c|java|rb|go|rs|sh|yaml|toml)\b',
        re.I,
    ),
    re.compile(
        r'\b(archivo|carpeta|directorio|escritorio|documento|código|script|'
        r'programa|función|clase|módulo|proyecto|repositorio|repo)\b',
        re.I,
    ),
    re.compile(r'\bPATH_\w+', re.I),
    # desktop control
    re.compile(
        r'\b(clicke?a?r?|hace?r? click|mouse|pantalla|ventana|aplicaci[oó]n|'
        r'spotify|chrome|firefox|navegador|notepad|calculadora|abre? el|'
        r'cierra? el|minimiza?|maximiza?|escrib[eí] en|tecla|atajo|'
        r'captura de pantalla|screenshot|que hay en (la )?pantalla)\b',
        re.I,
    ),
]

# Lo que parece una petición y no lo es. El verbo suelto dispara demasiado:
# "¿te gustaría crear una banda conmigo?" tiene "crear" y no es una tarea. Antes
# eso solo costaba latencia; con Claude en el portátil consume cuota de la
# suscripción, así que ahora conviene filtrar de verdad.
_CONVERSATIONAL_PATTERNS = [
    # Preguntas sobre gustos, opiniones o hipótesis: hablan DE la acción, no la piden.
    re.compile(
        r'\b(te gustar[íi]a|te apetece|quieres que|querr[íi]as|te molar[íi]a|'
        r'qu[ée] opinas|qu[ée] piensas|c[oó]mo ser[íi]a|imag[íi]nate|'
        r'te imaginas|alguna vez|te acuerdas|recuerdas cuando)\b',
        re.I,
    ),
    # Hablar sobre la propia Iris, no pedirle una tarea.
    re.compile(
        r'\b(c[oó]mo (te sientes|est[áa]s)|qu[ée] tal (est[áa]s|te)|'
        r'sabes (crear|escribir|leer|buscar)|puedes (crear|escribir|leer|buscar)\?)\b',
        re.I,
    ),
]

# Señales de que sí es una tarea, aunque la frase parezca conversacional.
_EXPLICIT_TASK_PATTERNS = [
    re.compile(r'\bPATH_\w+', re.I),
    re.compile(
        r'\.(py|txt|pdf|docx?|xlsx?|jpg|jpeg|png|gif|webp|csv|json|md|html?|js|ts|cpp?|c|java|rb|go|rs|sh|yaml|toml)\b',
        re.I,
    ),
    re.compile(r'(^|\s)(/|~/|[A-Za-z]:\\)', re.I),          # una ruta de verdad
    re.compile(r'\b(por favor|hazlo|h[aá]zme|ábre?me|créa?me)\b', re.I),
]


def _quick_delegate_check(text: str, file_path: str | None) -> bool:
    """
    True si el mensaje tiene señales de necesitar Claude Code. False → chat directo.

    Un adjunto no admite duda. Sin adjunto, hace falta una señal de tarea y que
    la frase no sea claramente conversacional — salvo que además haya algo
    inequívoco (una ruta, un archivo, un "por favor hazlo"), en cuyo caso manda eso.
    """
    if file_path:
        return True
    if not any(p.search(text) for p in _DELEGATION_PATTERNS):
        return False
    if any(p.search(text) for p in _EXPLICIT_TASK_PATTERNS):
        return True
    if any(p.search(text) for p in _CONVERSATIONAL_PATTERNS):
        logger.debug(f"[Prefiltro] Suena conversacional, no delego: {text[:80]}")
        return False
    return True


# ─── Contador de coste ────────────────────────────────────────────────────────


class CostLedger:
    """
    Lo que llevaría gastada la delegación si se pagara por token.

    Hoy no se paga — va contra la suscripción — pero es exactamente la medición
    que decide si la fase 07 (Claude por API, sin depender del portátil) sale a
    cuenta. Sin esto la decisión sería a ojo.

    Vive en memoria a propósito: interesa el ritmo de gasto de una temporada de
    uso real, no un total histórico, y así no hay una tabla más que mantener.
    """

    def __init__(self):
        self.calls        = 0
        self.failed       = 0
        self.total_usd    = 0.0
        self.since        = time.time()
        self._lock        = threading.Lock()

    def record(self, result: "ClaudeResult") -> None:
        with self._lock:
            self.calls     += 1
            self.total_usd += result.cost_usd
            if not result.ok:
                self.failed += 1
            calls, total = self.calls, self.total_usd
        logger.info(
            f"[Claude] {result.cost_usd:.4f} $ · {result.duration_ms} ms · "
            f"{result.num_turns} turno(s) — acumulado {total:.2f} $ en {calls} llamadas"
        )

    def summary(self) -> str:
        with self._lock:
            if not self.calls:
                return "Todavía no he delegado nada en esta sesión."
            horas = max((time.time() - self.since) / 3600, 0.01)
            media = self.total_usd / self.calls
            fallos = f" · {self.failed} fallidas" if self.failed else ""
            return (
                f"{self.calls} delegaciones{fallos} en {horas:.1f} h\n"
                f"Coste equivalente por API: {self.total_usd:.2f} $ "
                f"(media {media:.3f} $/llamada, ~{self.total_usd / horas:.2f} $/h)\n"
                f"Pagado de verdad: 0 $ — va contra la suscripción."
            )


ledger = CostLedger()


# ─── Resultado de una llamada a Claude ────────────────────────────────────────


@dataclass
class ClaudeResult:
    """
    Lo que devuelve `claude -p --output-format json`.

    Antes esto era un `str` y el fallo se detectaba mirando si el texto empezaba
    por "[Error", "[Timeout"… — con lo que cualquier respuesta legítima que
    empezara por un corchete se tomaba por error. Ahora `ok` viene del campo
    `is_error` de la propia CLI, que es quien lo sabe.
    """

    text: str = ""
    ok: bool = True
    session_id: str = ""
    cost_usd: float = 0.0
    usage: dict = field(default_factory=dict)
    duration_ms: int = 0
    num_turns: int = 0

    @classmethod
    def failure(cls, message: str) -> "ClaudeResult":
        return cls(text=message, ok=False)

    def to_dict(self) -> dict:
        """Para que quepa en el WebSocket cuando Claude corre en el portátil."""
        return {
            "text": self.text, "ok": self.ok, "session_id": self.session_id,
            "cost_usd": self.cost_usd, "usage": self.usage,
            "duration_ms": self.duration_ms, "num_turns": self.num_turns,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ClaudeResult":
        return cls(
            text=data.get("text", ""),
            ok=bool(data.get("ok", True)),
            session_id=data.get("session_id", "") or "",
            cost_usd=float(data.get("cost_usd") or 0.0),
            usage=data.get("usage") or {},
            duration_ms=int(data.get("duration_ms") or 0),
            num_turns=int(data.get("num_turns") or 0),
        )

    def as_json(self):
        """El `result` cuando se pidió con --json-schema: ya viene limpio, sin fences."""
        return json.loads(self.text)


# ─── Validación de rutas ──────────────────────────────────────────────────────

_PATH_REF_RE = re.compile(r'PATH_\w+')


def _validate_path_refs(prompt: str) -> list[str]:
    """Variables PATH_ referenciadas en el prompt pero no definidas en el entorno."""
    refs = set(_PATH_REF_RE.findall(prompt))
    return [r for r in refs if r not in os.environ]


# ─── Companion desktop ───────────────────────────────────────────────────────

_COMPANION_BAT_WSL = Path(__file__).parent.parent / "companion" / "start.bat"


def _wsl_to_windows_path(path: str) -> str:
    p = str(path)
    if p.startswith("/mnt/"):
        parts = p[5:].split("/", 1)
        drive = parts[0].upper()
        rest  = parts[1].replace("/", "\\") if len(parts) > 1 else ""
        return f"{drive}:\\{rest}"
    return p


def companion_headers() -> dict:
    """Cabecera de autenticación del companion. Vacía si aún no generó token."""
    from companion.auth import TOKEN_HEADER, read_token
    token = read_token()
    return {TOKEN_HEADER: token} if token else {}


def companion_get(companion_url: str, path: str, timeout: int = 10):
    import requests as _req
    return _req.get(f"{companion_url}{path}", headers=companion_headers(), timeout=timeout)


def companion_post(companion_url: str, path: str, payload: dict, timeout: int = 5):
    import requests as _req
    return _req.post(f"{companion_url}{path}", json=payload, headers=companion_headers(), timeout=timeout)


def take_desktop_snapshot(companion_url: str = "") -> dict:
    """
    Screenshot + elementos de UI.

    Solo viajan la ruta y los elementos. La captura ya no se manda en base64:
    quien la mira es Claude, y Claude corre en el portátil — la misma máquina
    donde el companion acaba de dejar el PNG. La ruta vale allí, y evita mover
    varios megas por el WebSocket en cada mirada a la pantalla.
    """
    from core.executor import desktop_request
    return desktop_request("GET", "/screenshot", timeout=20)


def execute_desktop_actions(actions: list[dict], companion_url: str = "") -> str:
    """Ejecuta la lista de acciones devuelta por Claude Code, esté donde esté el escritorio."""
    import time
    from core.executor import desktop_request
    done = []
    for act in actions:
        atype = act.get("action", "")
        try:
            if atype == "launch":
                desktop_request("POST", "/launch", {"app": act["app"]})
                done.append(f"Abrí {act['app']}")
                time.sleep(1.5)
            elif atype == "click":
                desktop_request("POST", "/click", {"x": act["x"], "y": act["y"], "button": act.get("button", "left")})
                done.append(f"Click en ({act['x']}, {act['y']})")
            elif atype == "double_click":
                desktop_request("POST", "/double_click", {"x": act["x"], "y": act["y"]})
                done.append(f"Doble click en ({act['x']}, {act['y']})")
            elif atype == "right_click":
                desktop_request("POST", "/right_click", {"x": act["x"], "y": act["y"]})
                done.append(f"Click derecho en ({act['x']}, {act['y']})")
            elif atype == "type":
                desktop_request("POST", "/type", {"text": act["text"]})
                done.append(f"Escribí: {act['text']}")
            elif atype == "key":
                desktop_request("POST", "/key", {"key": act["key"]})
                done.append(f"Tecla: {act['key']}")
            elif atype == "hotkey":
                desktop_request("POST", "/hotkey", {"keys": act["keys"]})
                done.append(f"Atajo: {'+'.join(act['keys'])}")
            elif atype == "scroll":
                desktop_request("POST", "/scroll", {"x": act["x"], "y": act["y"], "direction": act.get("direction", "down"), "amount": act.get("amount", 3)})
                done.append(f"Scroll {act.get('direction', 'down')}")
        except Exception as e:
            done.append(f"Error en {atype}: {e}")
    return "\n".join(done) if done else "Sin acciones ejecutadas"


def ensure_companion_alive(companion_url: str, timeout: int = 8) -> bool:
    """
    Verifica que el escritorio esté disponible; si es local y no corre, lo lanza.

    En modo servidor no hay nada que lanzar — el companion vive en el portátil,
    y lo único que se puede comprobar es si el agente está conectado. Si el
    portátil está apagado, la respuesta honesta es "ahora no puedo".
    """
    from config.settings import settings
    from core.executor import agent_available
    from core.link import protocol as _P

    if settings.mode.mode == "server":
        return agent_available(_P.CAP_DESKTOP)

    import requests as _req
    try:
        _req.get(f"{companion_url}/health", timeout=2)
        return True
    except Exception:
        pass

    bat_win = _wsl_to_windows_path(_COMPANION_BAT_WSL)
    print(f"[Companion] No está corriendo — iniciando {bat_win}...")
    try:
        # start "" abre el bat en su propia ventana (necesario para el servidor)
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "", bat_win],
            close_fds=True,
        )
    except Exception as e:
        print(f"[Companion] Error al iniciar: {e}")
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _req.get(f"{companion_url}/health", timeout=1)
            print("[Companion] Listo.")
            return True
        except Exception:
            time.sleep(0.5)

    print("[Companion] Timeout esperando al companion.")
    return False


# ─── Intent Agent ─────────────────────────────────────────────────────────────

class IntentAgent:
    """
    Usa Groq (analysis_llm) para entender la intención real del usuario
    y generar un prompt técnico optimizado para Claude Code.
    Es una llamada utilitaria — sin personalidad de Iris, sin español forzado.
    """

    def __init__(self, llm):
        self.llm = llm

    def analyze(self, user_input: str, detected_file_path: str | None = None) -> dict:
        """
        Analiza el mensaje del usuario y devuelve:
          should_delegate: bool   — confirma o descarta la delegación
          claude_prompt:   str    — prompt técnico optimizado para Claude Code
          file_path:       str|None — ruta de archivo resuelta/extraída
        
        En caso de error, hace fallback gracioso (delega con el input raw).
        """
        from config.prompts import DELEGATION_INTENT_PROMPT

        file_hint = (
            f"\nDetected file reference: {detected_file_path}"
            if detected_file_path else ""
        )
        prompt_text = DELEGATION_INTENT_PROMPT.format(
            user_input=user_input,
            file_hint=file_hint,
        )

        try:
            response = self.llm.invoke(prompt_text)
            raw = re.sub(r'```(?:json)?\s*', '', response.content).strip()
            result = json.loads(raw)

            should = bool(result.get("should_delegate", True))
            claude_prompt = (result.get("claude_prompt") or "").strip() or user_input
            file_path = result.get("file_path") or detected_file_path

            if file_path:
                should = True
                if result.get("task_type") == "conversational" and Path(file_path).suffix.lower() in _IMAGE_EXTENSIONS:
                    claude_prompt = (
                        f"The user showed you this image and said: '{user_input}'. "
                        f"Describe what you see in the image in first person, "
                        f"as if you are seeing it yourself. "
                        f"Respond naturally, not as a developer analyzing code."
                    )
            elif result.get("task_type") == "conversational":
                should = False

            print(
                f"[IntentAgent] should_delegate={should} "
                f"task_type={result.get('task_type', '?')} "
                f"file={file_path}"
            )
            return {
                "should_delegate": should,
                "claude_prompt":   claude_prompt,
                "file_path":       file_path,
                "task_type":       result.get("task_type", ""),
            }

        except Exception as e:
            print(f"[IntentAgent] Error — usando input raw como fallback: {e}")
            return {
                "should_delegate": True,
                "claude_prompt":   user_input,
                "file_path":       detected_file_path,
                "task_type":       "",
            }


# ─── Path helpers ─────────────────────────────────────────────────────────────

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
def windows_to_wsl_path(path: str) -> str:
    """Convert a Windows path to its WSL /mnt/ equivalent.
    'C:\\foo\\bar' → '/mnt/c/foo/bar'
    Already-Unix paths are returned unchanged.
    """
    if len(path) >= 2 and path[1] == ":":
        drive = path[0].lower()
        rest = path[2:].replace("\\", "/").lstrip("/")
        return f"/mnt/{drive}/{rest}"
    return path.replace("\\", "/")


def _build_file_prompt(user_prompt: str, file_path: str) -> str:
    """
    Adjunta el archivo al prompt pasando su RUTA, también las imágenes.

    Antes las imágenes se incrustaban como `data:image/png;base64,...` dentro
    del propio texto. Eso tenía dos problemas: Claude Code no lo trata como una
    imagen — es texto — y el base64 acababa en la línea de comandos, que para
    una captura a resolución nativa son megabytes. Su herramienta Read abre
    imágenes; con la ruta basta, y además funciona igual en modo servidor,
    porque el binario corre en el portátil, que es donde está el archivo.
    """
    wsl_path = windows_to_wsl_path(file_path)
    ext      = Path(file_path).suffix.lower()

    if ext in _IMAGE_EXTENSIONS:
        return (
            f"{user_prompt}\n\n"
            f"Imagen adjunta: {wsl_path}\n"
            f"Ábrela con la herramienta Read antes de responder."
        )
    return f"{user_prompt}\n\nFile path: {wsl_path}"


FILE_TASK_TYPES = [
    "file_creation", "file_reading", "file_search",
    "report_generation", "image_analysis", "document_analysis",
]


def _build_prompt(
    user_input: str,
    intent: dict,
    iris_context: dict | None = None,
    companion_url: str = "",
    speak_as_iris: bool = False,
) -> str:
    """
    Construye el prompt final para Claude Code.

    - Añade instrucciones de formato para que la respuesta sea limpia y fácil
      de integrar como voz de Iris (sin "I've done...", sin preambles).
    - Inyecta el nombre del usuario si está disponible.
    - Inyecta PATH_ vars para tareas de archivo.
    - Si hay PATH_ vars referenciadas pero no definidas, lo indica explícitamente
      para que Claude Code lo comunique en vez de fallar silenciosamente.
    """
    prompt = intent["claude_prompt"]

    if speak_as_iris:
        # La personalidad va aparte, en --append-system-prompt: Claude responde
        # SIENDO Iris, así que su salida ya es la respuesta final y no hay que
        # pedirle a un tercer modelo que la reescriba "en primera persona".
        format_block = [
            "=== CÓMO RESPONDER ===",
            "Haz la tarea y cuéntasela al usuario TÚ MISMA, en primera persona y con tu voz.",
            "Lo hiciste tú: 'lo creé', 'lo analicé', 'encontré'. Nunca digas 'Claude', "
            "'herramienta', 'sistema' ni 'análisis interno'.",
            "No hay preámbulos ni 'Done!'. Si el resultado es contenido (texto, código, "
            "análisis), entrégalo entero; si es un archivo, basta con decir dónde quedó.",
            "Es una conversación, no un informe: responde como responderías a cualquier "
            "otra cosa que te dijera.",
        ]
    else:
        # Instrucciones de formato — salida limpia para que Iris la integre bien
        format_block = [
            "=== OUTPUT INSTRUCTIONS ===",
            "Return ONLY the result or content requested. No preamble, no 'I've done...', no 'Done!'.",
            "If the task produces a file: one short confirmation line (e.g. 'Archivo creado en <ruta>').",
            "If the task produces content (text, code, analysis): return just the content.",
            "If analysis: findings directly, no meta-commentary about what you are doing.",
        ]
    if iris_context and iris_context.get("owner_name"):
        format_block.append(f"The user's name is: {iris_context['owner_name']}.")
    format_block.append("=== END INSTRUCTIONS ===")

    prompt = "\n".join(format_block) + "\n\n" + prompt

    # Rutas para tareas de archivo
    if intent.get("task_type") in FILE_TASK_TYPES:
        paths = {k: v for k, v in os.environ.items() if k.startswith("PATH_")}
        if paths:
            path_context = "Available paths:\n" + "\n".join(f"- {k}: {v}" for k, v in paths.items())
            prompt = f"{path_context}\n\n{prompt}"

        missing = _validate_path_refs(prompt)
        if missing:
            prompt += (
                f"\n\nNOTE: The path variable(s) {', '.join(missing)} are referenced but not defined. "
                "Use the current working directory or clearly state that the path is unknown."
            )

    # Prompt de control de escritorio
    if intent.get("task_type") == "desktop_control" and companion_url:
        from config.prompts import DESKTOP_CONTROL_PROMPT
        prompt = DESKTOP_CONTROL_PROMPT.format(
            companion_url=companion_url,
            task=intent["claude_prompt"],
        )

    return prompt


# ─── Claude Code subprocess ───────────────────────────────────────────────────

class ClaudeDelegator:
    """Ejecuta Claude Code como subprocess y devuelve un ClaudeResult."""

    TIMEOUT_SECONDS = 120

    def run_sync(
        self,
        prompt: str,
        file_path: str | None = None,
        system_prompt: str = "",
        resume_session: str = "",
        json_schema: dict | None = None,
        allowed_tools: str = "",
    ) -> ClaudeResult:
        """
        Llama a Claude Code y devuelve el resultado ya interpretado.

        system_prompt   — la personalidad de Iris. Con ella Claude responde
                          SIENDO Iris, y desaparece la llamada extra que antes
                          reescribía su salida "en primera persona".
        resume_session  — continúa la sesión anterior en vez de reenviar
                          contexto que Claude ya tiene.
        json_schema     — obliga a que la salida cumpla el esquema. Sustituye a
                          arrancarle las comillas de markdown a mano.
        allowed_tools   — acota los permisos por debajo del ajuste global. Lo usa
                          la vida interior: curiosear sin nadie delante no
                          necesita poder escribir en el disco.
        """
        if file_path:
            try:
                full_prompt = _build_file_prompt(prompt, file_path)
            except Exception as e:
                logger.warning(f"[Claude] Error preparando el archivo adjunto: {e}")
                full_prompt = f"{prompt}\n\nFile path: {windows_to_wsl_path(file_path)}"
        else:
            full_prompt = prompt

        from config.settings import settings
        # Sin --bare a propósito: bare mode ignora las credenciales OAuth y
        # exigiría ANTHROPIC_API_KEY, o sea pasar de la suscripción a pago por
        # token. Esto se factura contra la suscripción.
        cmd = [
            "wsl",
            settings.claude.bin_path,
            "--output-format", "json",
            "--allowedTools", allowed_tools or settings.claude.allowed_tools,
        ]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]
        if resume_session:
            cmd += ["--resume", resume_session]
        if json_schema:
            cmd += ["--json-schema", json.dumps(json_schema)]
        cmd += ["-p", full_prompt]

        logger.debug(f"[Claude] prompt ({len(full_prompt)} chars): {full_prompt[:400]}")

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return ClaudeResult.failure(f"Claude Code no terminó en {self.TIMEOUT_SECONDS}s")
        except FileNotFoundError:
            return ClaudeResult.failure("Claude Code no está instalado o no está en el PATH")
        except Exception as e:
            return ClaudeResult.failure(f"No pude invocar Claude Code: {e}")

        return self._parse(proc)

    def _parse(self, proc: subprocess.CompletedProcess) -> ClaudeResult:
        """Interpreta la salida JSON de la CLI."""
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        if not stdout:
            detalle = stderr[:300] if stderr else f"código de salida {proc.returncode}"
            return ClaudeResult.failure(f"Claude Code no devolvió respuesta ({detalle})")

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            # No debería pasar con --output-format json, pero si la CLI escupe
            # algo suelto es mejor devolverlo que perderlo.
            logger.warning("[Claude] La salida no era JSON; se devuelve en crudo.")
            return ClaudeResult(text=stdout, ok=proc.returncode == 0)

        result = ClaudeResult(
            text=(data.get("result") or "").strip(),
            ok=not data.get("is_error", False),
            session_id=data.get("session_id") or "",
            cost_usd=float(data.get("total_cost_usd") or 0.0),
            usage=data.get("usage") or {},
            duration_ms=int(data.get("duration_ms") or 0),
            num_turns=int(data.get("num_turns") or 0),
        )

        if not result.ok:
            logger.warning(
                f"[Claude] is_error=true (subtype={data.get('subtype')}, "
                f"stop_reason={data.get('stop_reason')}): {result.text[:200]}"
            )
            if not result.text:
                result.text = f"Claude Code falló ({data.get('subtype') or 'sin detalle'})"
        if data.get("permission_denials"):
            # Con --allowedTools acotado esto es esperable; saberlo explica
            # respuestas a medias que si no parecen caprichos del modelo.
            logger.info(f"[Claude] Permisos denegados: {data['permission_denials']}")

        return result

    def run_stream(
        self,
        prompt: str,
        file_path: str | None = None,
        system_prompt: str = "",
        resume_session: str = "",
        on_text: Callable[[str], None] | None = None,
    ) -> ClaudeResult:
        """
        Igual que run_sync, pero entrega el texto según se genera.

        Es lo que hace usable la delegación por voz: con la llamada bloqueante,
        Iris se quedaba callada hasta que la tarea terminaba — hasta dos minutos
        de silencio — y solo entonces hablaba. Aquí empieza a hablar con el
        primer token y sigue mientras trabaja.
        """
        if file_path:
            try:
                full_prompt = _build_file_prompt(prompt, file_path)
            except Exception as e:
                logger.warning(f"[Claude] Error preparando el archivo adjunto: {e}")
                full_prompt = f"{prompt}\n\nFile path: {windows_to_wsl_path(file_path)}"
        else:
            full_prompt = prompt

        from config.settings import settings
        cmd = [
            "wsl",
            settings.claude.bin_path,
            # --verbose no es opcional: la CLI rechaza stream-json sin él.
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--allowedTools", settings.claude.allowed_tools,
        ]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]
        if resume_session:
            cmd += ["--resume", resume_session]
        cmd += ["-p", full_prompt]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except FileNotFoundError:
            return ClaudeResult.failure("Claude Code no está instalado o no está en el PATH")
        except Exception as e:
            return ClaudeResult.failure(f"No pude invocar Claude Code: {e}")

        # Popen no tiene timeout propio mientras se lee: hace falta un vigilante.
        expirado = threading.Event()

        def _matar():
            if proc.poll() is None:
                expirado.set()
                proc.kill()

        watchdog = threading.Timer(self.TIMEOUT_SECONDS, _matar)
        watchdog.daemon = True
        watchdog.start()

        final: dict | None = None
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                tipo = event.get("type")
                if tipo == "stream_event":
                    inner = event.get("event", {})
                    delta = inner.get("delta") or {}
                    # Solo el texto: content_block_delta también transporta los
                    # argumentos de las herramientas, que no se dicen en voz alta.
                    if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                        if on_text and (txt := delta.get("text")):
                            on_text(txt)
                elif tipo == "result":
                    final = event
        finally:
            watchdog.cancel()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

        if expirado.is_set():
            return ClaudeResult.failure(f"Claude Code no terminó en {self.TIMEOUT_SECONDS}s")
        if final is None:
            stderr = (proc.stderr.read() or "").strip() if proc.stderr else ""
            return ClaudeResult.failure(
                f"Claude Code cortó sin dar resultado ({stderr[:200] or 'sin detalle'})"
            )

        result = ClaudeResult(
            text=(final.get("result") or "").strip(),
            ok=not final.get("is_error", False),
            session_id=final.get("session_id") or "",
            cost_usd=float(final.get("total_cost_usd") or 0.0),
            usage=final.get("usage") or {},
            duration_ms=int(final.get("duration_ms") or 0),
            num_turns=int(final.get("num_turns") or 0),
        )
        if not result.ok and not result.text:
            result.text = f"Claude Code falló ({final.get('subtype') or 'sin detalle'})"
        return result

    def run_async(
        self,
        prompt: str,
        file_path: str | None,
        on_done: Callable[["ClaudeResult"], None],
    ) -> None:
        """Ejecuta Claude Code en un hilo de fondo. Llama on_done(result) al terminar."""
        def _worker():
            on_done(self.run_sync(prompt, file_path))

        threading.Thread(target=_worker, daemon=True).start()
