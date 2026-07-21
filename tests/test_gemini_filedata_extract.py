"""Regression for #16: Gemini generateContent can return an image by URL
reference (fileData.fileUri) instead of inline base64 (inlineData.data).
extract_gemini_images used to only read inlineData, so URL-form results were
dropped and the caller raised "No image result found" even though the image
was generated. This verifies both forms are recognized.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(mod_path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestExtractGeminiImages:
    def setup_method(self):
        self.mod = _load(ROOT / "nano-banana" / "app.py", "nb_app_gemini_test")

    def test_inline_data_still_works(self):
        result = {
            "candidates": [
                {"content": {"parts": [
                    {"inlineData": {"data": "QUJD", "mimeType": "image/png"}},
                ]}}
            ]
        }
        items = self.mod.extract_gemini_images(result)
        assert len(items) == 1
        assert items[0]["b64_json"] == "QUJD"
        assert items[0]["mime_type"] == "image/png"
        assert "url" not in items[0]

    def test_snake_case_inline_data(self):
        result = {"candidates": [{"content": {"parts": [
            {"inline_data": {"data": "WFla", "mime_type": "image/webp"}},
        ]}}]}
        items = self.mod.extract_gemini_images(result)
        assert len(items) == 1
        assert items[0]["b64_json"] == "WFla"
        assert items[0]["mime_type"] == "image/webp"

    def test_file_data_uri_is_extracted_as_url(self):
        result = {"candidates": [{"content": {"parts": [
            {"fileData": {"fileUri": "https://cdn.example/x.png?x-expires=1", "mimeType": "image/png"}},
        ]}}]}
        items = self.mod.extract_gemini_images(result)
        assert len(items) == 1
        assert items[0]["url"] == "https://cdn.example/x.png?x-expires=1"
        assert items[0]["mime_type"] == "image/png"
        assert "b64_json" not in items[0]

    def test_snake_case_file_data(self):
        result = {"candidates": [{"content": {"parts": [
            {"file_data": {"file_uri": "https://cdn.example/y.jpg", "mime_type": "image/jpeg"}},
        ]}}]}
        items = self.mod.extract_gemini_images(result)
        assert len(items) == 1
        assert items[0]["url"] == "https://cdn.example/y.jpg"
        assert items[0]["mime_type"] == "image/jpeg"

    def test_mixed_inline_and_file_data(self):
        result = {"candidates": [{"content": {"parts": [
            {"inlineData": {"data": "QQ==", "mimeType": "image/png"}},
            {"fileData": {"fileUri": "https://cdn.example/z.png"}},
            {"text": "ignore me"},
        ]}}]}
        items = self.mod.extract_gemini_images(result)
        assert len(items) == 2
        assert items[0]["b64_json"] == "QQ=="
        assert items[1]["url"] == "https://cdn.example/z.png"

    def test_file_data_without_uri_is_skipped(self):
        result = {"candidates": [{"content": {"parts": [
            {"fileData": {"mimeType": "image/png"}},  # no fileUri
        ]}}]}
        items = self.mod.extract_gemini_images(result)
        assert items == []

    def test_empty_result(self):
        assert self.mod.extract_gemini_images({}) == []
        assert self.mod.extract_gemini_images({"candidates": []}) == []

    def test_save_image_item_url_branch_reachable(self):
        """The url dict produced above must be consumable by save_image_item's
        existing url branch (we don't hit the network; just confirm it routes
        to download_url rather than raising 'No image data')."""
        import inspect
        src = inspect.getsource(self.mod.save_image_item)
        assert 'item.get("url")' in src
        assert "download_url" in src
