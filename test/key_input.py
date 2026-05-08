from pynput import keyboard

def on_press(key):
    print(f"Tecla presionada: {key}")

def on_release(key):
    print(f"Tecla soltada: {key}")
    if key == keyboard.Key.esc:
        return False

with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
    listener.join()