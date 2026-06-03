"""
companion/control.py
Control de mouse, teclado y aplicaciones en Windows.
"""

import subprocess
import pyautogui

pyautogui.FAILSAFE = True   # mover el mouse a la esquina superior izquierda aborta
pyautogui.PAUSE    = 0.08


def click(x: int, y: int, button: str = "left"):
    pyautogui.click(x, y, button=button)

def double_click(x: int, y: int):
    pyautogui.doubleClick(x, y)

def right_click(x: int, y: int):
    pyautogui.rightClick(x, y)

def move(x: int, y: int):
    pyautogui.moveTo(x, y)

def drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.3):
    pyautogui.dragTo(x2, y2, duration=duration, button="left")

def type_text(text: str, interval: float = 0.04):
    pyautogui.write(text, interval=interval)

def press_key(key: str):
    pyautogui.press(key)

def hotkey(*keys: str):
    pyautogui.hotkey(*keys)

def scroll(x: int, y: int, amount: int = 3, direction: str = "down"):
    pyautogui.moveTo(x, y)
    clicks = amount if direction == "up" else -amount
    pyautogui.scroll(clicks)

def launch_app(app: str):
    """Abre una aplicación por nombre o ruta. Busca primero en UWP/Store, luego clásico."""
    ps = f"""
$name = '{app}'
$uwp = Get-StartApps | Where-Object {{$_.Name -like "*$name*"}} | Select-Object -First 1
if ($uwp) {{
    Start-Process "shell:AppsFolder\\$($uwp.AppID)"
}} else {{
    Start-Process $name
}}
"""
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
    )

def get_installed_apps() -> list[str]:
    """Lista apps instaladas incluyendo UWP/Store."""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "Get-StartApps | Select-Object -ExpandProperty Name | Sort-Object"],
        capture_output=True, text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def get_windows() -> list[dict]:
    try:
        from pywinauto import Desktop
        result = []
        for w in Desktop(backend="uia").windows():
            try:
                title = w.window_text().strip()
                rect  = w.rectangle()
                if title:
                    result.append({
                        "title":  title,
                        "x":      rect.left,
                        "y":      rect.top,
                        "width":  rect.width(),
                        "height": rect.height(),
                    })
            except Exception:
                continue
        return result
    except Exception:
        return []
