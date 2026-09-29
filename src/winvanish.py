"""
WinVanish - blendet mehrere Ziel-Fenster (z.B. Browser + Discord + ein Spiel wie
Civ 5) auf einmal komplett aus - nicht nur minimiert, sondern auch aus der
Taskleiste entfernt - mutet den Ton und pausiert das Video, per selbst gewaehlter
Taste. Erneutes Druecken macht alles rueckgaengig - jedes Fenster kommt wieder in
den Zustand (maximiert/normal), in dem es vorher war. Der tatsaechliche Fenster-
zustand wird bei jedem Tastendruck neu geprueft, damit nichts "haengen" bleibt.

Alles wird ueber das Tray-Icon (unten rechts bei der Uhr) eingestellt:
- Taste aendern...              -> gewuenschte Taste/Kombination einfach druecken
- Ziel-Fenster hinzufuegen...    -> z.B. auf Civ 5 klicken/wechseln, wird gemerkt
- Aus offenen Fenstern waehlen.. -> Liste aller offenen Fenster, Mehrfachauswahl per Strg/Shift-Klick
- Ziel-Fenster entfernen...      -> einzelnes Ziel aus der Liste loeschen
- Alle Ziele zuruecksetzen       -> wieder "was gerade aktiv ist" verwenden
- Video pausieren (An/Aus)       -> Media-Play/Pause-Taste beim Verstecken mitsenden
- Ziele automatisch mitstarten   -> alle Ziel-Programme beim Start von WinVanish mitstarten
"""

import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
import traceback
import winreg

import keyboard
import psutil
import win32api
import win32con
import win32event
import winerror
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
LOG_PATH = os.path.join(BASE_DIR, "winvanish.log")

# ----------------------- Log-Datei -----------------------
# Damit sich "manchmal geht's nicht"-Faelle nachvollziehen lassen: jeder
# Tastendruck, jede gefundene/fehlende Ziel-Fenster-Suche und jeder Fehler
# landet hier (max. 1 MB, 2 alte Dateien als Backup - waechst nicht unbegrenzt).
log = logging.getLogger("winvanish")
log.setLevel(logging.DEBUG)
_log_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
)
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
log.addHandler(_log_handler)


def log_unhandled_exception(exc_type, exc_value, exc_tb):
    log.error(
        "UNBEHANDELTER FEHLER - Programm haette abstuerzen koennen:\n%s",
        "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
    )


sys.excepthook = log_unhandled_exception


def log_unhandled_thread_exception(args):
    # Fehler in Hintergrund-Threads (Taste aendern, Ziel hinzufuegen, Theme-
    # Ueberwachung, ...) landen NICHT in sys.excepthook - dafuer extra.
    log.error(
        "UNBEHANDELTER FEHLER in Thread '%s':\n%s",
        args.thread.name if args.thread else "?",
        "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
    )


threading.excepthook = log_unhandled_thread_exception

log.info("=" * 60)
log.info("WinVanish gestartet (BASE_DIR=%s)", BASE_DIR)
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
    "paused_media": [],   # Mediaplayer, die WinVanish selbst pausiert hat (fuers Fortsetzen)
    "capturing": False,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception as e:
            log.error("load_config: config.json konnte nicht gelesen werden, nutze Standardwerte: %s", e)
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


def _is_stuck_offscreen(hwnd):
    """Windows verschiebt minimierte Fenster intern auf (-32000, -32000).
    Bleibt ein Fenster nach dem Wiederherstellen dort haengen (v.a. bei
    DX9-Vollbild-Spielen im Exclusive-Modus passiert das gelegentlich),
    ist es fuer den Nutzer trotz 'erfolgreichem' ShowWindow unsichtbar."""
    try:
        left, top, _, _ = win32gui.GetWindowRect(hwnd)
        return left <= -30000 and top <= -30000
    except Exception:
        return False


