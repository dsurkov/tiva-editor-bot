"""Юнит-тесты ai_editor: гейт пика (без реального вызова OpenRouter)."""
import json
from unittest.mock import patch

import pytest
import requests

from ai_editor import AIError, PeakTimeError, edit_article, edit_existing

SECTIONS = [(40, "Salon News"), (41, "Care Guide"), (42, "Skincare")]

VALID_JSON = (
    '{"title": "T", "seo_title": "S Calgary", "seo_description": "D Calgary", '
    '"edited_text": "E", "section_id": 41, "changes": ["c"]}'
)

VALID_RESPONSE = {
    "choices": [{"message": {"content": VALID_JSON}}],
    "usage": {"total_tokens": 100},
}


def _mock_response(json_data, status_code=200):
    resp = requests.Response()
    resp.status_code = status_code
    resp._content = json.dumps(json_data).encode("utf-8")
    return resp


@pytest.mark.asyncio
async def test_peak_raises():
    with patch("ai_editor.is_peak", return_value=True):
        with pytest.raises(PeakTimeError):
            edit_article("текст", None, [], SECTIONS, "fake-key")
        with pytest.raises(PeakTimeError):
            edit_existing("old", "Old", "fix it", [], SECTIONS, "fake-key")


@pytest.mark.asyncio
async def test_off_peak_calls_api():
    with patch("ai_editor.is_peak", return_value=False), patch(
        "ai_editor.requests.post"
    ) as mock_post:
        mock_post.return_value = _mock_response(VALID_RESPONSE)
        result = edit_article("текст", None, [], SECTIONS, "fake-key")
        assert result["title"] == "T"
        assert result["section_id"] == 41
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["model"] == "openrouter/inclusionai/ling-3.0-flash-fin:free"
        assert kwargs["json"]["response_format"] == {"type": "json_object"}
        assert "Care Guide" in kwargs["json"]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_edit_existing_passes_instruction():
    with patch("ai_editor.is_peak", return_value=False), patch(
        "ai_editor.requests.post"
    ) as mock_post:
        mock_post.return_value = _mock_response(VALID_RESPONSE)
        edit_existing("old text", "Old Title", "удали статью", [], SECTIONS, "fake-key")
        _, kwargs = mock_post.call_args
        user_msg = kwargs["json"]["messages"][1]["content"]
        assert "Old Title" in user_msg
        assert "удали статью" in user_msg


@pytest.mark.asyncio
async def test_bad_json_raises():
    with patch("ai_editor.is_peak", return_value=False), patch(
        "ai_editor.requests.post"
    ) as mock_post:
        mock_post.return_value = _mock_response(
            {"choices": [{"message": {"content": "not json"}}]}
        )
        with pytest.raises(AIError):
            edit_article("текст", None, [], SECTIONS, "fake-key")


if __name__ == "__main__":
    import asyncio

    asyncio.run(test_peak_raises())
    asyncio.run(test_off_peak_calls_api())
    asyncio.run(test_edit_existing_passes_instruction())
    asyncio.run(test_bad_json_raises())
    print("ai_editor: все тесты OK")
