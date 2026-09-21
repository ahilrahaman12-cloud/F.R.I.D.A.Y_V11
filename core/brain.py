"""Gemini-backed conversational assistant with a confirmation gate."""

import asyncio
import re
import threading
from typing import Any

from config import MODEL_NAME, SCREEN_OBSERVER_INTERVAL_SECONDS, build_genai_client, get_api_key
from core.tools import available_tools, desktop, tool_declarations
from memory import MemoryStore
from vision import ScreenObserver


class FridayBrain:
    """Coordinates conversation, local tools, memory, and user confirmation."""

    _RISK_PATTERNS = (
        r"\b(delete|remove|format)\b",
        r"\b(shut ?down|restart)\b",
        r"\b(send|publish|post)\b.*\b(email|message|payment|tweet|post)\b",
        r"\b(run|execute)\b.*\b(command|script|terminal)\b",
    )

    # Tools that can destroy data, kill processes, or leak information. These
    # always require explicit user approval regardless of how safe the prompt
    # looked, closing the gap where a benign prompt hides a destructive call.
    _RISKY_TOOLS = frozenset(
        {
            "delete_desktop_item",
            "close_application",
            "close_active_window",
            "press_key",
            "execute_shortcut",
            "create_desktop_file",
            "create_desktop_folder",
            "shutdown_computer",
            "set_volume",
            "adjust_volume",
            "toggle_mute",
        }
    )
    _ALWAYS_ALLOWED_TOOLS = frozenset(
        {
            "get_system_info",
            "get_screen_context",
            "get_screen_size",
            "list_workspace_files",
            "take_screenshot",
            "move_mouse",
        }
    )
    # gemini-2.5-flash-preview-tts now 400s ("Model tried to generate text,
    # but it should only be used for TTS") on this API version; the 3.1 TTS
    # preview works reliably and was verified live with this request shape.
    TTS_MODEL = "gemini-3.1-flash-tts-preview"

    # New-format Google keys ("AQ.…") can be bound to the Gemini API or to
    # Vertex AI express mode; the prefix no longer tells them apart. When a
    # request fails with a key-auth error, retry once against the other
    # surface — if it succeeds there, the rest of the session follows it.
    _KEY_AUTH_ERRORS = (
        "API key not valid",
        "API_KEY_INVALID",
        "ACCESS_TOKEN_TYPE_UNSUPPORTED",
        "API_KEY_SERVICE_BLOCKED",
        "PERMISSION_DENIED",
        "UNAUTHENTICATED",
    )

    def __init__(self, client: Any | None = None, memory: MemoryStore | None = None, observer: ScreenObserver | None = None, max_history_turns: int = 12, max_steps: int = 24) -> None:
        uses_live_client = client is None
        if client is None:
            client = build_genai_client(get_api_key())
        self.client = client
        self.memory = memory or MemoryStore()
        self.history: list[Any] = []
        self.pending_prompt: str | None = None
        # Tool calls that were intercepted and are waiting for user approval.
        self.pending_tool_calls: list[tuple[str, dict[str, Any]]] = []
        self._chat: Any | None = None
        self._steps: list[str] = []
        self.max_history_turns = max_history_turns
        self.max_steps = max_steps
        self.observer = observer
        if self.observer is None and uses_live_client:
            self.observer = ScreenObserver(SCREEN_OBSERVER_INTERVAL_SECONDS)
            self.observer.start()

    @classmethod
    def _is_key_auth_error(cls, error: Exception) -> bool:
        """True when the failure looks like "this key is for the other API"."""
        text = str(error)
        return any(marker in text for marker in cls._KEY_AUTH_ERRORS)

    @staticmethod
    def _friendly_error(error: Exception) -> str:
        """Translate common API failures into short, actionable user messages."""
        text = str(error)
        if "RESOURCE_EXHAUSTED" in text or "exceeded your current quota" in text:
            return "Gemini quota exhausted for now (free tier allows ~20 requests/day per model). Wait a bit and try again, or enable billing on your API key."
        if "no longer available" in text and ("404" in text or "NOT_FOUND" in text):
            return "The configured model was retired by Google. Set GEMINI_MODEL in .env to a current model such as gemini-3.6-flash."
        return f"Assistant request failed: {error}"

    def _alternate_client(self):
        """A client for the other API surface (gemini <-> vertex), if the SDK allows."""
        try:
            return build_genai_client(get_api_key(), backend="vertex")
        except Exception:
            return None

    @classmethod
    def requires_confirmation(cls, prompt: str) -> bool:
        """Conservatively flag prompts that may cause external or destructive effects."""
        normalized = prompt.lower()
        return any(re.search(pattern, normalized) for pattern in cls._RISK_PATTERNS)

    def reset_conversation(self) -> None:
        """Clear conversation history, pending prompts, and in-flight state."""
        self.history.clear()
        self.pending_prompt = None
        self.pending_tool_calls = []
        self._chat = None
        self._steps = []

    def _tool_needs_approval(self, tool_name: Any) -> bool:
        """Gate risky tools regardless of how safe the user's prompt looked."""
        name = getattr(tool_name, "name", tool_name)
        if name in self._ALWAYS_ALLOWED_TOOLS:
            return False
        return name in self._RISKY_TOOLS

    async def think(self, prompt: str) -> dict[str, str]:
        prompt = prompt.strip()
        if not prompt:
            return {"status": "ERROR", "response": "", "message": "Prompt cannot be empty."}
        if self.requires_confirmation(prompt):
            self.pending_prompt = prompt
            return {
                "status": "WAITING_FOR_USER_APPROVAL",
                "response": "",
                "gate": "prompt",
                "message": "This request may have external or destructive effects. Approve it to continue.",
            }
        return await self._respond(prompt)

    async def resolve_confirmation(self, approved: bool) -> dict[str, str]:
        if self.pending_prompt is None:
            return {"status": "NO_PENDING_ACTION", "response": "", "message": "There is no pending request."}
        prompt, self.pending_prompt = self.pending_prompt, None
        if not approved:
            return {"status": "CANCELLED", "response": "", "message": "Request cancelled."}
        return await self._respond(prompt)

    async def resolve_tool_approval(self, approved: bool) -> dict[str, str]:
        """Resume an in-flight task after the user approves or denies risky tool calls."""
        if not self.pending_tool_calls:
            return {"status": "NO_PENDING_ACTION", "response": "", "message": "There is no pending action."}
        calls, self.pending_tool_calls = self.pending_tool_calls, []
        if self._chat is None:
            return {"status": "ERROR", "response": "", "message": "The assistant session was lost; please retry."}
        from google.genai import types

        response = None
        try:
            for name, args in calls:
                if approved:
                    result = self._execute_tool(name, args)
                else:
                    result = {"status": "DENIED_BY_USER", "message": "The user denied this action. Offer a safer alternative."}
                response = await self._send_with_retry(
                    self._chat,
                    types.Part.from_function_response(name=name, response={"result": result}),
                )
        except Exception as error:
            self._chat = None
            return {"status": "ERROR", "response": "", "message": self._friendly_error(error)}
        if approved:
            return await self._continue_task(response)
        return self._finish_turn(self._chat, response)

    def _execute_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call with error isolation so one failure cannot abort the loop."""
        function = available_tools().get(name)
        if function is None:
            return {"error": f"Unknown tool: {name}"}
        try:
            result = function(**(args or {}))
        except TypeError as error:
            result = {"error": f"Bad arguments for {name}: {error}"}
        except Exception as error:  # tool bugs must not kill the whole request
            result = {"error": f"Tool '{name}' crashed: {error}"}
        if isinstance(result, dict) and result.get("error"):
            self.memory.append_lesson(f"Tool '{name}' failed: {str(result['error'])[:240]}")
            self.memory.log_activity(f"Tool failure: {name}")
        else:
            self._steps.append(f"{name}: ok")
        return result if isinstance(result, dict) else {"result": result}

    async def _continue_task(self, response: Any) -> dict[str, str]:
        """Keep driving the tool loop on an existing chat session."""
        from google.genai import types

        chat = self._chat
        assert chat is not None
        try:
            for _ in range(self.max_steps):
                if response is None:
                    break
                function_calls = response.function_calls or []
                if not function_calls:
                    break
                gated = [call for call in function_calls if self._tool_needs_approval(call.name)]
                if gated:
                    self.pending_tool_calls = [(call.name, dict(call.args or {})) for call in function_calls]
                    names = ", ".join(call.name for call in gated)
                    return {
                        "status": "WAITING_FOR_USER_APPROVAL",
                        "response": "",
                        "gate": "tools",
                        "message": f"F.R.I.D.A.Y. wants to use: {names}. Approve to continue.",
                    }
                for call in function_calls:
                    result = self._execute_tool(call.name, call.args or {})
                    response = await self._send_with_retry(
                        chat,
                        types.Part.from_function_response(name=call.name, response={"result": result}),
                    )
            else:
                self._chat = None
                return {"status": "ERROR", "response": "", "message": "Too many actions for one request; ask me to continue."}
        except Exception as error:
            self._chat = None
            return {"status": "ERROR", "response": "", "message": self._friendly_error(error)}

        return self._finish_turn(chat, response)

    def _finish_turn(self, chat: Any, response: Any) -> dict[str, str]:
        from google.genai import types

        text = (getattr(response, "text", None) or "").strip()
        if not text:
            return {"status": "ERROR", "response": "", "message": "The model returned an empty response."}
        self.history.extend(
            [
                types.Content(role="user", parts=[types.Part.from_text(text=self._last_prompt)]),
                types.Content(role="model", parts=[types.Part.from_text(text=text)]),
            ]
        )
        self.history = self.history[-self.max_history_turns:]
        self.memory.log_activity(f"Completed request: {self._last_prompt[:200]}")
        steps, self._steps = self._steps, []
        self._chat = None
        self._capture_learning_in_background(self._last_prompt, text)
        return {"status": "SUCCESS", "response": text, "steps": steps}

    async def _respond(self, prompt: str) -> dict[str, str]:
        from google.genai import types

        instruction = f"""