def minimize_window(hwnd):
    """Minimiert ein Fenster robust - auch Vollbild-Spiele (Alt+Tab-Verhalten).
    Merkt sich vorher, ob es maximiert war, damit restore_window es korrekt
    wiederherstellen kann.

    Der Taskleisten-Button wird NUR bei Fenstern entfernt, die normal auf
    SW_MINIMIZE reagieren. Fenster, die dafuer SW_FORCEMINIMIZE brauchten
    (typisch fuer altes DirectX-9-Exclusive-Vollbild, z.B. Civ 5), werden
    bewusst NICHT per Fenster-Stil manipuliert: dieser Trick kann bei
    solchen Spielen dazu fuehren, dass sie in einem kaputten Zustand haengen
    bleiben (auf (-32000,-32000) "minimiert", aber weder normal
    minimiert noch wiederherstellbar) - lieber zuverlaessig nur minimiert
    als kaputt."""
    if not hwnd or not win32gui.IsWindow(hwnd):
        log.warning("minimize_window: hwnd %s ist ungueltig/existiert nicht mehr", hwnd)
        return None
    title = win32gui.GetWindowText(hwnd)
    rect, was_maximized = _capture_geometry(hwnd)
    try:
        original_exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    except Exception as e:
        original_exstyle = None
        log.warning("minimize_window: GetWindowLong fehlgeschlagen fuer '%s': %s", title, e)
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    except Exception as e:
        log.error("minimize_window: SW_MINIMIZE fehlgeschlagen fuer '%s': %s", title, e)
        return None
    time.sleep(MINIMIZE_SETTLE_SECONDS)
    # Manche Vollbild-/Exclusive-Fenster (aeltere DirectX-Spiele) reagieren nicht
    # auf das normale SW_MINIMIZE. Falls es noch nicht minimiert ist -> nachlegen.
    needed_force_minimize = False
    try:
        placement = win32gui.GetWindowPlacement(hwnd)
        if placement[1] != win32con.SW_SHOWMINIMIZED:
            needed_force_minimize = True
            log.info("minimize_window: '%s' reagiert nicht auf SW_MINIMIZE, versuche SW_FORCEMINIMIZE", title)
            win32gui.ShowWindow(hwnd, win32con.SW_FORCEMINIMIZE)
    except Exception as e:
        log.warning("minimize_window: Force-Minimize-Check fehlgeschlagen fuer '%s': %s", title, e)

    # Taskleisten-Button entfernen: WS_EX_TOOLWINDOW setzen / WS_EX_APPWINDOW
    # entfernen. Nur bei "normalen" Fenstern - siehe Docstring oben.
    # Windows aktualisiert die Taskleiste dafuer nur zuverlaessig, wenn das
    # Fenster kurz komplett versteckt und danach neu gezeigt wird.
    # Nur bei normalen Fenstern mit Titelleiste (Browser, Editoren, ...).
    # Spiele (randlos/Vollbild, ohne WS_CAPTION) bleiben beim reinen, sicheren
    # Minimieren - der Trick machte z.B. Fallout 4 nach dem Verstecken
    # unauffindbar/unsichtbar.
    try:
        has_caption = (win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE) & win32con.WS_CAPTION) == win32con.WS_CAPTION
    except Exception:
        has_caption = False
    if not has_caption and not needed_force_minimize:
        log.info("minimize_window: '%s' hat keine Titelleiste (Spiel/Vollbild) - Taskleisten-Trick uebersprungen", title)
        original_exstyle = None
    elif original_exstyle is not None and not needed_force_minimize:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            new_exstyle = (original_exstyle | win32con.WS_EX_TOOLWINDOW) & ~win32con.WS_EX_APPWINDOW
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, new_exstyle)
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWMINNOACTIVE)
        except Exception as e:
            log.warning("minimize_window: Taskleisten-Button verstecken fehlgeschlagen fuer '%s': %s", title, e)
            original_exstyle = None  # restore_window soll den Stil dann nicht anfassen
    elif needed_force_minimize:
        original_exstyle = None  # nichts am Stil veraendert -> restore_window soll ihn in Ruhe lassen
        log.info("minimize_window: '%s' ist ein Vollbild-Exclusive-Fenster - Taskleisten-Trick uebersprungen", title)

    log.info("minimize_window: '%s' (hwnd %s) versteckt (war_maximiert=%s, taskleiste_versteckt=%s)",
              title, hwnd, was_maximized, original_exstyle is not None)
    return (hwnd, was_maximized, original_exstyle, rect)


