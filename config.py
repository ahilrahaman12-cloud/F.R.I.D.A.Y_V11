"""Application configuration loaded from environment variables."""

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv


# When frozen by PyInstaller (onefile), bundled read-only data files extract
# to the temp _MEIPASS folder, while mutable state (.env, workspace/, vault/)
# should live next to the executable.
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
else:
    PROJECT_ROOT = Path(__file__).resolve().parent
    BUNDLE_DIR = PROJECT_ROOT

# Load .env before reading any environment variables below.
load_dotenv(PROJECT_ROOT / ".env")

WORKSPACE_DIR = PROJECT_ROOT / "workspace"
VAULT_DIR = PROJECT_ROOT / "vault"
LINKS_FILE = PROJECT_ROOT / "links.json"
OBSIDIAN_VAULT_DIR = Path(
    os.getenv("OBSIDIAN_VAULT_PATH", r"D:\Obsedian\F.R.I.D.A.Y. Vault")
).expanduser()

# Google retired gemini-2.5-flash for new users (404 "no longer available");
# 3.6-flash is the current recommended successor for the Gemini API.
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()
SCREEN_OBSERVER_INTERVAL_SECONDS = max(0.5, float(os.getenv("SCREEN_OBSERVER_INTERVAL_SECONDS", "2")))

# Google now issues new-format keys ("AQ.\u2026") for both the Gemini API and
# Vertex AI express mode; the prefix no longer identifies the surface. Default
# to the classic Gemini API host; "vertex" targets the Vertex AI express host
# instead. The brain falls back to the other surface once if a key-auth error
# shows the key belongs to the other one.
VALID_BACKENDS = ("gemini", "vertex")


def _normalize_backend(value: str | None) -> str:
    text = (value or "").strip().lower()
    if text in {"vertex", "vertexai", "aiplatform", "express"}:
        return "vertex"
    return "gemini"


def _env_backend() -> str:
    return _normalize_backend(os.getenv("GEMINI_BACKEND"))


def build_genai_client(api_key: str, backend: str | None = None):
    """Create a google-genai Client for the requested surface.

    backend: "gemini" (default, generativelanguage host) or "vertex"
    (Vertex AI express mode, aiplatform host). None reads GEMINI_BACKEND.
    """
    from google import genai

    surface = _env_backend() if backend is None else _normalize_backend(backend)
    if surface == "vertex":
        return genai.Client(vertexai=True, api_key=api_key)
    return genai.Client(api_key=api_key)


# Populated at build time for the embedded-key executable via a PyInstaller
# runtime hook (see friday.spec). The portable build leaves this empty.
_EMBEDDED_API_KEY = os.environ.pop("FRIDAY_EMBEDDED_KEY", "")
os.environ.pop("FRIDAY_EMBEDDED_KEY", None)


def key_source() -> str:
    """Return where the Gemini key comes from: 'env', 'embedded', or 'none'."""
    if os.getenv("GEMINI_API_KEY", "").strip():
        return "env"
    if _EMBEDDED_API_KEY.strip():
        return "embedded"
    return "none"


def has_api_key() -> bool:
    """True when a Gemini key is available from any source."""
    return key_source() != "none"


def get_api_key() -> str:
    """Return the configured Gemini API key or raise a clear setup error."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        api_key = _EMBEDDED_API_KEY.strip()
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key, or open /setup.")
    return api_key


def save_api_key_to_env(api_key: str) -> bool:
    """Persist GEMINI_API_KEY into the .env next to the app; True on success."""
    key = (api_key or "").strip()
    # Google AI Studio keys are long alphanumeric strings (may contain -, _
    # and, in newer key formats, a dot separator like "AQ.").
    if len(key) < 20 or not re.fullmatch(r"[A-Za-z0-9_.\-]+", key):
        return False
    env_path = PROJECT_ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    except OSError:
        lines = []
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith("GEMINI_API_KEY="):
            lines[index] = f"GEMINI_API_KEY={key}"
            replaced = True
            break
    if not replaced:
        lines.append(f"GEMINI_API_KEY={key}")
    try:
        env_path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    except OSError:
        return False
    os.environ["GEMINI_API_KEY"] = key
    return True
