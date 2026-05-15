"""
core/claude_delegate.py
Detección y delegación de tareas complejas a Claude Code.

Flujo:
  1. needs_delegation()  — detección rápida por extensiones/keywords (sin LLM)
  2. IntentAgent.analyze() — Groq entiende el intent real y genera un prompt técnico
  3. ClaudeDelegator.run_sync() — llama a Claude Code con ese prompt optimizado
"""

import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable

# ─── Detección rápida (pre-filtro sin LLM) ────────────────────────────────────

_FILE_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".pptx", ".ppt", ".odt", ".ods", ".txt", ".md",
}

_COMPLEXITY_KEYWORDS = [
    "analiza", "analizar", "análisis", "analisis",
    "resume", "resumir", "resumen",
    "extrae", "extraer", "extracción", "extraccion",
    "genera", "generar", "redacta", "redactar",
    "informe", "reporte", "documento",
    "compara", "comparar",
    "transcribe", "transcribir",
    "summarize", "analyze", "analysis", "report",
    "explica detalladamente", "explica en detalle",
]

_FILE_EXT_RE = re.compile(
    r'\b\S+(?:' + '|'.join(re.escape(e) for e in sorted(_FILE_EXTENSIONS, key=len, reverse=True)) + r')\b',
    re.IGNORECASE,
)


def needs_delegation(user_input: str) -> tuple[bool, str | None]:
    """
    Pre-filtro rápido basado en extensiones y keywords.
    Returns (should_delegate, file_path_or_None).
    Si returns True, se debe llamar a IntentAgent para confirmar y optimizar.
    """
    # 1. Buscar rutas de archivo reales en el mensaje
    for match in _FILE_EXT_RE.finditer(user_input):
        candidate = match.group(0).strip("'\"")
        p = Path(candidate)
        if p.exists():
            return True, str(p.resolve())

    # 2. Menciones de extensiones (sin ruta completa)
    lower = user_input.lower()
    for ext in _FILE_EXTENSIONS:
        if ext in lower:
            return True, None

    # 3. Keywords de tareas complejas
    for kw in _COMPLEXITY_KEYWORDS:
        if kw in lower:
            return True, None

    return False, None


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
            raw = response.content.strip().replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)

            should = bool(result.get("should_delegate", True))
            claude_prompt = (result.get("claude_prompt") or "").strip() or user_input
            file_path = result.get("file_path") or detected_file_path

            print(
                f"[IntentAgent] should_delegate={should} "
                f"task_type={result.get('task_type', '?')} "
                f"file={file_path}"
            )
            return {
                "should_delegate": should,
                "claude_prompt":   claude_prompt,
                "file_path":       file_path,
            }

        except Exception as e:
            print(f"[IntentAgent] Error — usando input raw como fallback: {e}")
            return {
                "should_delegate": True,
                "claude_prompt":   user_input,
                "file_path":       detected_file_path,
            }


# ─── Claude Code subprocess ───────────────────────────────────────────────────

class ClaudeDelegator:
    """Ejecuta Claude Code como subprocess y devuelve su salida raw."""

    TIMEOUT_SECONDS = 120

    def run_sync(self, prompt: str, file_path: str | None = None) -> str:
        """Llama a Claude Code sincrónicamente y devuelve el texto resultante."""
        full_prompt = prompt
        if file_path:
            full_prompt = f"{prompt}\n\nFile path: {file_path}"

        cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "-p", full_prompt,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.TIMEOUT_SECONDS,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            if result.stderr.strip():
                return f"[Error de Claude Code]: {result.stderr.strip()[:500]}"
            return "[Claude Code no devolvió respuesta]"
        except subprocess.TimeoutExpired:
            return f"[Claude Code: timeout después de {self.TIMEOUT_SECONDS}s]"
        except FileNotFoundError:
            return "[Claude Code no está instalado o no está en el PATH]"
        except Exception as e:
            return f"[Error al invocar Claude Code]: {e}"

    def run_async(
        self,
        prompt: str,
        file_path: str | None,
        on_done: Callable[[str], None],
    ) -> None:
        """Ejecuta Claude Code en un hilo de fondo. Llama on_done(raw) al terminar."""
        def _worker():
            raw = self.run_sync(prompt, file_path)
            on_done(raw)

        threading.Thread(target=_worker, daemon=True).start()