def _capture_geometry(hwnd):
    """Position und Maximiert-Zustand VOR dem Verstecken. Bei einem schon
    minimierten Fenster kommt beides aus GetWindowPlacement (dessen
    Normal-Rechteck), da GetWindowRect dann nur (-32000,-32000) liefert."""
    try:
        flags, show_cmd, _, _, normal_rect = win32gui.GetWindowPlacement(hwnd)
        if win32gui.IsIconic(hwnd):
            return tuple(normal_rect), bool(flags & win32con.WPF_RESTORETOMAXIMIZED)
        return win32gui.GetWindowRect(hwnd), show_cmd == win32con.SW_SHOWMAXIMIZED
    except Exception:
        return None, False


def restore_window(hidden_entry):
    hwnd, was_maximized, original_exstyle, rect = hidden_entry
    if not hwnd or not win32gui.IsWindow(hwnd):
        log.warning("restore_window: hwnd %s existiert nicht mehr (Programm evtl. beendet) - ueberspringe", hwnd)
        return
    title = win32gui.GetWindowText(hwnd)

    # original_exstyle ist NUR bei "normalen" Fenstern gesetzt (mit Titelleiste,
    # die den Taskleisten-Trick bekommen haben - siehe minimize_window).
    # Captionless-Fenster (Spiele/Vollbild) werden bewusst NUR mit einem
    # einzelnen, einfachen ShowWindow(SW_RESTORE) behandelt: mehrere
    # ShowWindow/SetWindowPos-Aufrufe hintereinander koennen ein DX9-
    # Exclusive-Vollbild-Fenster destabilisieren - ein realer Fall dabei war
    # sogar ein kompletter Absturz des Spiels. Lieber ein Fenster, das an der
    # falschen Position/dem falschen Monitor landet, als eins, das abstuerzt.
    if original_exstyle is None:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception as e:
            log.warning("restore_window: SetForegroundWindow fehlgeschlagen fuer '%s': %s", title, e)
        log.info("restore_window: '%s' (hwnd %s) wiederhergestellt (Spiel/Vollbild - minimale Behandlung)", title, hwnd)
        return

    # Erst den urspruenglichen Taskleisten-Stil zuruecksetzen (Fenster bleibt
    # dabei unsichtbar/minimiert), dann normal wiederherstellen - sonst taucht
    # der Taskleisten-Button teils verzoegert oder gar nicht wieder auf.
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, original_exstyle)
    except Exception as e:
        log.warning("restore_window: Taskleisten-Stil zuruecksetzen fehlgeschlagen fuer '%s': %s", title, e)
    # Zuerst normal wiederherstellen, dann exakt an die gemerkte Position
    # (und damit auf den gemerkten MONITOR) schieben. Nur SW_RESTORE zu
    # nutzen setzt das Fenster dorthin, wo Windows es zuletzt "normal"
    # platziert hatte - bei Mehrmonitor-Setups oft der falsche Bildschirm.
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    if rect and rect[2] - rect[0] > 0 and rect[3] - rect[1] > 0 and rect[0] > -30000:
        try:
            win32gui.SetWindowPos(
                hwnd, 0, rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1],
                win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
            )
        except Exception as e:
            log.warning("restore_window: Position zuruecksetzen fehlgeschlagen fuer '%s': %s", title, e)
    if was_maximized:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWMAXIMIZED)
    try:
        # Windows verweigert SetForegroundWindow, wenn der Aufruf nicht von
        # einer Eingabe stammt ("Zugriff verweigert"). Ein kurzer Alt-Tastendruck
        # zaehlt als Eingabe und hebt die Sperre auf.
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        log.warning("restore_window: SetForegroundWindow fehlgeschlagen fuer '%s': %s", title, e)

    # Selbstheilung nur fuer diese normalen Fenster (bei denen mehrere
    # ShowWindow-Aufrufe erwiesenermassen unproblematisch sind): manche
    # bleiben trotz "erfolgreichem" ShowWindow auf der internen
    # Minimiert-Position (-32000,-32000) haengen. Falls das passiert, aktiv
    # auf den sichtbaren Bereich zurueckschieben.
    time.sleep(0.15)
    if _is_stuck_offscreen(hwnd):
        log.warning("restore_window: '%s' haengt auf (-32000,-32000) fest - erzwinge Position zurueck", title)
        try:
            monitor = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd))
            l, t, r, b = monitor["Monitor"]
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, l, t, r - l, b - t, win32con.SWP_SHOWWINDOW)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            log.exception("restore_window: Zwangs-Reposition fuer '%s' fehlgeschlagen", title)

    log.info("restore_window: '%s' (hwnd %s) wiederhergestellt", title, hwnd)


