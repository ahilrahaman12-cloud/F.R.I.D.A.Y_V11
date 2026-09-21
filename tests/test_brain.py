"""Tests for local brain decisions that do not require an API key."""

import unittest

from core.brain import FridayBrain


def make_brain() -> FridayBrain:
    return FridayBrain(client=object(), memory=_StubMemory(), observer=_StubObserver())


class _StubMemory:
    def context(self, query: str = "") -> str:
        return "No saved memory is available."

    def append_lesson(self, lesson: str) -> None:
        self.last_lesson = lesson

    def log_activity(self, event: str) -> None:
        self.last_activity = event


class _StubObserver:
    def latest_image(self):
        return None


class FridayBrainSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_prompt_is_rejected(self):
        brain = make_brain()
        result = await brain.think("   ")
        self.assertEqual(result["status"], "ERROR")

    async def test_risky_prompt_waits_for_confirmation(self):
        brain = make_brain()
        result = await brain.think("Delete the old report")
        self.assertEqual(result["status"], "WAITING_FOR_USER_APPROVAL")
        self.assertEqual(brain.pending_prompt, "Delete the old report")

    async def test_risky_prompt_gate_reports_prompt_endpoint(self):
        brain = make_brain()
        result = await brain.think("Restart the computer")
        self.assertEqual(result.get("gate"), "prompt")

    async def test_rejected_confirmation_clears_pending_prompt(self):
        brain = make_brain()
        await brain.think("Restart the computer")
        result = await brain.resolve_confirmation(False)
        self.assertEqual(result["status"], "CANCELLED")
        self.assertIsNone(brain.pending_prompt)

    def test_risky_tools_require_approval_even_after_safe_prompt(self):
        brain = make_brain()
        for tool in ("delete_desktop_item", "close_application", "execute_shortcut", "press_key"):
            self.assertTrue(brain._tool_needs_approval(tool), tool)

    def test_safe_tools_are_always_allowed(self):
        brain = make_brain()
        for tool in ("get_screen_context", "get_screen_size", "list_workspace_files", "take_screenshot", "move_mouse"):
            self.assertFalse(brain._tool_needs_approval(tool), tool)

    def test_latest_observed_frame_is_attached_to_every_request(self):
        image = object()
        observer = type("Observer", (), {"latest_image": lambda self: image})()
        brain = FridayBrain(client=object(), memory=_StubMemory(), observer=observer)
        self.assertEqual(brain._message_with_screen("Explain Python decorators"), ["Explain Python decorators", image])

    async def test_tool_approval_without_pending_calls_is_rejected(self):
        brain = make_brain()
        result = await brain.resolve_tool_approval(True)
        self.assertEqual(result["status"], "NO_PENDING_ACTION")

    async def test_reset_conversation_clears_state(self):
        brain = make_brain()
        brain.history.append("stale")
        brain.pending_tool_calls.append(("delete_desktop_item", {}))
        brain._steps.append("click: ok")
        brain.reset_conversation()
        self.assertEqual(brain.history, [])
        self.assertEqual(brain.pending_tool_calls, [])
        self.assertEqual(brain._steps, [])
        self.assertIsNone(brain._chat)

    def test_key_auth_errors_are_detected_for_fallback(self):
        brain = make_brain()
        self.assertTrue(brain._is_key_auth_error(Exception("400 API key not valid. Please pass a valid API key.")))
        self.assertTrue(brain._is_key_auth_error(Exception("401 UNAUTHENTICATED ACCESS_TOKEN_TYPE_UNSUPPORTED")))
        self.assertTrue(brain._is_key_auth_error(Exception("403 PERMISSION_DENIED")))
        self.assertFalse(brain._is_key_auth_error(Exception("503 UNAVAILABLE")))
        self.assertFalse(brain._is_key_auth_error(Exception("429 RESOURCE_EXHAUSTED")))

    def test_quota_errors_get_friendly_message(self):
        error = Exception(
            "429 RESOURCE_EXHAUSTED. You exceeded your current quota, "
            "limit: 20, model: gemini-3.6-flash"
        )
        message = FridayBrain._friendly_error(error)
        self.assertIn("quota", message.lower())
        self.assertNotIn("RESOURCE_EXHAUSTED", message)

    def test_retired_model_errors_get_friendly_message(self):
        error = Exception(
            "404 NOT_FOUND. This model models/gemini-2.5-flash is no longer "
            "available to new users. Please update your code."
        )
        message = FridayBrain._friendly_error(error)
        self.assertIn("GEMINI_MODEL", message)
        self.assertIn("gemini-3.6-flash", message)

    def test_unknown_errors_pass_through(self):
        message = FridayBrain._friendly_error(Exception("503 UNAVAILABLE"))
        self.assertIn("Assistant request failed", message)


if __name__ == "__main__":
    unittest.main()