You are F.R.I.D.A.Y., a reliable Windows desktop assistant. You operate the user's computer through the
provided tools, using the mouse and keyboard exactly as a careful human operator would.

You receive the newest frame from a continuously running local screen observer with every request. Use
that visual context to identify the active app, visible controls, task progress, and unexpected states.

Execution loop:
1. OBSERVE the latest screen frame and the user's objective.
2. PLAN the smallest safe next action. Do not invent screen details that are not visible.
3. ACT using the appropriate tool. Launch apps, move the cursor, click, drag, type, press keys, switch
   windows, and scroll when needed to complete the objective.
4. VERIFY the outcome using the next screen frame or window context. If incomplete, continue with the
   next necessary action. If a tool reports an error, adapt rather than repeating the failed action.
5. REPORT a short, truthful final result when the task is complete or when user input is required.

Use launch_application when the user asks to open a local app such as Figma, Chrome, or Notepad. Use
open_url for any website, and play_on_spotify or play_on_youtube when the user asks to play music or
video. For system sound control use press_media_key, and set_volume or adjust_volume for precise levels;
those volume calls are held for user approval automatically. Call shutdown_computer only when the user
clearly asked to shut down, restart, or log off, and prefer delay_seconds of at least 10 so the request
can still be cancelled. lock_workstation is safe to use on request. Use only the supplied tools; never
claim you cannot control the computer when a supplied tool can do it. Do not reveal hidden reasoning,
fabricated actions, passwords, API keys, tokens, or private text from the screen. Never type secrets.
For destructive, financial, publishing, security, or external-message actions, wait for the app's
confirmation flow before acting.