def get_exe_of_window(hwnd):
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return psutil.Process(pid).exe()


def find_windows_for_exe(target_exe):
    """Findet alle sichtbaren Top-Level-Fenster eines Programms (z.B. mehrere
    Fenster/Dialoge desselben Spiels/Browsers) - reine Zuordnung ueber die
    exe, OHNE Filterung nach Fenstertitel. Der gespeicherte 'title' in der
    config.json ist nur ein Anzeige-Hinweis vom Moment des Hinzufuegens
    (siehe DEFAULT_CONFIG) und darf NICHT als Suchfilter verwendet werden:
    bei Browsern, IDEs, Mediaplayern etc. aendert sich der Fenstertitel
    staendig (anderer Tab, andere Datei, ...). Wuerde man danach filtern,
    faende WinVanish das laengst offene Fenster fast nie mehr wieder und
    wuerde stattdessen woanders staendig ein NEUES Fenster/Programm starten -
    genau das war der 'oeffnet ein neues Chrome-Fenster'-Bug."""
    target_exe = os.path.normcase(target_exe)
    matches = []

    def enum_handler(hwnd, _):
        if not win32gui.GetWindowText(hwnd):
            return
        # Auch minimierte Fenster zaehlen, selbst wenn Windows sie nicht mehr
        # als "sichtbar" meldet: Spiele wie Fallout 4 liefern nach dem
        # Verstecken IsWindowVisible()==False, waeren so nie wieder
        # auffindbar und blieben fuer immer weg.
        if not (win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd)):
            return
        if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
            return
        try:
            exe = get_exe_of_window(hwnd)
        except Exception:
            return
        if os.path.normcase(exe) != target_exe:
            return
        matches.append(hwnd)

    win32gui.EnumWindows(enum_handler, None)
    if not matches:
        log.debug("find_windows_for_exe: kein sichtbares Fenster fuer '%s'", target_exe)
    return matches


def list_open_windows():
    """Listet alle aktuell offenen, "echten" Fenster (wie sie auch in der
    Taskleiste/beim Alt+Tab auftauchen) mit Titel und zugehoeriger exe auf -
    Basis fuer die Mehrfachauswahl im Tray-Menue."""
    own_pid = os.getpid()
    results = []

    def enum_handler(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        # Tool-Fenster (z.B. Flyouts, unsichtbare Hilfsfenster) ausblenden,
        # damit nur "richtige" Programme in der Liste auftauchen.
        exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if exstyle & win32con.WS_EX_TOOLWINDOW:
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == own_pid:
                return
            exe = get_exe_of_window(hwnd)
        except Exception:
            return
        results.append({"hwnd": hwnd, "title": title, "exe": exe})

    win32gui.EnumWindows(enum_handler, None)
    return results


def is_exe_running(target_exe):
    target_exe = os.path.normcase(target_exe)
    for p in psutil.process_iter(["exe"]):
        try:
            if p.info["exe"] and os.path.normcase(p.info["exe"]) == target_exe:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


try:
    import asyncio
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as _PlaybackStatus,
    )
    HAVE_MEDIA_SESSIONS = True
except Exception:
    HAVE_MEDIA_SESSIONS = False


async def _pause_playing_sessions_async():
    manager = await _MediaManager.request_async()
    paused = []
    for session in manager.get_sessions():
        app_id = session.source_app_user_model_id
        status = _PlaybackStatus(session.get_playback_info().playback_status)
        if status == _PlaybackStatus.PLAYING:
            ok = await session.try_pause_async()
            log.info("pause_media: '%s' pausiert (ok=%s)", app_id, ok)
            if ok:
                paused.append(app_id)
        else:
            log.debug("pause_media: '%s' laeuft nicht (%s) - nichts zu pausieren", app_id, status.name)
    return paused


async def _resume_sessions_async(app_ids):
    manager = await _MediaManager.request_async()
    for session in manager.get_sessions():
        app_id = session.source_app_user_model_id
        if app_id not in app_ids:
            continue
        status = _PlaybackStatus(session.get_playback_info().playback_status)
        if status == _PlaybackStatus.PAUSED:
            ok = await session.try_play_async()
            log.info("resume_media: '%s' fortgesetzt (ok=%s)", app_id, ok)
        else:
            log.info("resume_media: '%s' ist nicht mehr pausiert (%s) - nichts zu tun", app_id, status.name)


