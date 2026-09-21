"""Concrete, user-directed Windows desktop operations."""

import os
import shutil
import time
from pathlib import Path
from typing import Any

import psutil
import pyautogui
import pygetwindow as window_api

from config import WORKSPACE_DIR


class DesktopController:
    """Performs one explicit desktop action at a time."""

    # Terminating any of these can crash or brick the Windows session, so the
    # controller refuses them even if the model asks by name.
    _CRITICAL_PROCESSES = frozenset(
        {
            "csrss.exe",
            "dwm.exe",
            "explorer.exe",
            "lsass.exe",
            "services.exe",
            "smss.exe",
            "svchost.exe",
            "system",
            "system idle process",
            "wininit.exe",
            "winlogon.exe",
        }
    )

    def __init__(self) -> None:
        pyautogui.PAUSE = 0.1

    @staticmethod
    def _clamp_point(x: int, y: int) -> tuple[int, int]:
        """Keep pointer targets on a connected screen (pyautogui raises otherwise)."""
        width, height = pyautogui.size()
        margin = 2
        return (
            max(margin, min(int(x), width - margin)),
            max(margin, min(int(y), height - margin)),
        )

    @staticmethod
    def _result(message: str, **details: Any) -> dict[str, Any]:
        return {"status": "SUCCESS", "message": message, **details}

    def launch_application(self, app_name: str) -> dict[str, Any]:
        app_name = app_name.strip()
        if not app_name:
            return {"error": "Application name cannot be empty."}
        try:
            pyautogui.press("win")
            time.sleep(0.3)
            pyautogui.write(app_name, interval=0.02)
            time.sleep(0.4)  # give Start search time to resolve results
            pyautogui.press("enter")
        except Exception as error:
            return {"error": f"Could not launch '{app_name}': {error}"}
        return self._result(f"Launching {app_name}.")

    def close_application(self, app_name: str) -> dict[str, Any]:
        target = app_name.strip().lower()
        if not target:
            return {"error": "Specify an application to close."}
        closed = []
        skipped = []
        for process in psutil.process_iter(["pid", "name"]):
            try:
                process_name = (process.info["name"] or "").lower()
                if target not in process_name:
                    continue
                if process_name in self._CRITICAL_PROCESSES or process.pid == os.getpid():
                    skipped.append(process.info["name"])
                    continue
                process.terminate()
                closed.append(process.info["name"])
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        if skipped:
            return {"error": f"Refused to close protected processes: {', '.join(sorted(set(skipped)))}"}
        if not closed:
            return {"error": f"No running process matched '{app_name}'."}
        return self._result(f"Closing {app_name}.", processes=closed)

    def type_text(self, text: str) -> dict[str, Any]:
        if not text:
            return {"error": "Text cannot be empty."}
        pyautogui.write(text, interval=0.03)
        return self._result("Typed the requested text.")

    def press_key(self, key: str) -> dict[str, Any]:
        key = key.strip().lower()
        if key not in pyautogui.KEYBOARD_KEYS:
            return {"error": f"Unsupported keyboard key: {key}"}
        pyautogui.press(key)
        return self._result(f"Pressed {key}.")

    def hotkey(self, keys: list[str]) -> dict[str, Any]:
        normalized = [key.strip().lower() for key in keys]
        if not 2 <= len(normalized) <= 4 or any(key not in pyautogui.KEYBOARD_KEYS for key in normalized):
            return {"error": "Provide two to four valid keyboard keys."}
        pyautogui.hotkey(*normalized)
        return self._result("Executed keyboard shortcut.")

    def move_mouse(self, x: int, y: int) -> dict[str, Any]:
        x, y = self._clamp_point(x, y)
        current_x, current_y = pyautogui.position()
        distance = ((x - current_x) ** 2 + (y - current_y) ** 2) ** 0.5
        pyautogui.moveTo(x, y, duration=min(0.8, max(0.15, distance / 1_500)))
        return self._result("Moved the pointer.", x=x, y=y)

    def click(self, x: int, y: int, button: str = "left") -> dict[str, Any]:
        if button not in {"left", "right", "middle"}:
            return {"error": "Button must be left, right, or middle."}
        x, y = self._clamp_point(x, y)
        pyautogui.click(x=x, y=y, button=button)
        return self._result("Clicked the requested location.", x=x, y=y, button=button)

    def double_click(self, x: int, y: int) -> dict[str, Any]:
        x, y = self._clamp_point(x, y)
        pyautogui.doubleClick(x=x, y=y)
        return self._result("Double-clicked the requested location.", x=x, y=y)

    def right_click(self, x: int, y: int) -> dict[str, Any]:
        return self.click(x, y, button="right")

    def drag(self, start_x: int, start_y: int, end_x: int, end_y: int) -> dict[str, Any]:
        start_x, start_y = self._clamp_point(start_x, start_y)
        end_x, end_y = self._clamp_point(end_x, end_y)
        self.move_mouse(start_x, start_y)
        pyautogui.dragTo(end_x, end_y, duration=0.5, button="left")
        return self._result("Dragged the pointer.")

    def switch_window(self, window_name: str) -> dict[str, Any]:
        target = window_name.strip().lower()
        try:
            for candidate in window_api.getAllWindows():
                if candidate.title and target in candidate.title.lower():
                    candidate.activate()
                    return self._result(f"Switched to {candidate.title}.")
        except Exception as error:
            return {"error": f"Could not switch windows: {error}"}
        return {"error": f"No window matched '{window_name}'."}

    def close_active_window(self) -> dict[str, Any]:
        pyautogui.hotkey("alt", "f4")
        return self._result("Closed the active window.")

    def minimize_all_windows(self) -> dict[str, Any]:
        pyautogui.hotkey("win", "d")
        return self._result("Minimized all windows.")

    def scroll(self, amount: int) -> dict[str, Any]:
        pyautogui.scroll(amount)
        return self._result("Scrolled the active window.", amount=amount)

    def screenshot(self) -> dict[str, Any]:
        screenshots = WORKSPACE_DIR / "screenshots"
        screenshots.mkdir(parents=True, exist_ok=True)
        image_path = screenshots / f"screen-{int(time.time())}.png"
        pyautogui.screenshot().save(image_path)
        return self._result("Captured a screenshot.", path=str(image_path.relative_to(WORKSPACE_DIR)))

    @staticmethod
    def capture_screen_image() -> Any:
        """Return a current screenshot for Gemini vision analysis."""
        return pyautogui.screenshot()

    @staticmethod
    def screen_size() -> dict[str, int]:
        """Expose the display resolution so click coordinates stay in range."""
        width, height = pyautogui.size()
        return {"width": int(width), "height": int(height)}

    # -------------------------------------------------------------
    # Audio control (Windows core audio via pycaw)
    # -------------------------------------------------------------

    @staticmethod
    def _get_volume_interface():
        """Return the master volume endpoint interface, or None if unavailable."""
        if os.name != "nt":
            return None
        try:
            from ctypes import POINTER, cast

            import comtypes
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

            device = AudioUtilities.GetSpeakers()
            interface = device.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
            return cast(interface, POINTER(IAudioEndpointVolume))
        except Exception:
            return None

    def set_volume(self, level: int) -> dict[str, Any]:
        """Set master volume to an absolute 0-100 level."""
        volume = self._get_volume_interface()
        if volume is None:
            return {"error": "Volume control is only available on Windows with pycaw installed."}
        if not 0 <= int(level) <= 100:
            return {"error": "Volume level must be between 0 and 100."}
        volume.SetMasterVolumeLevelScalar(int(level) / 100.0, None)
        return self._result(f"Volume set to {int(level)}%.", level=int(level))

    def adjust_volume(self, delta: int) -> dict[str, Any]:
        """Raise or lower master volume by a percentage delta."""
        volume = self._get_volume_interface()
        if volume is None:
            return {"error": "Volume control is only available on Windows with pycaw installed."}
        delta = int(delta)
        if not -100 <= delta <= 100 or delta == 0:
            return {"error": "Provide a volume change between -100 and 100 (non-zero)."}
        current = round(volume.GetMasterVolumeLevelScalar() * 100)
        target = max(0, min(100, current + delta))
        volume.SetMasterVolumeLevelScalar(target / 100.0, None)
        return self._result(f"Volume changed from {current}% to {target}%.", level=target)

    def toggle_mute(self, mute: bool | None = None) -> dict[str, Any]:
        """Mute, unmute, or flip the master mute state."""
        volume = self._get_volume_interface()
        if volume is None:
            return {"error": "Volume control is only available on Windows with pycaw installed."}
        if mute is None:
            mute = not bool(volume.GetMute())
        volume.SetMute(bool(mute), None)
        return self._result("Muted." if mute else "Unmuted.", muted=bool(mute))

    def press_media_key(self, key: str) -> dict[str, Any]:
        """Press a media key: play_pause, next_track, previous_track, stop, volume_up, volume_down, mute."""
        media_keys = {
            "play_pause": "playpause",
            "next_track": "nexttrack",
            "previous_track": "prevtrack",
            "stop": "stop",
            "volume_up": "volumeup",
            "volume_down": "volumedown",
            "mute": "volumemute",
        }
        normalized = str(key).strip().lower().replace(" ", "_")
        if normalized not in media_keys:
            return {"error": f"Unsupported media key: {key}. Use one of: {', '.join(sorted(media_keys))}."}
        pyautogui.press(media_keys[normalized])
        return self._result(f"Pressed media key {normalized}.")

    # -------------------------------------------------------------
    # Session power actions
    # -------------------------------------------------------------

    def lock_workstation(self) -> dict[str, Any]:
        """Lock the Windows session (non-destructive; reversible at login)."""
        if os.name != "nt":
            return {"error": "Locking is only supported on Windows."}
        import ctypes

        ctypes.windll.user32.LockWorkStation()
        return self._result("Workstation locked.")

    def shutdown_computer(self, action: str, delay_seconds: int = 5) -> dict[str, Any]:
        """Schedule or cancel a Windows shutdown/restart/logoff with a safety delay."""
        if os.name != "nt":
            return {"error": "Shutdown actions are only supported on Windows."}
        normalized = str(action).strip().lower()
        delay = max(0, min(600, int(delay_seconds)))
        try:
            if normalized == "cancel":
                os.system("shutdown /a")
                return self._result("Scheduled shutdown cancelled.")
            if normalized == "shutdown":
                os.system(f"shutdown /s /t {delay}")
                return self._result(f"Shutting down in {delay} seconds.", delay_seconds=delay)
            if normalized == "restart":
                os.system(f"shutdown /r /t {delay}")
                return self._result(f"Restarting in {delay} seconds.", delay_seconds=delay)
            if normalized == "logoff":
                if delay > 0:
                    return {"error": "Logoff happens immediately; use delay 0 or choose shutdown/restart."}
                os.system("shutdown /l")
                return self._result("Logging off.")
            return {"error": "Action must be shutdown, restart, logoff, or cancel."}
        except Exception as error:
            return {"error": f"Shutdown action failed: {error}"}

    def screen_context(self) -> dict[str, Any]:
        try:
            active = window_api.getActiveWindow()
            windows = [
                current.title.strip()
                for current in window_api.getAllWindows()
                if current.visible and current.title and current.title.strip()
            ]
            return self._result(
                "Read current screen context.",
                active_window=active.title.strip() if active and active.title else "Unknown",
                open_windows=windows,
            )
        except Exception as error:
            return {"error": f"Could not read window context: {error}"}

    @staticmethod
    def _desktop_item(name: str) -> Path:
        item = Path(name)
        if not name.strip() or item.name != name or item.is_absolute():
            raise ValueError("Use a single file or folder name, not a path.")
        return Path.home() / "Desktop" / item

    def create_desktop_folder(self, folder_name: str) -> dict[str, Any]:
        try:
            path = self._desktop_item(folder_name)
            path.mkdir(exist_ok=True)
            return self._result(f"Desktop folder '{folder_name}' is ready.")
        except (OSError, ValueError) as error:
            return {"error": str(error)}

    def create_desktop_file(self, filename: str, content: str = "") -> dict[str, Any]:
        try:
            path = self._desktop_item(filename)
            path.write_text(content, encoding="utf-8")
            return self._result(f"Desktop file '{filename}' was created.")
        except (OSError, ValueError) as error:
            return {"error": str(error)}

    def delete_desktop_item(self, item_name: str) -> dict[str, Any]:
        try:
            path = self._desktop_item(item_name)
            if not path.exists():
                return {"error": f"'{item_name}' was not found on the Desktop."}
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            return self._result(f"Deleted '{item_name}' from the Desktop.")
        except (OSError, ValueError) as error:
            return {"error": str(error)}
