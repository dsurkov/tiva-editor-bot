"""Редактура статьи через OpenRouter (inclusionai/ling-3.0-flash-fin:free, JSON)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import requests

from peak import is_peak

logger = logging.getLogger("ai_editor")

MODEL = "inclusionai/ling-3.0-flash-fin:free"
FALLBACK_MODEL = "z-ai/glm-5.3-flash"
# Модели, поддерживающие response_format (json_object); остальным не слать — будет 400.
JSON_RESPONSE_MODELS = {FALLBACK_MODEL}

SYSTEM_PROMPT = """You are the editor of the TIVA BEAUTY beauty salon blog in Calgary, Canada.
The website and blog are entirely in English — your readers are English-speaking salon customers.

Task: prepare an article for publication.

Rules:
- The article MUST be in English. If the source text is in Russian or another language,
  translate it fully into natural English.
- Fix spelling, grammar, and style errors. Keep the meaning and structure; never invent
  facts that are not present in the text.
- The text is for the end visitor (the salon customer). Remove any service or technical
  content: keyword lists, tag lists, comments, instructions to the editor, SEO notes,
  visible markup tags — anything not meant for the reader.
- Do NOT insert any booking buttons or promotional call-to-action blocks into the text.
- Do NOT repeat the headline: "edited_text" must NOT start with the title or any H1
  version of it. The title lives only in the "title" field; start edited_text directly
  with the body (a lead paragraph or a subheading).
- You may add up to 2 natural internal links to relevant services from the provided list,
  only where contextually appropriate (format: anchor text with href).
- Choose the best matching journal section from the provided list; return its id.

Return STRICTLY JSON without markdown wrappers or explanations. Schema:
{
  "title": "article headline, ≤70 chars, attractive",
  "seo_title": "SEO title, ≤60 chars, must contain Calgary",
  "seo_description": "SEO description, ≤160 chars, must contain Calgary",
  "edited_text": "full edited HTML article text",
  "section_id": "int — id of the best matching section from the list",
  "changes": ["2-5 short items in English describing what was changed"]
}"""

EDIT_SYSTEM_PROMPT = """You are the editor of the TIVA BEAUTY beauty salon blog in Calgary, Canada.
The website and blog are entirely in English — your readers are English-speaking salon customers.

Task: apply the editor's revision request to an existing article.

Rules:
- The article MUST be in English. Translate any non-English material into natural English.
- Follow the revision instruction exactly. The instruction may ask to: fix specific parts,
  rewrite the whole article with new text, change the headline, improve SEO fields,
  adjust the section, or change internal links.
- If the instruction says to delete the article, return changes=["delete"] and leave other
  fields unchanged.
- The text is for the end visitor. Remove any service/technical content: keyword lists,
  tag lists, comments, instructions to the editor, SEO notes, visible markup.
- Do NOT insert booking buttons or promotional call-to-action blocks.
- Do NOT repeat the headline: "edited_text" must NOT start with the title or any H1
  version of it. The title lives only in the "title" field; start edited_text directly
  with the body (a lead paragraph or a subheading).
- You may add up to 2 natural internal links to relevant services from the provided list,
  only where contextually appropriate.
- Choose the best matching journal section from the provided list; return its id.

Return STRICTLY JSON without markdown wrappers or explanations. Schema:
{
  "title": "article headline, ≤70 chars, attractive",
  "seo_title": "SEO title, ≤60 chars, must contain Calgary",
  "seo_description": "SEO description, ≤160 chars, must contain Calgary",
  "edited_text": "full edited HTML article text",
  "section_id": "int — id of the best matching section from the list",
  "changes": ["2-5 short items in English describing what was changed"]
}"""


class AIError(Exception):
    """Ошибка обращения к OpenRouter или парсинга ответа."""


def _services_block(services: list[tuple[str, str]]) -> str:
    return "\n".join(f"- {title}: {link}" for title, link in services)


def _sections_block(sections: list[tuple[int, str]]) -> str:
    return "\n".join(f"- {sid}: {name}" for sid, name in sections)


def _post_chat(api_key: str, model: str, system_prompt: str, user_message: str) -> str:
    """POST к OpenRouter; возвращает content первого choice."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }
    if model in JSON_RESPONSE_MODELS:
        body["response_format"] = {"type": "json_object"}
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=body,
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    tokens = data.get("usage", {}).get("total_tokens", "?")
    logger.debug("OpenRouter %s: ответ получен, токенов=%s", model, tokens)
    return data["choices"][0]["message"]["content"]


