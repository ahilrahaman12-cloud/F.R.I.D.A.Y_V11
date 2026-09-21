"""Desktop and web entry point for F.R.I.D.A.Y."""

import asyncio
import json
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request

import config
from config import BUNDLE_DIR, MODEL_NAME, OBSIDIAN_VAULT_DIR, VAULT_DIR
from core.brain import FridayBrain
from core.tools import list_custom_links, open_custom_link, save_custom_link
from system.telemetry import get_system_state


# Runtime flags for the packaged executables.
PORTABLE_MODE = bool(os.getenv("FRIDAY_PORTABLE"))


def embedded_key_present() -> bool:
    """True when this exe was built with a precompiled Gemini key."""
    return bool(str(getattr(config, "_EMBEDDED_API_KEY", "")).strip())


def create_app(brain: FridayBrain | None = None) -> Flask:
    """Create the Flask application and allow dependency injection in tests."""
    # Frozen (PyInstaller) builds extract bundled templates to the _MEIPASS
    # bundle dir; source runs read them from disk next to this file.
    template_folder = str(Path(BUNDLE_DIR, "templates"))
    app = Flask(__name__, template_folder=template_folder)
    assistant = brain

    def get_brain() -> FridayBrain:
        nonlocal assistant
        if assistant is None:
            assistant = FridayBrain()
        return assistant

    @app.get("/")
    def index():
        if needs_setup():
            return render_template("setup.html")
        return render_template("index.html")

    @app.get("/office")
    def office():
        if needs_setup():
            return redirect("/setup")
        return render_template("office.html")

    @app.get("/setup")
    def setup():
        if not needs_setup():
            return redirect("/")
        return render_template("setup.html")

    def needs_setup() -> bool:
        """Portable build requires a key until one is saved or provided."""
        return PORTABLE_MODE and not config.has_api_key()

    def get_brain_or_error():
        """Return the brain, or an error response when no API key is configured.

        The brain needs a Gemini key at construction time; building it lazily
        for /api/reset and the confirm routes crashed with a 500 on keyless
        portable installs instead of pointing the user at setup.
        """
        if not config.has_api_key():
            return None, (
                jsonify({"status": "ERROR", "message": "No Gemini API key configured. Complete setup first."}),
                503,
            )
        return get_brain(), None

    @app.get("/api/setup/status")
    def setup_status():
        return jsonify(
            {
                "needs_setup": needs_setup(),
                "embedded_key": embedded_key_present(),
                "key_source": config.key_source(),
                "status": "OPERATIONAL" if config.has_api_key() else "NEEDS_API_KEY",
            }
        )

    @app.post("/api/setup/key")
    def setup_key():
        payload = request.get_json(silent=True) or {}
        api_key = (payload.get("api_key") or "").strip()
        if not api_key:
            return jsonify({"status": "ERROR", "message": "Paste your Gemini API key."}), 400
        if not config.save_api_key_to_env(api_key):
            return jsonify({"status": "ERROR", "message": "That does not look like a valid Gemini API key."}), 400
        return jsonify({"status": "SUCCESS", "message": "API key saved. F.R.I.D.A.Y. is online."})

    @app.get("/api/links")
    def links():
        """Saved link shortcuts for the office dashboard."""
        return jsonify(list_custom_links())

    @app.post("/api/links/open")
    def links_open():
        name = (request.get_json(silent=True) or {}).get("name", "").strip()
        if not name:
            return jsonify({"status": "ERROR", "message": "Link name required."}), 400
        return jsonify(open_custom_link(name))

    @app.post("/api/links/save")
    def links_save():
        payload = request.get_json(silent=True) or {}
        name = payload.get("name", "").strip()
        url = payload.get("url", "").strip()
        result = save_custom_link(name, url)
        status_code = 400 if result.get("error") else 200
        return jsonify(result), status_code

    @app.post("/api/chat")
    def chat():
        prompt = (request.get_json(silent=True) or {}).get("prompt", "").strip()
        if not prompt:
            return jsonify({"status": "ERROR", "response": "Prompt cannot be empty."}), 400
        try:
            return jsonify(asyncio.run(get_brain().think(prompt)))
        except Exception as error:
            return jsonify({"status": "ERROR", "response": "", "message": f"Server error: {error}"}), 500

    @app.post("/api/confirm")
    def confirm():
        brain, error = get_brain_or_error()
        if error is not None:
            return error
        approved = bool((request.get_json(silent=True) or {}).get("approve", False))
        return jsonify(asyncio.run(brain.resolve_confirmation(approved)))

    @app.post("/api/confirm/tools")
    def confirm_tools():
        brain, error = get_brain_or_error()
        if error is not None:
            return error
        approved = bool((request.get_json(silent=True) or {}).get("approve", False))
        try:
            return jsonify(asyncio.run(brain.resolve_tool_approval(approved)))
        except Exception as error:
            return jsonify({"status": "ERROR", "response": "", "message": f"Server error: {error}"}), 500

    @app.post("/api/reset")
    def reset():
        brain, error = get_brain_or_error()
        if error is not None:
            return error
        brain.reset_conversation()
        return jsonify({"status": "SUCCESS", "response": "Conversation reset."})

    @app.post("/api/transcribe")
    def transcribe():
        """Transcribe a recorded voice note via Gemini audio understanding."""
        if "audio" not in request.files:
            return jsonify({"status": "ERROR", "response": "No audio upload."}), 400
        upload = request.files["audio"]
        audio_bytes = upload.read()
        if not audio_bytes:
            return jsonify({"status": "ERROR", "response": "Empty audio upload."}), 400
        mime = (upload.mimetype or "").lower()
        if not mime.startswith("audio/"):
            # Browsers and test clients may label voice-only webm/ogg/mp4 uploads
            # as video because of the container; normalize to the audio type.
            if any(container in mime for container in ("webm", "ogg", "wav", "mpeg", "mp4", "x-matroska")):
                mime = mime.replace("video/", "audio/", 1)
            else:
                mime = "audio/webm"
        try:
            return jsonify(asyncio.run(get_brain().transcribe_audio(audio_bytes, mime)))
        except Exception as error:
            return jsonify({"status": "ERROR", "response": "", "message": f"Transcription failed: {error}"}), 500

    @app.post("/api/speak")
    def speak():
        """Convert assistant text to speech via Gemini TTS."""
        text = (request.get_json(silent=True) or {}).get("text", "").strip()
        if not text:
            return jsonify({"status": "ERROR", "response": "Nothing to speak."}), 400
        try:
            pcm, mime = asyncio.run(get_brain().synthesize_speech(text))
        except Exception as error:
            return jsonify({"status": "ERROR", "response": "", "message": f"TTS failed: {error}"}), 500
        if not pcm:
            return jsonify({"status": "ERROR", "response": "", "message": "TTS returned no audio."}), 502
        response = jsonify({"status": "SUCCESS", "audio_base64": pcm, "mime": mime})
        return response

    @app.get("/api/health")
    def health():
        """Real component status for the boot screens; never raises."""
        checks: dict[str, str] = {}

        # Gemini key: report without raising or creating the brain.
        # In portable mode a missing key means "not set up yet", not broken.
        if config.has_api_key():
            checks["gemini_key"] = "ok"
        else:
            checks["gemini_key"] = "not_configured" if PORTABLE_MODE else "missing"
        checks["model"] = MODEL_NAME

        # Memory vault: prefer the configured Obsidian path, note the fallback.
        # A fresh portable install has neither; create the local vault on demand
        # so the first boot reports healthy instead of "missing".
        try:
            if OBSIDIAN_VAULT_DIR.exists() and os.access(OBSIDIAN_VAULT_DIR, os.W_OK):
                checks["memory_vault"] = "ok"
            else:
                try:
                    VAULT_DIR.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                checks["memory_vault"] = "fallback" if VAULT_DIR.is_dir() else "missing"
        except OSError:
            checks["memory_vault"] = "unknown"

        # Screen observer: running only if a brain was already created.
        observer = getattr(assistant, "observer", None)
        if observer is not None:
            try:
                status = observer.status()
                checks["screen_observer"] = "ok" if status.get("running") else "standby"
            except Exception:
                checks["screen_observer"] = "unknown"
        else:
            checks["screen_observer"] = "standby"

        # Telemetry: cheap probe to prove psutil works.
        try:
            get_system_state()
            checks["telemetry"] = "ok"
        except Exception:
            checks["telemetry"] = "error"

        # "model" is informational, not a pass/fail check.
        status_values = [value for key, value in checks.items() if key != "model"]
        if PORTABLE_MODE and checks["gemini_key"] == "not_configured":
            overall = "NEEDS_API_KEY"
        else:
            overall = "OPERATIONAL" if all(
                value in {"ok", "standby", "fallback"} for value in status_values
            ) else "DEGRADED"
        return jsonify({"status": overall, "checks": checks})

    @app.get("/api/stats")
    def stats():
        return jsonify(get_system_state())

    return app


