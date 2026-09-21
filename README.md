# F.R.I.D.A.Y. — Gemini-powered desktop assistant

A Windows desktop assistant with a sci-fi HUD: it watches your screen, remembers
your preferences in an Obsidian-compatible vault, and controls the desktop
through a gated tool layer (mouse, keyboard, windows, apps, files, browser).

Ships as a **standalone executable** — no Python or terminal window required —
that asks for your Gemini API key on first run and remembers it.

## What's new in this upgrade

- **Standalone windowed executable**: built with PyInstaller (`icon.ico`
  embedded, no console flash). Mutable state (`.env`, `workspace/`, `vault/`,
  `links.json`) lives next to the exe, never in temp folders.
- **First-run setup page**: the executable gates the UI behind an
  "API KEY REQUIRED" screen; the key is validated, saved to a local `.env`,
  and remembered for every future launch.
- **Two-layer safety gate**: risky *prompts* still require approval, and risky
  *tools* (delete, close app, hotkeys, desktop writes) now require approval even
  when the prompt looked safe. The UI shows an APPROVE / DENY bar.
- **Voice in, voice out**: the mic button records real audio, Gemini transcribes
  it, and replies are spoken back via Gemini TTS.
- **New tools**: `open_url` (http/https only) and `get_screen_size` so clicks
  stay on-screen; mouse coordinates are clamped to the display.
- **Safer process control**: `close_application` refuses to terminate critical
  system processes (explorer, csrss, svchost, ...) or itself.
- **Real GPU telemetry**: NVML (NVIDIA) with a Windows-counter fallback feeds
  the previously decorative GPU gauge.
- **Resilient memory**: duplicate entries are skipped, files are trimmed, and a
  missing/unmounted Obsidian vault falls back to the local `vault/` folder.
- **Per-request action log** (`steps` in the API response), tool errors are
  isolated so one failure can't kill a task, and a RESET SESSION button clears
  conversation state via `POST /api/reset`.
- **Restored from the original monolith, rebuilt safely**: master volume control
  (`set_volume`, `adjust_volume`, `toggle_mute` via pycaw), media keys
  (`press_media_key`), Spotify/YouTube playback search, named link shortcuts
  (`save_custom_link` / `open_custom_link`, stored in `links.json`),
  `lock_workstation`, and `shutdown_computer` with cancel support. Shutdown and
  volume changes are approval-gated; the schedule uses a delay so it can be
  cancelled. The `/office` page (OFFICE button in the HUD) manages link tiles.

## Run from source

1. Create a virtual environment and install `requirements.txt`.
2. Copy `.env.example` to `.env`, then insert your own Gemini API key.
   Both Google key formats are accepted: new keys starting with `AQ.` and
   classic `AIza` keys. `AQ.` keys can be bound to the Gemini API or to Vertex
   AI express mode; if the first request fails with a key-auth error the app
   automatically retries once against the other surface (or force one with
   `GEMINI_BACKEND=vertex` in `.env`).
3. Run `python app.py`.

## Build the executable

Requires the project venv with `requirements.txt` installed, plus PyInstaller:

```bash
.venv/Scripts/python.exe -m pip install pyinstaller
.venv/Scripts/python.exe -m PyInstaller friday.spec --noconfirm
```

This produces a single executable in `dist/`:

| Output | Behavior |
| --- | --- |
| `dist/friday.exe` | Asks for the Gemini API key on first run |

The build is windowed (`console=False`), reads an optional `.env` from its own
folder, and creates `workspace/`, `vault/`, and `links.json` next to itself on
demand. A hidden self-test exists for verifying the packaged exe without
opening the GUI:

```bash
FRIDAY_SELFTEST=1 ./friday.exe
```

## First-run setup (portable build)

1. Launch `friday.exe`. The window opens on an **API KEY REQUIRED** screen.
2. Paste your key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
3. Click **Initialize System** — the key is validated and written to `.env`
   next to the exe, then the main HUD loads. Subsequent launches skip setup.

To move the app to another machine, copy `friday.exe` together with its
`.env` — or copy the exe alone and repeat the setup there.

## Test it

Run `python -m unittest discover -s tests -v` from this folder. The suite
covers routes, brain gating, memory, tools, and the first-run setup flow —
no API key needed.

## Boot screen

Both pages run a cinematic initialization overlay that probes `GET /api/health`
instead of faking success. Diagnostics show the configured model, memory vault
state, screen-observer mode, and telemetry status as they resolve. A healthy
system flashes **SYSTEM ONLINE**; the portable build without a key reports
**NEEDS_API_KEY**, a misconfigured source install shows **DEGRADED MODE**, and
an offline server shows **OFFLINE** in amber, holding longer so you can read it.

## Troubleshooting

- **401 `ACCESS_TOKEN_TYPE_UNSUPPORTED` on chat** — the key is the wrong
  credential type (e.g. an OAuth token, not an AI Studio API key) or is
  API-restricted. Create a fresh key at
  [aistudio.google.com/apikey](https://aistudio.google.com/apikey), then update
  `.env` (source install) or delete the `.env` next to the exe and re-run the
  setup page.
- **Vault reports `missing`/`fallback`** — `OBSIDIAN_VAULT_PATH` points to an
  unavailable drive; the app falls back to the local `vault/` automatically.
- **PyInstaller warns about a missing hidden import** — verify it against the
  runtime behavior; optional platform imports (e.g. `nvidia_ml_py` on machines
  without NVML) degrade gracefully at run time.

## Screen privacy

While the app is running, it captures a new local screen frame every two seconds. Frames are held only in memory, but the latest frame is sent to Gemini whenever you submit a request so F.R.I.D.A.Y. can visually understand the desktop.

## Obsidian Memory

Open your vault folder (set via `OBSIDIAN_VAULT_PATH`, defaulting to the
project-local `vault/`) in Obsidian. F.R.I.D.A.Y. updates its profile, operating
lessons, and activity log there in the background. Add your own Markdown notes
to that vault; notes whose filename or contents match the current request are
included as context automatically.

## API surface

| Route | Purpose |
| --- | --- |
| `POST /api/chat` | Send a prompt; returns status, response, steps |
| `POST /api/confirm` | Approve/deny a prompt-gated request |
| `POST /api/confirm/tools` | Approve/deny gated tool calls mid-task |
| `POST /api/reset` | Clear conversation history and pending state |
| `POST /api/transcribe` | multipart `audio` file → Gemini transcription |
| `POST /api/speak` | `{text}` → base64 PCM audio via Gemini TTS |
| `GET /api/stats` | CPU / RAM / GPU / disk / network telemetry |
| `GET /api/health` | Component health for the boot screens (Gemini key, vault, observer, telemetry) |
| `GET /setup` | First-run API-key setup page (portable build) |
| `GET /api/setup/status` | Setup state: `needs_setup`, `key_source`, overall status |
| `POST /api/setup/key` | Validate and save the Gemini key to the local `.env` |
| `GET /office` | Link-tile dashboard (office dock) |
| `GET /api/links` | List saved link shortcuts |
| `POST /api/links/save` | Save a link shortcut `{name, url}` |
| `POST /api/links/open` | Open a saved link `{name}` |