def pause_media():
    """Pausiert gezielt nur, was gerade WIRKLICH spielt (Chrome/YouTube,
    Spotify, ...) und merkt sich, was pausiert wurde. Die globale
    Play/Pause-Medientaste ist dafuer ungeeignet: sie ist ein blindes
    Umschalten (startet ein bereits pausiertes Video sogar) und landet bei
    mehreren Mediaplayern (z.B. zusaetzlich Apple Music) leicht beim
    falschen."""
    already_paused = list(state.get("paused_media") or [])
    if not config.get("pause_media", True):
        return
    if not HAVE_MEDIA_SESSIONS:
        log.warning("pause_media: Windows-Mediasessions nicht verfuegbar - nutze Medientaste als Notloesung")
        try:
            keyboard.send("play/pause media")
        except Exception:
            log.exception("pause_media: Medientaste fehlgeschlagen")
        return
    try:
        newly_paused = asyncio.run(_pause_playing_sessions_async())
        # Bei erneutem Verstecken (gemischter Zustand) nicht vergessen, was
        # schon vorher von WinVanish pausiert wurde.
        state["paused_media"] = already_paused + [a for a in newly_paused if a not in already_paused]
        _verify_paused_or_fallback(newly_paused)
    except Exception:
        log.exception("pause_media: Pausieren fehlgeschlagen")


def _audio_peak_for_apps(app_ids, seconds=0.6):
    """Hoechster Audio-Pegel (0..1) der Audio-Sessions, deren Prozessname zu
    den angegebenen Mediaplayern passt (z.B. 'Chrome' -> chrome.exe)."""
    from pycaw.pycaw import IAudioMeterInformation
    needles = [a.lower().replace(".exe", "") for a in app_ids]
    best = 0.0
    end = time.time() + seconds
    while time.time() < end:
        for s in AudioUtilities.GetAllSessions():
            try:
                if not s.Process:
                    continue
                name = s.Process.name().lower()
                if any(n in name or name.replace(".exe", "") in n for n in needles):
                    best = max(best, s._ctl.QueryInterface(IAudioMeterInformation).GetPeakValue())
            except Exception:
                continue
        time.sleep(0.05)
    return best


def _verify_paused_or_fallback(app_ids):
    """try_pause_async() meldet 'ok', auch wenn der Player die Pause
    ignoriert. Deshalb wird nachgemessen: ist der Player nach kurzer Zeit
    immer noch hoerbar, wird die Medientaste als zweiter Versuch gesendet
    und das Ergebnis protokolliert (damit man im Log sieht, was wirklich
    passiert ist)."""
    if not app_ids:
        return
    time.sleep(0.4)
    peak = _audio_peak_for_apps(app_ids)
    if peak <= 0.02:
        log.info("pause_media: Pause bestaetigt (Pegel %.3f)", peak)
        return
    log.warning("pause_media: %s meldet 'pausiert', ist aber noch hoerbar (Pegel %.3f) - sende Medientaste", app_ids, peak)
    try:
        keyboard.send("play/pause media")
    except Exception:
        log.exception("pause_media: Medientaste fehlgeschlagen")
        return
    time.sleep(0.4)
    peak2 = _audio_peak_for_apps(app_ids)
    if peak2 <= 0.02:
        state["paused_via_key"] = True
        log.info("pause_media: Medientaste hat geholfen (Pegel %.3f)", peak2)
    else:
        log.warning("pause_media: auch die Medientaste hat nichts bewirkt (Pegel %.3f) - Ton wird nur stummgeschaltet", peak2)


def resume_media():
    """Setzt nur fort, was WinVanish selbst pausiert hat."""
    app_ids = state.get("paused_media") or []
    via_key = state.get("paused_via_key", False)
    state["paused_media"] = []
    state["paused_via_key"] = False
    if not app_ids or not HAVE_MEDIA_SESSIONS or not config.get("pause_media", True):
        return
    try:
        asyncio.run(_resume_sessions_async(app_ids))
        if via_key:
            time.sleep(0.4)
            if _audio_peak_for_apps(app_ids) <= 0.02:
                log.info("resume_media: per Medientaste pausiert - sende sie erneut zum Fortsetzen")
                keyboard.send("play/pause media")
    except Exception:
        log.exception("resume_media: Fortsetzen fehlgeschlagen")