def run_desktop_app() -> None:
    """Start the Flask interface in a native desktop window."""
    import webview

    webview.create_window("F.R.I.D.A.Y. Assistant", create_app(), width=900, height=650, resizable=True)
    webview.start()


if __name__ == "__main__":
    # Hidden self-test entry point: FRIDAY_SELFTEST=1 runs headless checks and
    # exits, so a packaged exe can be verified without opening a GUI window.
    if os.getenv("FRIDAY_SELFTEST") == "1":
        client = create_app().test_client()
        health = client.get("/api/health").get_json()
        stats_ok = client.get("/api/stats").status_code == 200
        office_ok = client.get("/office").status_code == 200
        setup_status = client.get("/api/setup/status").get_json()
        setup_page = client.get("/setup")
        chat = client.post("/api/chat", json={"prompt": "tell me a joke"}).get_json()
        # Extra coverage: every route + the local tool layer.
        reset_ok = client.post("/api/reset").get_json().get("status") == "SUCCESS"
        confirm_empty = client.post("/api/confirm", json={"approve": True}).get_json().get("status")
        tools_empty = client.post("/api/confirm/tools", json={"approve": True}).get_json().get("status")
        transcribe_empty = client.post("/api/transcribe").status_code == 400
        speak_empty = client.post("/api/speak", json={"text": ""}).status_code == 400
        chat_empty = client.post("/api/chat", json={"prompt": ""}).status_code == 400
        bad_key_route = client.post("/api/setup/key", json={"api_key": "nope"}).status_code
        sysinfo = __import__("core.tools", fromlist=["get_system_info"]).get_system_info()
        screen_size = __import__("core.tools", fromlist=["get_screen_size"]).get_screen_size()
        main_page_ok = client.get("/").status_code == 200
        from config import PROJECT_ROOT

        print("SELFPATHS", {
            "frozen": bool(getattr(sys, "frozen", False)),
            "project_root": str(PROJECT_ROOT),
            "env_file_exists": (PROJECT_ROOT / ".env").exists(),
        })
        print("SELFTEST", {
            "health": health.get("status"),
            "vault": health.get("checks", {}).get("memory_vault"),
            "telemetry": health.get("checks", {}).get("telemetry"),
            "stats": stats_ok,
            "office": office_ok,
            "setup_page": setup_page.status_code,
            "needs_setup": setup_status.get("needs_setup"),
            "key_source": setup_status.get("key_source"),
            "chat_gate": chat.get("status"),
            # New checks:
            "main_page": main_page_ok,
            "reset": reset_ok,
            "confirm_no_pending": confirm_empty,
            "tools_confirm_no_pending": tools_empty,
            "transcribe_rejects_empty": transcribe_empty,
            "speak_rejects_empty": speak_empty,
            "chat_rejects_empty": chat_empty,
            "setup_key_rejects_bad": bad_key_route,
            "sysinfo": sysinfo.get("operating_system"),
            "screen": f"{screen_size.get('width')}x{screen_size.get('height')}",
            "python": sysinfo.get("python_version"),
        })
        sys.exit(0)
    # Headless server mode for automated smoke tests of the packaged exe:
    # FRIDAY_HEADLESS_PORT=5015 serves the Flask app over real HTTP instead of
    # opening the desktop GUI window. Normal double-click launches are unchanged.
    if os.getenv("FRIDAY_HEADLESS_PORT"):
        port = int(os.getenv("FRIDAY_HEADLESS_PORT", "5015"))
        create_app().run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
        sys.exit(0)
    run_desktop_app()
