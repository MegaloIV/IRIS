"""
companion/screen.py
Captura de pantalla y elementos UI del escritorio Windows.
"""

import tempfile
from pathlib import Path


def _windows_to_wsl(path: str) -> str:
    if len(path) >= 2 and path[1] == ":":
        drive = path[0].lower()
        rest = path[2:].replace("\\", "/").lstrip("/")
        return f"/mnt/{drive}/{rest}"
    return path.replace("\\", "/")


def take_screenshot() -> dict:
    import pyautogui

    temp_dir = Path(tempfile.gettempdir()) / "iris_companion"
    temp_dir.mkdir(exist_ok=True)
    win_path = str(temp_dir / "screenshot.png")

    shot = pyautogui.screenshot()
    shot.save(win_path)

    return {
        "windows_path": win_path,
        "wsl_path": _windows_to_wsl(win_path),
        "width": shot.width,
        "height": shot.height,
        "elements": get_ui_elements(),
    }


def get_ui_elements(window_title: str | None = None) -> list[dict]:
    try:
        from pywinauto import Desktop
        desktop = Desktop(backend="uia")

        if window_title:
            candidates = [
                w for w in desktop.windows()
                if window_title.lower() in w.window_text().lower()
            ]
            target = candidates[0] if candidates else desktop.active()
        else:
            target = desktop.active()

        elements = []
        for elem in target.descendants():
            try:
                name = elem.window_text().strip()
                rect = elem.rectangle()
                if not name or rect.width() <= 0 or rect.height() <= 0:
                    continue
                elements.append({
                    "name": name,
                    "type": str(elem.element_info.control_type),
                    "x": rect.left + rect.width() // 2,
                    "y": rect.top + rect.height() // 2,
                    "bounds": {
                        "left": rect.left, "top": rect.top,
                        "right": rect.right, "bottom": rect.bottom,
                    },
                })
            except Exception:
                continue

        return elements[:60]
    except Exception:
        return []