# ----------------------- Kernlogik -----------------------

def _current_target_hwnds():
    """Sucht die aktuellen Fenster-Handles aller konfigurierten Ziele frisch
    (nicht aus einem alten Zwischenspeicher). Ohne konfigurierte Ziele:
    aktuell aktives Fenster als Fallback.

    Ziele ohne offenes Fenster werden bewusst NUR uebersprungen, niemals
    gestartet: ein Tastendruck darf nichts oeffnen. Sonst wuerde z.B. ein
    gerade geschlossenes Civ 5 bei jedem Druck Steam und das Spiel neu
    starten, nur weil Chrome umgeschaltet werden sollte. Programme beim
    WinVanish-Start mitzustarten ist Sache der Option 'auto_launch' (main())."""
    targets = config.get("targets") or []
    hwnds = []
    if targets:
        for t in targets:
            exe = t.get("exe")
            if not exe:
                continue
            found = find_windows_for_exe(exe)
            if not found:
                log.info("_current_target_hwnds: kein Fenster fuer '%s' - uebersprungen (wird nicht gestartet)", exe)
                continue
            hwnds.extend(found)
    else:
        hwnd = win32gui.GetForegroundWindow()
        if hwnd:
            hwnds.append(hwnd)
    return hwnds


def toggle_panic():
    """Wird direkt vom keyboard-Hook aufgerufen, sobald die Taste gedrueckt
    wird. WICHTIG: der komplette Ablauf steckt in einem try/except - eine
    unbehandelte Exception hier wuerde sonst den Hotkey-Callback im
    'keyboard'-Modul lautlos sterben lassen (die Taste 'geht dann einfach
    nicht mehr', ohne jede sichtbare Fehlermeldung, da die App --noconsole
    laeuft). So landet jeder Fehler stattdessen im Log."""
    if state["capturing"]:
        log.debug("toggle_panic: ignoriert - gerade wird eine Taste/ein Fenster erfasst (capturing=True)")
        return

    try:
        hwnds = _current_target_hwnds()
        if not hwnds:
            log.warning("toggle_panic: keine Ziel-Fenster gefunden - nichts zu tun")
            return

        # Wichtig: der ECHTE aktuelle Zustand jedes Ziel-Fensters entscheidet -
        # nicht eine intern gemerkte Flag. Wurde z.B. ein Fenster zwischendurch
        # von Hand wiederhergestellt, waeren sonst Chrome und Civ 5 nicht mehr
        # synchron (eins bleibt offen, das andere geht zu). Nur wenn WIRKLICH
        # alle Ziel-Fenster gerade minimiert sind, wird wiederhergestellt -
        # in jedem anderen Fall (auch bei gemischtem Zustand) werden IMMER
        # alle auf einmal minimiert, bis sie wieder synchron sind.
        all_minimized = all(
            win32gui.IsIconic(hwnd) for hwnd in hwnds if win32gui.IsWindow(hwnd)
        )
        log.info(
            "toggle_panic: ausgeloest - %d Ziel-Fenster gefunden, alle_minimiert=%s",
            len(hwnds), all_minimized,
        )

        if all_minimized:
            # Nicht blind nur state["hidden"] durchgehen: wurde WinVanish
            # zwischenzeitlich neu gestartet (Absturz, Update, manuelles
            # Neustarten), waere dieser Zwischenspeicher leer, obwohl die
            # Fenster noch real minimiert/versteckt sind - sie blieben dann
            # fuer immer "haengen". Stattdessen ueber die aktuell gefundenen
            # Ziel-Fenster gehen und pro Fenster den gemerkten Eintrag nutzen,
            # falls vorhanden, sonst bestmoeglich (nur) wiederherstellen.
            remembered = {entry[0]: entry for entry in state["hidden"]}
            for hwnd in hwnds:
                entry = remembered.get(hwnd)
                if entry:
                    restore_window(entry)
                elif win32gui.IsWindow(hwnd) and win32gui.IsIconic(hwnd):
                    log.warning(
                        "toggle_panic: hwnd %s ohne gemerkten Zustand (z.B. nach WinVanish-Neustart) "
                        "- stelle bestmoeglich wieder her", hwnd,
                    )
                    rect, was_max = _capture_geometry(hwnd)
                    restore_window((hwnd, was_max, None, rect))
            mute(False)
            resume_media()
            state["hidden"] = []
            state["active"] = False
        else:
            hidden = []
            previous = {e[0]: e for e in state["hidden"]}
            for hwnd in hwnds:
                # Gemischter Zustand: ein schon von uns verstecktes Fenster nicht
                # erneut anfassen, sonst ginge die gemerkte Ursprungsposition verloren.
                if hwnd in previous and win32gui.IsWindow(hwnd) and win32gui.IsIconic(hwnd):
                    hidden.append(previous[hwnd])
                    continue
                entry = minimize_window(hwnd)
                if entry:
                    hidden.append(entry)
            if hidden:
                pause_media()
            mute(True)
            state["hidden"] = hidden
            state["active"] = True
            if len(hidden) < len(hwnds):
                log.warning(
                    "toggle_panic: nur %d von %d Ziel-Fenstern konnten versteckt werden",
                    len(hidden), len(hwnds),
                )
    except Exception:
        log.exception("toggle_panic: unerwarteter Fehler beim Umschalten")