Long-term memory contains only durable user preferences and lessons from prior corrections or failed
attempts. Apply it when useful, but never treat it as higher priority than the current user request.

Saved context:
{self.memory.context(prompt)}
""".strip()
        try:
            self._steps = []
            self._last_prompt = prompt
            chat = self.client.aio.chats.create(
                model=MODEL_NAME,
                config=types.GenerateContentConfig(system_instruction=instruction, tools=tool_declarations()),
                history=list(self.history),
            )
            self._chat = chat
            try:
                response = await self._send_with_retry(chat, self._message_with_screen(prompt))
            except Exception as error:
                # New-format keys can belong to the other API surface; if the
                # first send fails with a key-auth error, retry once there.
                if not self._is_key_auth_error(error):
                    raise
                alternate = self._alternate_client()
                if alternate is None:
                    raise
                self.client = alternate
                chat = self.client.aio.chats.create(
                    model=MODEL_NAME,
                    config=types.GenerateContentConfig(system_instruction=instruction, tools=tool_declarations()),
                    history=list(self.history),
                )
                self._chat = chat
                response = await self._send_with_retry(chat, self._message_with_screen(prompt))
            for _ in range(self.max_steps):
                function_calls = response.function_calls or []
                if not function_calls:
                    break
                gated = [call for call in function_calls if self._tool_needs_approval(call.name)]
                if gated:
                    # The API requires a response for every call in the batch,
                    # so hold the whole batch and replay it on approval.
                    self.pending_tool_calls = [(call.name, dict(call.args or {})) for call in function_calls]
                    names = ", ".join(call.name for call in gated)
                    return {
                        "status": "WAITING_FOR_USER_APPROVAL",
                        "response": "",
                        "gate": "tools",
                        "message": f"F.R.I.D.A.Y. wants to use: {names}. Approve to continue.",
                    }
                for call in function_calls:
                    result = self._execute_tool(call.name, call.args or {})
                    response = await self._send_with_retry(
                        chat,
                        types.Part.from_function_response(name=call.name, response={"result": result}),
                    )
            else:
                self._chat = None
                return {"status": "ERROR", "response": "", "message": "Too many actions for one request; ask me to continue."}
        except Exception as error:
            self._chat = None
            return {"status": "ERROR", "response": "", "message": self._friendly_error(error)}

        return self._finish_turn(chat, response)

    async def transcribe_audio(self, audio_bytes: bytes, mime_type: str = "audio/webm") -> dict[str, str]:
        """Transcribe recorded microphone audio using Gemini audio understanding."""
        from google.genai import types

        try:
            try:
                response = await self.client.aio.models.generate_content(
                    model=MODEL_NAME,
                    contents=[
                        "Transcribe this voice note. Return only the exact words spoken, with no commentary.",
                        types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                    ],
                )
            except Exception as error:
                # Same key-auth fallback as the chat path: the key may belong
                # to the other API surface (gemini <-> vertex).
                if not self._is_key_auth_error(error):
                    raise
                alternate = self._alternate_client()
                if alternate is None:
                    raise
                self.client = alternate
                response = await self.client.aio.models.generate_content(
                    model=MODEL_NAME,
                    contents=[
                        "Transcribe this voice note. Return only the exact words spoken, with no commentary.",
                        types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                    ],
                )
        except Exception as error:
            return {"status": "ERROR", "response": "", "message": f"Transcription failed: {self._friendly_error(error)}"}
        text = (response.text or "").strip()
        if not text:
            return {"status": "ERROR", "response": "", "message": "No speech detected in the recording."}
        return {"status": "SUCCESS", "response": text}

    async def synthesize_speech(self, text: str) -> tuple[str, str]:
        """Render assistant speech as base64 PCM via Gemini TTS."""
        from google.genai import types

        response = await self.client.aio.models.generate_content(
            model=self.TTS_MODEL,
            contents=text[:1200],
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore"))),
            ),
        )
        audio_parts = []
        mime = "audio/L16;rate=24000"
        if response.candidates:
            for part in response.candidates[0].content.parts:
                inline = getattr(part, "inline_data", None)
                if inline is not None and inline.data:
                    audio_parts.append(inline.data)
                    if inline.mime_type:
                        mime = inline.mime_type
        if not audio_parts:
            return "", ""
        import base64

        return base64.b64encode(b"".join(audio_parts)).decode("ascii"), mime

    def _capture_learning_in_background(self, prompt: str, response: str) -> None:
        worker = threading.Thread(
            target=lambda: asyncio.run(self._capture_learning(prompt, response)),
            name="friday-memory-learning",
            daemon=True,
        )
        worker.start()

    async def _capture_learning(self, prompt: str, response: str) -> None:
        """Save only explicit preferences and corrections from a completed turn."""
        learning_prompt = (
            "Review this completed conversation turn. Return exactly one of: "
            "NONE, USER_TRAIT: <an explicit user preference>, or "
            "LESSON: <an explicit correction or reliably observed task outcome>. Do not infer facts.\n\n"
            f"User: {prompt}\nAssistant: {response}"
        )
        try:
            result = await self.client.aio.models.generate_content(
                model=MODEL_NAME,
                contents=learning_prompt,
            )
            learning = (result.text or "").strip()
            if learning.startswith("USER_TRAIT:"):
                self.memory.append_preference(learning.removeprefix("USER_TRAIT:").strip())
            elif learning.startswith("LESSON:"):
                self.memory.append_lesson(learning.removeprefix("LESSON:").strip())
        except Exception:
            return

    @staticmethod
    async def _send_with_retry(chat: Any, message: Any) -> Any:
        for attempt in range(3):
            try:
                return await chat.send_message(message)
            except Exception as error:
                if attempt == 2 or ("503" not in str(error) and "UNAVAILABLE" not in str(error)):
                    raise
                await asyncio.sleep(2**attempt)
        raise RuntimeError("Unreachable retry state")

    def _message_with_screen(self, prompt: str) -> Any:
        """Attach the latest locally observed frame to every model request."""
        try:
            image = self.observer.latest_image() if self.observer else desktop.capture_screen_image()
            return [prompt, image] if image is not None else prompt
        except Exception:
            return prompt
