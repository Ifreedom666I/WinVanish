"""
WinVanish - minimiert mehrere Ziel-Fenster (z.B. Browser + Discord + ein Spiel
wie Civ 5) auf einmal, mutet den Ton und pausiert das Video, per selbst gewaehlter
Taste. Erneutes Druecken macht alles rueckgaengig - jedes Fenster kommt wieder in
den Zustand (maximiert/normal), in dem es vorher war.

Alles wird ueber das Tray-Icon (unten rechts bei der Uhr) eingestellt:
- Taste aendern...              -> gewuenschte Taste/Kombination einfach druecken
- Ziel-Fenster hinzufuegen...    -> z.B. auf Civ 5 klicken/wechseln, wird gemerkt
- Ziel-Fenster entfernen...      -> einzelnes Ziel aus der Liste loeschen
- Alle Ziele zuruecksetzen       -> wieder "was gerade aktiv ist" verwenden
- Video pausieren (An/Aus)       -> Media-Play/Pause-Taste beim Verstecken mitsenden
- Ziele automatisch mitstarten   -> alle Ziel-Programme beim Start von WinVanish mitstarten
"""

import json
import os
import subprocess
import sys
import threading
import time
import winreg

import keyboard
import psutil
import win32con
import win32gui
import win32process
from pycaw.pycaw import AudioUtilities
from PIL import Image, ImageDraw
import pystray

WEBSITE = "https://kotsch.tech"

# ----------------------------------------------------------------------
if getattr(sys, "frozen", False):
    # Ordner der .exe -> hier lebt die config.json (portabel, neben der exe)
    BASE_DIR = os.path.dirname(sys.executable)
    # PyInstaller (--onefile) entpackt mitgelieferte Ressourcen (Icons) zur
    # Laufzeit in einen temporaeren Ordner (sys._MEIPASS). So braucht es keine
    # losen .png-Dateien neben der exe - alles steckt in der einen Datei.
    RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = BASE_DIR

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEFAULT_CONFIG = {
    "toggle": "f8",
    # Liste von Ziel-Programmen: [{"exe": "C:\\...\\civ5.exe", "title": "Civilization V"}, ...]
    "targets": [],
    "pause_media": True,     # Media-Play/Pause-Taste beim Umschalten mitsenden
    "auto_launch": False,    # Ziel-Programme beim Start von WinVanish mitstarten
}
# Wie lange nach ShowWindow(MINIMIZE) gewartet wird, bevor geprueft wird, ob ein
# (v.a. Vollbild-)Fenster wirklich weg ist. Vermeidet Race-Conditions bei Spielen.
MINIMIZE_SETTLE_SECONDS = 0.05
# ----------------------------------------------------------------------