_toggle_lock = threading.Lock()
_last_toggle_end = 0.0


def on_hotkey():
    """Hotkey-Callback. Laeuft im Tastatur-Hook-Thread und darf deshalb NICHT
    selbst arbeiten: toggle_panic() sendet Tastendruecke (Alt, Medientaste)
    und wartet auf Fenster - im Hook-Thread blockiert das den Hook, Windows
    verzoegert die ganze Tastatur und wirft den Hook nach einem Timeout sogar
    lautlos raus (Taste 'geht dann nicht mehr'). Darum: eigener Thread."""
    threading.Thread(target=_toggle_worker, daemon=True).start()


def _toggle_worker():
    global _last_toggle_end
    if not _toggle_lock.acquire(blocking=False):
        log.info("Tastendruck ignoriert - der vorherige Vorgang laeuft noch")
        return
    try:
        if time.time() - _last_toggle_end < 0.5:
            log.info("Tastendruck ignoriert - zu kurz nach dem letzten Umschalten")
            return
        toggle_panic()
    finally:
        _last_toggle_end = time.time()
        _toggle_lock.release()


def register_hotkey(key):
    if state["hotkey_handle"] is not None:
        try:
            keyboard.remove_hotkey(state["hotkey_handle"])
        except (KeyError, ValueError):
            pass
        state["hotkey_handle"] = None
    try:
        # trigger_on_release: genau EIN Ausloesen pro Tastendruck, auch wenn die
        # Taste etwas laenger gehalten wird (Tasten-Wiederholung wuerde sonst
        # dutzende Male hintereinander umschalten = Flackern).
        state["hotkey_handle"] = keyboard.add_hotkey(key, on_hotkey, trigger_on_release=True)
        log.info("register_hotkey: Taste '%s' erfolgreich registriert", key)
    except Exception:
        log.exception("register_hotkey: Registrieren der Taste '%s' fehlgeschlagen", key)


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


def _is_window_target(exe):
    norm = os.path.normcase(exe)
    return any(os.path.normcase(t.get("exe", "")) == norm for t in config.get("targets", []))


def _make_toggle_window_handler(exe, title):
    """Checkbox-Klick im Untermenue: Fenster/Programm als Ziel an- oder
    abhaken - direkt im Tray-Menue, ohne separates Fenster."""
    def handler(icon, item):
        targets = config.setdefault("targets", [])
        norm = os.path.normcase(exe)
        if any(os.path.normcase(t.get("exe", "")) == norm for t in targets):
            config["targets"] = [t for t in targets if os.path.normcase(t.get("exe", "")) != norm]
            save_config(config)
            icon.notify(f"Ziel entfernt: {os.path.basename(exe)}", "WinVanish")
        else:
            targets.append({"exe": exe, "title": title})
            save_config(config)
            icon.notify(f"Ziel hinzugefügt: {title}", "WinVanish")
        icon.update_menu()
    return handler


