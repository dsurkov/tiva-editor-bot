"""Юнит-тесты ai_editor: гейт пика (без реального вызова DeepSeek)."""
from unittest.mock import AsyncMock, patch

import pytest

from ai_editor import AIError, PeakTimeError, edit_article, edit_existing

SECTIONS = [(40, "Salon News"), (41, "Care Guide"), (42, "Skincare")]

VALID_JSON = (
    '{"title": "T", "seo_title": "S Calgary", "seo_description": "D Calgary", '
    '"edited_text": "E", "section_id": 41, "changes": ["c"]}'
)


def _completion(content: str):
    message = type("M", (), {"content": content})()
    choice = type("C", (), {"message": message})()
    return AsyncMock(choices=[choice], usage=None)


@pytest.mark.asyncio
async def test_peak_raises():
    with patch("ai_editor.is_peak", return_value=True):
        with pytest.raises(PeakTimeError):
            await edit_article("текст", None, [], SECTIONS, "fake-key")
        with pytest.raises(PeakTimeError):
            await edit_existing("old", "Old", "fix it", [], SECTIONS, "fake-key")


@pytest.mark.asyncio
async def test_off_peak_calls_api():
    with patch("ai_editor.is_peak", return_value=False), patch(
        "ai_editor.AsyncOpenAI"
    ) as mock_openai:
        client = mock_openai.return_value
        client.chat.completions.create = AsyncMock(return_value=_completion(VALID_JSON))
        result = await edit_article("текст", None, [], SECTIONS, "fake-key")
        assert result["title"] == "T"
        assert result["section_id"] == 41
        _, kwargs = client.chat.completions.create.await_args
        assert kwargs["model"] == "deepseek-v4-flash"
        assert kwargs["response_format"] == {"type": "json_object"}
        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        # разделы передаются в user-сообщение
        assert "Care Guide" in kwargs["messages"][1]["content"]


@pytest.mark.asyncio
async def test_edit_existing_passes_instruction():
    with patch("ai_editor.is_peak", return_value=False), patch(
        "ai_editor.AsyncOpenAI"
    ) as mock_openai:
        client = mock_openai.return_value
        client.chat.completions.create = AsyncMock(return_value=_completion(VALID_JSON))
        await edit_existing("old text", "Old Title", "удали статью", [], SECTIONS, "fake-key")
        _, kwargs = client.chat.completions.create.await_args
        user_msg = kwargs["messages"][1]["content"]
        assert "Old Title" in user_msg
        assert "удали статью" in user_msg


@pytest.mark.asyncio
async def test_bad_json_raises():
    with patch("ai_editor.is_peak", return_value=False), patch(
        "ai_editor.AsyncOpenAI"
    ) as mock_openai:
        client = mock_openai.return_value
        client.chat.completions.create = AsyncMock(return_value=_completion("not json"))
        with pytest.raises(AIError):
            await edit_article("текст", None, [], SECTIONS, "fake-key")


if __name__ == "__main__":
    import asyncio

    asyncio.run(test_peak_raises())
    asyncio.run(test_off_peak_calls_api())
    asyncio.run(test_edit_existing_passes_instruction())
    asyncio.run(test_bad_json_raises())
    print("ai_editor: все тесты OK")