def _call_openrouter(
    api_key: str,
    system_prompt: str,
    user_message: str,
) -> dict:
    """Единая точка вызова OpenRouter. Возвращает распарсенный JSON.

    Пиковый гейт и fallback — только для DeepSeek-моделей (у DeepSeek льготный
    ночной тариф): в пик запрос сразу уходит на FALLBACK_MODEL. При сбое
    основной модели тоже пробуем fallback.
    """
    try:
        if MODEL.startswith("deepseek") and is_peak(datetime.now(timezone.utc)):
            logger.warning("DeepSeek пиковые часы — fallback на %s", FALLBACK_MODEL)
            content = _post_chat(api_key, FALLBACK_MODEL, system_prompt, user_message)
        else:
            content = _post_chat(api_key, MODEL, system_prompt, user_message)
    except requests.RequestException as exc:
        if MODEL == FALLBACK_MODEL:
            logger.error("OpenRouter API error: %s", exc)
            raise AIError(f"Ошибка OpenRouter API: {exc}") from exc
        logger.warning("Модель %s недоступна (%s) — fallback на %s", MODEL, exc, FALLBACK_MODEL)
        try:
            content = _post_chat(api_key, FALLBACK_MODEL, system_prompt, user_message)
        except requests.RequestException as exc2:
            logger.error("OpenRouter API error (fallback): %s", exc2)
            raise AIError(f"Ошибка OpenRouter API: {exc2}") from exc2

    for attempt in (1, 2):
        try:
            parsed = json.loads(content)
            break
        except json.JSONDecodeError:
            logger.warning("OpenRouter: невалидный JSON (попытка %d/2)", attempt)
            if attempt == 2:
                raise AIError("OpenRouter вернул невалидный JSON")
            content = _repair_json(content)
    else:  # pragma: no cover
        raise AIError("OpenRouter вернул невалидный JSON")

    required = {"title", "seo_title", "seo_description", "edited_text", "changes", "section_id"}
    if not required.issubset(parsed.keys()):
        raise AIError(f"OpenRouter вернул JSON без обязательных полей: {sorted(required - parsed.keys())}")
    logger.debug("OpenRouter: JSON распарсен — title «%s», edited %d симв.",
                 parsed["title"], len(parsed["edited_text"]))
    return parsed


def edit_article(
    text: str,
    user_command: str | None,
    services: list[tuple[str, str]],
    sections: list[tuple[int, str]],
    api_key: str,
) -> dict:
    """Прогнать статью через OpenRouter. Возвращает dict из JSON-схемы."""
    logger.debug(
        "edit_article: текст %d симв., услуг %d, разделов %d, команда=%r",
        len(text), len(services), len(sections), user_command,
    )

    user_message = (
        f"Journal sections:\n{_sections_block(sections)}\n\n"
        f"Services available for internal links:\n{_services_block(services)}\n\n"
        f"Article text:\n\n{text}"
    )
    if user_command:
        user_message += f"\n\nEditor instruction: {user_command}"

    return _call_openrouter(api_key, SYSTEM_PROMPT, user_message)


def edit_existing(
    current_text: str,
    current_title: str,
    instruction: str,
    services: list[tuple[str, str]],
    sections: list[tuple[int, str]],
    api_key: str,
) -> dict:
    """Применить правку к существующей статье. Возвращает dict из JSON-схемы."""
    logger.debug(
        "edit_existing: current %d симв., instruction %d симв.",
        len(current_text), len(instruction),
    )

    user_message = (
        f"Journal sections:\n{_sections_block(sections)}\n\n"
        f"Services available for internal links:\n{_services_block(services)}\n\n"
        f"Current article title: {current_title}\n"
        f"Current article text:\n\n{current_text}\n\n"
        f"Revision instruction:\n{instruction}"
    )

    return _call_openrouter(api_key, EDIT_SYSTEM_PROMPT, user_message)


def _repair_json(content: str) -> str:
    """Минимальный ремонт: обрезать markdown-обёртку ```json ... ```."""
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        return stripped.strip()
    return content