def _build_open_windows_submenu_items():
    """Baut das Untermenue 'Aus offenen Fenstern waehlen' - jede Zeile ist
    eine anhakbare Checkbox fuer ein aktuell offenes Fenster/Programm.
    Mehrere Haekchen gleichzeitig setzen = Mehrfachauswahl, ganz ohne
    Klicken/Wechseln zum Zielfenster und ohne separates Popup-Fenster."""
    windows = list_open_windows()
    # Pro Programm (exe) nur einen Eintrag zeigen, auch wenn es mehrere
    # Fenster hat - unser Ziel-Modell arbeitet ohnehin pro exe.
    by_exe = {}
    for w in windows:
        key = os.path.normcase(w["exe"])
        if key not in by_exe:
            by_exe[key] = w
    entries = sorted(by_exe.values(), key=lambda w: w["title"].lower())

    if not entries:
        return [pystray.MenuItem("(keine offenen Fenster gefunden)", None, enabled=False)]

    items = []
    for w in entries:
        base = os.path.basename(w["exe"])
        title_short = w["title"] if len(w["title"]) <= 42 else w["title"][:39] + "..."
        label = f"{title_short}  —  {base}"
        items.append(pystray.MenuItem(
            label,
            _make_toggle_window_handler(w["exe"], w["title"]),
            checked=lambda item, exe=w["exe"]: _is_window_target(exe),
        ))
    return items


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


def on_open_log(icon, item):
    """Oeffnet die Log-Datei im Standard-Texteditor - fuer den Fall, dass die
    Taste mal 'einfach nicht geht': hier steht genau, was WinVanish bei jedem
    Tastendruck gefunden/versucht/verworfen hat."""
    try:
        _log_handler.flush()
        if os.path.exists(LOG_PATH):
            os.startfile(LOG_PATH)
        else:
            icon.notify("Noch keine Log-Datei vorhanden.", "WinVanish")
    except Exception as e:
        log.exception("on_open_log: Log-Datei konnte nicht geoeffnet werden")
        icon.notify(f"Log konnte nicht geöffnet werden: {e}", "WinVanish")


def run_tray():
    menu = pystray.Menu(
        pystray.MenuItem(current_key_label, None, enabled=False),
        pystray.MenuItem(current_targets_label, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("✏️  Taste ändern...", on_change_key),
        pystray.MenuItem("➕  Ziel-Fenster hinzufügen...", on_add_target),
        pystray.MenuItem("📋  Aus offenen Fenstern wählen", pystray.Menu(_build_open_windows_submenu_items)),
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
        pystray.MenuItem("📄  Log-Datei öffnen (falls mal was nicht geht)", on_open_log),
        pystray.MenuItem("👻  WinVanish  ·  by Kotsch.Tech", on_open_website),
        pystray.MenuItem(f"🌐  {WEBSITE}", on_open_website),
        pystray.MenuItem("✕  Beenden", on_exit),
    )
    def setup(ic):
        ic.visible = True
        threading.Thread(target=watch_theme_and_update_icon, args=(ic,), daemon=True).start()

    icon = pystray.Icon("WinVanish", make_icon_image(), "WinVanish by Kotsch.Tech", menu)
    icon.run(setup=setup)


_instance_mutex = None


def ensure_single_instance():
    """Nur EINE WinVanish-Instanz gleichzeitig. Laufen zwei (z.B. Autostart
    plus manueller Start), reagiert jede auf dieselbe Taste: die eine
    versteckt, die andere sieht "alles minimiert" und holt es sofort wieder
    zurueck - Flackern, Verzoegerungen und Fenster an falscher Stelle. Eine
    zweite Instanz beendet sich deshalb sofort."""
    global _instance_mutex
    _instance_mutex = win32event.CreateMutex(None, False, "Local\\KotschTech.WinVanish.SingleInstance")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        log.warning("Eine andere WinVanish-Instanz laeuft bereits - beende diese zweite Instanz")
        sys.exit(0)


def main():
    ensure_single_instance()
    try:
        register_hotkey(config["toggle"])
        if config.get("auto_launch"):
            for t in config.get("targets") or []:
                exe = t.get("exe")
                if exe and not is_exe_running(exe):
                    try:
                        subprocess.Popen([exe])
                    except Exception as e:
                        log.error("main: Auto-Start von '%s' fehlgeschlagen: %s", exe, e)
        run_tray()
    except Exception:
        log.exception("main: WinVanish ist unerwartet abgestuerzt")
        raise
    finally:
        log.info("WinVanish beendet")


if __name__ == "__main__":
    main()
