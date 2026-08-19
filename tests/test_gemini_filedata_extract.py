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

    def test_gemini_url_item_is_downloaded_instead_of_read_as_b64(self, tmp_path, monkeypatch):
        """The Gemini run path uses save_gemini_image_item, so a fileData URL
        must be downloaded there instead of raising KeyError('b64_json')."""
        calls = []

        def fake_download(url, out_path):
            calls.append((url, out_path))
            out_path.write_bytes(b"PNG")

        monkeypatch.setattr(self.mod, "download_url", fake_download)
        source_url, local_path = self.mod.save_gemini_image_item(
            {
                "url": "https://cdn.example/generated?id=4",
                "mime_type": "image/png",
            },
            tmp_path,
            "parallel_4",
            1,
        )

        assert source_url == "https://cdn.example/generated?id=4"
        assert Path(local_path).name == "parallel_4_1.png"
        assert Path(local_path).read_bytes() == b"PNG"
        assert calls == [("https://cdn.example/generated?id=4", Path(local_path))]
