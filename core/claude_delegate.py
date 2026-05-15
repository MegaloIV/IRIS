"""
core/claude_delegate.py
Detección y delegación de tareas complejas a Claude Code.

Flujo:
  1. needs_delegation()  — siempre True; IntentAgent decide si delegar realmente
  2. IntentAgent.analyze() — Groq entiende el intent real y genera un prompt técnico
  3. ClaudeDelegator.run_sync() — llama a Claude Code con ese prompt optimizado
"""

import base64
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable


def needs_delegation(user_input: str) -> tuple[bool, str | None]:
    """Always delegates — IntentAgent.analyze() decides whether to actually call Claude Code."""
    return True, None


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

_IMAGE_MIME = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif":  "image/gif",
}


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
    """Append file content to the prompt.
    Images are embedded as base64 data URIs; other files use the WSL path.
    """
    ext = Path(file_path).suffix.lower()
    if ext in _IMAGE_EXTENSIONS:
        wsl_path = windows_to_wsl_path(file_path)
        mime = _IMAGE_MIME.get(ext, "image/png")
        with open(wsl_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return (
            f"{user_prompt}\n\n"
            f"[Attached image: {Path(file_path).name}]\n"
            f"data:{mime};base64,{b64}"
        )
    else:
        wsl_path = windows_to_wsl_path(file_path)
        return f"{user_prompt}\n\nFile path: {wsl_path}"


FILE_TASK_TYPES = [
    "file_creation", "file_reading", "file_search",
    "report_generation", "image_analysis", "document_analysis",
]


def _build_prompt(user_input: str, intent: dict) -> str:
    """Return the prompt to send to Claude Code, injecting PATH_ env vars only for file tasks."""
    prompt = intent["claude_prompt"]
    if intent.get("task_type") in FILE_TASK_TYPES:
        paths = {k: v for k, v in os.environ.items() if k.startswith("PATH_")}
        if paths:
            path_context = "Available paths:\n" + "\n".join(f"- {k}: {v}" for k, v in paths.items())
            prompt = f"{path_context}\n\n{prompt}"
    return prompt


# ─── Claude Code subprocess ───────────────────────────────────────────────────

class ClaudeDelegator:
    """Ejecuta Claude Code como subprocess y devuelve su salida raw."""

    TIMEOUT_SECONDS = 120

    def run_sync(self, prompt: str, file_path: str | None = None) -> str:
        """Llama a Claude Code sincrónicamente y devuelve el texto resultante."""
        if file_path:
            try:
                full_prompt = _build_file_prompt(prompt, file_path)
            except Exception as e:
                print(f"[Claude Code] Error leyendo archivo adjunto: {e}")
                full_prompt = f"{prompt}\n\nFile path: {windows_to_wsl_path(file_path)}"
        else:
            full_prompt = prompt

        print(f"[Claude Code] PROMPT ENVIADO:\n{full_prompt}")

        cmd = [
            "wsl",
            "/home/matias/.npm-global/bin/claude",
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
            print(f"[Claude Code] RETURN CODE: {result.returncode}")
            print(f"[Claude Code] STDERR: {result.stderr}")
            if result.returncode == 0 and result.stdout.strip():
                raw_response = result.stdout.strip()
            elif result.stderr.strip():
                raw_response = f"[Error de Claude Code]: {result.stderr.strip()[:500]}"
            else:
                raw_response = "[Claude Code no devolvió respuesta]"
            print(f"[Claude Code] RESPUESTA COMPLETA: '{raw_response}'")
            return raw_response
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
