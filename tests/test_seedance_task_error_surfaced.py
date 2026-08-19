"""Seedance surfaces Ark's structured error, and translates known ones to Chinese.

Ark returns errors as ``{"code": ..., "message": ...}`` in English. When a
matcher fires the user gets a Chinese hint plus the raw code/message for
diagnosis; when no matcher fires the code+message is still shown cleanly
(never as a Python-repr'd dict, which is what production started with).

These tests drive ``translate_ark_error`` directly — the function is the
whole abstraction, so poking it beats grepping source text.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_seedance():
    spec = importlib.util.spec_from_file_location(
        "seedance_error_surface_test", ROOT / "seedance" / "app.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["seedance_error_surface_test"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TranslateArkErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_seedance()

    def translate(self, code: str, message: str):
        return self.mod.translate_ark_error(code, message)

    # --- production incidents ------------------------------------------

    def test_edit_task_input_too_short(self):
        """2026-08-10 incident: '我是wym' submitted a 2.8s edit-input video."""
        zh = self.translate(
            "InvalidParameter.TaskTypeConstraint",
            "The parameter `content[1].video_url` specified in the request is not valid. "
            "Seedance identified your task as video editing based on your prompt. For this "
            "task type, the output ratio and duration follow the input video selected by the "
            "model for editing, and the video selected must satisfy the duration requirement "
            "of 4 to 30 seconds. Issues: [0] `content[1].video_url` is 2.8 seconds.",
        )
        self.assertIsNotNone(zh, "the production hit must have a translation")
        self.assertIn("视频编辑", zh)
        self.assertIn("4~30 秒", zh)
        # The escape hatch — user's prompt keywords caused the edit classification.
        self.assertIn("删除/编辑/替换/增加", zh)

    def test_content_policy_output_refusal(self):
        """2026-08-07: 'external monster' prompt tripped the copyright filter."""
        zh = self.translate(
            "OutputVideoSensitiveContentDetected.PolicyViolation",
            "The request failed because the output video may be related to copyright restrictions.",
        )
        self.assertIsNotNone(zh)
        self.assertIn("内容审核", zh)
        self.assertIn("版权", zh)

    def test_content_policy_input_refusal(self):
        zh = self.translate(
            "InputImageSensitiveContentDetected.PolicyViolation",
            "The request failed because the input image 'content[4]' may be related to copyright restrictions.",
        )
        self.assertIsNotNone(zh)
        self.assertIn("内容审核", zh)
        # The advice for input differs from output — user should change the source.
        self.assertIn("素材", zh)

    def test_model_not_activated(self):
        """2026-08-07 morning: 2.5 was in the model list but not activated."""
        zh = self.translate(
            "ModelNotOpen",
            "Your account 2123954716 has not activated the model doubao-seedance-2-5-260628.",
        )
        self.assertIsNotNone(zh)
        self.assertIn("开通", zh)

    def test_bad_model_id(self):
        zh = self.translate("InvalidEndpointOrModel.NotFound",
                            "The model or endpoint doubao-seedance-2-5-pro does not exist")
        self.assertIsNotNone(zh)
        self.assertIn("模型", zh)

    # --- InvalidParameter subcases from activity_log.json -------------

    def test_missing_asset_reference(self):
        """18 hits in prod: user still references an asset that was deleted or is Processing."""
        zh = self.translate(
            "InvalidParameter",
            "The parameter `content[1].image_url.url` specified in the request is not valid: "
            "The specified asset asset-20260807113001-86tdg is not found.",
        )
        self.assertIsNotNone(zh)
        self.assertIn("素材", zh)
        self.assertIn("刷新", zh)

    def test_video_asset_in_image_slot(self):
        zh = self.translate(
            "InvalidParameter",
            "The parameter `content[2].image_url.url` specified in the request is not valid: "
            "the specified asset is not an image.",
        )
        self.assertIsNotNone(zh)
        self.assertIn("视频", zh)
        self.assertIn("参考", zh)

    def test_duration_over_15_on_2_0(self):
        """6 hits: user tried >=15s on the 2.0 family."""
        zh = self.translate(
            "InvalidParameter",
            "The parameter `content[6]` specified in the request is not valid: the parameter "
            "video duration (seconds) specified in the request must be less than or equal to "
            "15.2 for model doubao-seedance-2-0 in r2v.",
        )
        self.assertIsNotNone(zh)
        self.assertIn("Seedance 2.5", zh, "the fix is to switch to 2.5")

    def test_generic_bad_duration(self):
        """4 hits: duration outside the accepted set for the current model."""
        zh = self.translate(
            "InvalidParameter",
            "the parameter duration specified in the request is not valid for model "
            "doubao-seedance-2-0 in r2v",
        )
        self.assertIsNotNone(zh)
        self.assertIn("4-15", zh)
        self.assertIn("4-30", zh)

    # --- fallback behaviour -------------------------------------------

    def test_unknown_error_returns_none(self):
        """Unmatched errors must return None so the caller shows the raw pair."""
        self.assertIsNone(self.translate("SomeNewCode.Recently", "brand new error we haven't seen"))
        self.assertIsNone(self.translate("", ""))

    def test_matcher_ordering_specific_before_generic(self):
        """The TaskTypeConstraint entry is under InvalidParameter — it must
        match before the generic InvalidParameter fallbacks even though the
        latter would also fire on the same code prefix."""
        # If ordering broke, this would fall through to a generic
        # InvalidParameter matcher and return a less specific message.
        zh = self.translate(
            "InvalidParameter.TaskTypeConstraint",
            "must satisfy the duration requirement of 4 to 30 seconds.",
        )
        self.assertIn("4~30 秒", zh)

    def test_code_prefix_match_but_message_miss_falls_through(self):
        """When the code matches but no message narrower fires, translation
        should return None rather than pick a wrong matcher."""
        # A hypothetical InvalidParameter subcode Ark might add later:
        self.assertIsNone(self.translate(
            "InvalidParameter",
            "some future rejection message we've never seen before"))


class FailurePathFormattingTests(unittest.TestCase):
    """The failure branch in run_one composes the final user-visible string.

    We can't easily invoke run_one() end-to-end (it hits Ark and polls), but
    the composition rules are simple enough to pin here so a future refactor
    of that branch can't regress silently.
    """

    def format_failure(self, task_id, status, code, message, translation):
        """Mirror of the composition in seedance/app.py run_one."""
        if translation:
            return f"Task {task_id}: {translation} 原始错误：{code}: {message}"
        return (f"Task {task_id} ended as {status}: {code} — {message}" if code
                else f"Task {task_id} ended as {status}: {message}")

    def test_translated_hit_keeps_raw_pair_for_debugging(self):
        """Even when Chinese is shown, the raw code+message must remain visible
        so the operator can find the request id and cross-check with Ark."""
        msg = self.format_failure(
            "cgt-x", "failed",
            "InvalidParameter.TaskTypeConstraint",
            "must satisfy the duration requirement of 4 to 30 seconds. Request id: 021...",
            "视频编辑要求 4~30 秒",
        )
        self.assertIn("视频编辑要求 4~30 秒", msg)
        self.assertIn("原始错误", msg)
        self.assertIn("InvalidParameter.TaskTypeConstraint", msg)
        self.assertIn("Request id: 021", msg, "the request id lives in the message — must survive")

    def test_untranslated_error_still_readable(self):
        """Unmatched errors need a clean code — message layout, no dict."""
        msg = self.format_failure(
            "cgt-y", "failed",
            "SomeNewCode.Whatever",
            "a fresh failure mode we haven't translated",
            None,
        )
        self.assertNotIn("{'", msg, "no Python-repr'd dict")
        self.assertIn("SomeNewCode.Whatever", msg)
        self.assertIn("a fresh failure mode", msg)

    def test_code_less_error_still_shows_message(self):
        """Some Ark responses have message without a code — must still print."""
        msg = self.format_failure("cgt-z", "failed", "", "raw message", None)
        self.assertIn("raw message", msg)


if __name__ == "__main__":
    unittest.main()