state = {
    "active": False,
    # Liste von (hwnd, war_maximiert) fuer alle gerade minimierten Fenster
    "hidden": [],
    "hotkey_handle": None,
    "capturing": False,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception:
            loaded = {}
        cfg.update(loaded)
        # Migration vom alten Einzel-Ziel-Format (target_exe/target_title)
        if not cfg.get("targets") and loaded.get("target_exe"):
            cfg["targets"] = [{
                "exe": loaded["target_exe"],
                "title": loaded.get("target_title", ""),
            }]
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


config = load_config()
save_config(config)  # legacy Felder direkt bereinigen / neue Defaults schreiben


def get_volume_interface():
    speakers = AudioUtilities.GetSpeakers()
    return speakers.EndpointVolume


volume = get_volume_interface()


def mute(value: bool):
    volume.SetMute(1 if value else 0, None)


# ----------------------- Fenster-Helfer -----------------------

def is_window_maximized(hwnd):
    try:
        placement = win32gui.GetWindowPlacement(hwnd)
        return placement[1] == win32con.SW_SHOWMAXIMIZED
    except Exception:
        return False


def minimize_window(hwnd):
    """Minimiert ein Fenster robust - auch Vollbild-Spiele (Alt+Tab-Verhalten).
    Merkt sich vorher, ob es maximiert war, damit restore_window es korrekt
    wiederherstellen kann."""
    if not hwnd or not win32gui.IsWindow(hwnd):
        return None
    was_maximized = is_window_maximized(hwnd)
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    except Exception:
        return None
    time.sleep(MINIMIZE_SETTLE_SECONDS)
    # Manche Vollbild-/Exclusive-Fenster (aeltere DirectX-Spiele) reagieren nicht
    # auf das normale SW_MINIMIZE. Falls es noch nicht minimiert ist -> nachlegen.
    try:
        placement = win32gui.GetWindowPlacement(hwnd)
        if placement[1] != win32con.SW_SHOWMINIMIZED:
            win32gui.ShowWindow(hwnd, win32con.SW_FORCEMINIMIZE)
    except Exception:
        pass
    return (hwnd, was_maximized)


def restore_window(hidden_entry):
    hwnd, was_maximized = hidden_entry
    if not hwnd or not win32gui.IsWindow(hwnd):
        return
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWMAXIMIZED if was_maximized else win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def get_exe_of_window(hwnd):
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return psutil.Process(pid).exe()


def find_windows_for_exe(target_exe, title_sub=""):
    """Findet alle sichtbaren Top-Level-Fenster eines Programms (z.B. mehrere
    Fenster/Dialoge desselben Spiels/Browsers)."""
    target_exe = os.path.normcase(target_exe)
    title_sub = (title_sub or "").lower()
    matches = []

    def enum_handler(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
            return
        try:
            exe = get_exe_of_window(hwnd)
        except Exception:
            return
        if os.path.normcase(exe) != target_exe:
            return
        if title_sub and title_sub not in win32gui.GetWindowText(hwnd).lower():
            return
        matches.append(hwnd)

    win32gui.EnumWindows(enum_handler, None)
    return matches


def is_exe_running(target_exe):
    target_exe = os.path.normcase(target_exe)
    for p in psutil.process_iter(["exe"]):
        try:
            if p.info["exe"] and os.path.normcase(p.info["exe"]) == target_exe:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def send_media_play_pause():
    if config.get("pause_media", True):
        try:
            keyboard.send("play/pause media")
        except Exception:
            pass


# ----------------------- Kernlogik -----------------------

def toggle_panic():
    if state["capturing"]:
        return

    if not state["active"]:
        targets = config.get("targets") or []
        hidden = []
        any_hidden = False

        if targets:
            for t in targets:
                exe = t.get("exe")
                if not exe:
                    continue
                hwnds = find_windows_for_exe(exe, t.get("title", ""))
                if not hwnds:
                    # Programm laeuft nicht (oder kein sichtbares Fenster) -> starten
                    try:
                        subprocess.Popen([exe])
                    except Exception:
                        pass
                    continue
                for hwnd in hwnds:
                    entry = minimize_window(hwnd)
                    if entry:
                        hidden.append(entry)
                        any_hidden = True
        else:
            # Kein Ziel konfiguriert -> Fallback wie bisher: aktives Fenster
            hwnd = win32gui.GetForegroundWindow()
            entry = minimize_window(hwnd)
            if entry:
                hidden.append(entry)
                any_hidden = True

        if any_hidden:
            send_media_play_pause()
        mute(True)
        state["hidden"] = hidden
        state["active"] = True
    else:
        for entry in state["hidden"]:
            restore_window(entry)
        mute(False)
        if state["hidden"]:
            send_media_play_pause()
        state["active"] = False
        state["hidden"] = []


def register_hotkey(key):
    if state["hotkey_handle"] is not None:
        try:
            keyboard.remove_hotkey(state["hotkey_handle"])
        except (KeyError, ValueError):
            pass
        state["hotkey_handle"] = None
    state["hotkey_handle"] = keyboard.add_hotkey(key, toggle_panic)


# ----------------------- Tray-Icon (hell/dunkel je nach Windows-Design) -----------------------

# Wie oft geprueft wird, ob der Nutzer zwischen hellem/dunklem Windows-Design
# gewechselt hat (Einstellungen -> Personalisierung -> Farben).
THEME_POLL_SECONDS = 2

ICON_FILES = {
    "light": os.path.join(RESOURCE_DIR, "icon-light.png"),
    "dark": os.path.join(RESOURCE_DIR, "icon-dark.png"),
}


def get_windows_theme():
    """Liest 'AppsUseLightTheme' aus der Registry aus.
    1 = helles Design, 0 = dunkles Design. Bei Fehlern/fehlendem Wert (z.B.
    sehr alte Windows-Version oder frisch installiertes System) wird gemaess
    Vorgabe standardmaessig das dunkle Design angenommen."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return "light" if value == 1 else "dark"
    except Exception:
        return "dark"


def load_theme_icon_image(theme):
    path = ICON_FILES.get(theme, ICON_FILES["dark"])
    try:
        return Image.open(path).convert("RGBA")
    except Exception:
        # Fallback: das jeweils andere Design-Bild versuchen, sonst ein
        # einfach gezeichnetes Icon, damit die App auf jeden Fall startet.
        other = ICON_FILES["light"] if theme == "dark" else ICON_FILES["dark"]
        try:
            return Image.open(other).convert("RGBA")
        except Exception:
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((2, 2, 61, 61), fill=(200, 40, 40, 255))
            d.rectangle((22, 18, 30, 40), fill=(255, 255, 255, 255))
            d.rectangle((34, 18, 42, 40), fill=(255, 255, 255, 255))
            return img


def make_icon_image():
    return load_theme_icon_image(get_windows_theme())


def watch_theme_and_update_icon(icon):
    """Laeuft im Hintergrund und aktualisiert das Tray-Icon live, sobald der
    Nutzer in Windows zwischen hellem und dunklem Design wechselt."""
    current_theme = get_windows_theme()
    while True:
        time.sleep(THEME_POLL_SECONDS)
        theme = get_windows_theme()
        if theme != current_theme:
            current_theme = theme
            icon.icon = load_theme_icon_image(theme)


def current_key_label(item):
    return f"⌨  Taste: {config['toggle'].upper()}"


def current_targets_label(item):
    targets = config.get("targets") or []
    if not targets:
        return "🎯  Ziel: aktives Fenster"
    if len(targets) == 1:
        return f"🎯  Ziel: {os.path.basename(targets[0]['exe'])}"
    names = ", ".join(os.path.basename(t["exe"]) for t in targets)
    return f"🎯  Ziele ({len(targets)}): {names}"


def on_change_key(icon, item):
    if state["capturing"]:
        return
    threading.Thread(target=_capture_new_key, args=(icon,), daemon=True).start()


def _capture_new_key(icon):
    state["capturing"] = True
    try:
        icon.notify("Drücke jetzt die gewünschte Taste (auch Kombination möglich)...", "WinVanish")
        new_key = keyboard.read_hotkey(suppress=False)
        config["toggle"] = new_key
        save_config(config)
        register_hotkey(new_key)
        icon.notify(f"Neue Taste gespeichert: {new_key.upper()}", "WinVanish")
    except Exception as e:
        icon.notify(f"Fehler beim Aufnehmen der Taste: {e}", "WinVanish")
    finally:
        state["capturing"] = False
        icon.update_menu()


def on_add_target(icon, item):
    if state["capturing"]:
        return
    threading.Thread(target=_capture_add_target, args=(icon,), daemon=True).start()


def _capture_add_target(icon):
    state["capturing"] = True
    try:
        icon.notify("Wechsle jetzt innerhalb von 4 Sekunden zum gewünschten Fenster (z.B. Civ 5)...", "WinVanish")
        time.sleep(4)
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        exe = get_exe_of_window(hwnd)
        targets = config.setdefault("targets", [])
        if any(os.path.normcase(t.get("exe", "")) == os.path.normcase(exe) for t in targets):
            icon.notify(f"{os.path.basename(exe)} ist bereits in der Ziel-Liste.", "WinVanish")
        else:
            targets.append({"exe": exe, "title": title})
            save_config(config)
            icon.notify(f"Ziel hinzugefügt: {title}", "WinVanish")
    except Exception as e:
        icon.notify(f"Fehler beim Hinzufügen: {e}", "WinVanish")
    finally:
        state["capturing"] = False
        icon.update_menu()


def _make_remove_handler(exe):
    def handler(icon, item):
        config["targets"] = [t for t in config.get("targets", []) if t.get("exe") != exe]
        save_config(config)
        icon.update_menu()
        icon.notify(f"Ziel entfernt: {os.path.basename(exe)}", "WinVanish")
    return handler


def _build_remove_submenu_items():
    targets = config.get("targets") or []
    if not targets:
        return [pystray.MenuItem("(keine Ziele)", None, enabled=False)]
    return [
        pystray.MenuItem(os.path.basename(t["exe"]), _make_remove_handler(t["exe"]))
        for t in targets
    ]


def on_clear_targets(icon, item):
    config["targets"] = []
    save_config(config)
    icon.update_menu()
    icon.notify("Alle Ziele zurückgesetzt - es wird wieder das gerade aktive Fenster verwendet.", "WinVanish")


def on_toggle_pause_media(icon, item):
    config["pause_media"] = not config.get("pause_media", True)
    save_config(config)


def on_toggle_auto_launch(icon, item):
    config["auto_launch"] = not config.get("auto_launch", False)
    save_config(config)


def on_exit(icon, item):
    icon.stop()
    os._exit(0)


def on_open_website(icon, item):
    try:
        import webbrowser
        webbrowser.open(WEBSITE)
    except Exception:
        pass


def run_tray():
    menu = pystray.Menu(
        pystray.MenuItem(current_key_label, None, enabled=False),
        pystray.MenuItem(current_targets_label, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("✏️  Taste ändern...", on_change_key),
        pystray.MenuItem("➕  Ziel-Fenster hinzufügen...", on_add_target),
        pystray.MenuItem("➖  Ziel-Fenster entfernen", pystray.Menu(_build_remove_submenu_items)),
        pystray.MenuItem("↺  Alle Ziele zurücksetzen", on_clear_targets),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "⏯  Video pausieren beim Verstecken",
            on_toggle_pause_media,
            checked=lambda item: config.get("pause_media", True),
        ),
        pystray.MenuItem(
            "🚀  Ziele automatisch mitstarten",
            on_toggle_auto_launch,
            checked=lambda item: config.get("auto_launch", False),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("👻  WinVanish  ·  by Kotsch.Tech", on_open_website),
        pystray.MenuItem(f"🌐  {WEBSITE}", on_open_website),
        pystray.MenuItem("✕  Beenden", on_exit),
    )
    def setup(ic):
        ic.visible = True
        threading.Thread(target=watch_theme_and_update_icon, args=(ic,), daemon=True).start()

    icon = pystray.Icon("WinVanish", make_icon_image(), "WinVanish by Kotsch.Tech", menu)
    icon.run(setup=setup)


def main():
    register_hotkey(config["toggle"])
    if config.get("auto_launch"):
        for t in config.get("targets") or []:
            exe = t.get("exe")
            if exe and not is_exe_running(exe):
                try:
                    subprocess.Popen([exe])
                except Exception:
                    pass
    run_tray()


if __name__ == "__main__":
    main()
