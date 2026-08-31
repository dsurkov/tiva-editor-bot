"""Редактура статьи через DeepSeek (deepseek-v4-flash, не-thinking, JSON)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from openai import AsyncOpenAI

from peak import is_peak

logger = logging.getLogger("ai_editor")

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
- You may add up to 2 natural internal links to relevant services from the provided list,
  only where contextually appropriate (format: anchor text with href).
- Choose the best matching journal section from the provided list; return its id.

Return STRICTLY JSON without markdown wrappers or explanations. Schema:
{{
  "title": "article headline, ≤70 chars, attractive",
  "seo_title": "SEO title, ≤60 chars, must contain Calgary",
  "seo_description": "SEO description, ≤160 chars, must contain Calgary",
  "edited_text": "full edited HTML article text",
  "section_id": "int — id of the best matching section from the list",
  "changes": ["2-5 short items in English describing what was changed"]
}}"""

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
- You may add up to 2 natural internal links to relevant services from the provided list,
  only where contextually appropriate.
- Choose the best matching journal section from the provided list; return its id.

Return STRICTLY JSON without markdown wrappers or explanations. Schema:
{{
  "title": "article headline, ≤70 chars, attractive",
  "seo_title": "SEO title, ≤60 chars, must contain Calgary",
  "seo_description": "SEO description, ≤160 chars, must contain Calgary",
  "edited_text": "full edited HTML article text",
  "section_id": "int — id of the best matching section from the list",
  "changes": ["2-5 short items in English describing what was changed"]
}}"""


class PeakTimeError(Exception):
    """Сейчас пиковые часы DeepSeek — AI-функции недоступны."""


class AIError(Exception):
    """Ошибка обращения к DeepSeek или парсинга ответа."""


def _services_block(services: list[tuple[str, str]]) -> str:
    return "\n".join(f"- {title}: {link}" for title, link in services)


def _sections_block(sections: list[tuple[int, str]]) -> str:
    return "\n".join(f"- {sid}: {name}" for sid, name in sections)


async def _call_deepseek(
    api_key: str,
    system_prompt: str,
    user_message: str,
) -> dict:
    """Единая точка вызова DeepSeek (после гейта пика). Возвращает распарсенный JSON."""
    client = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    try:
        response = await client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
        tokens = response.usage.total_tokens if response.usage else "?"
        logger.debug("DeepSeek: ответ получен, токенов=%s", tokens)
    except Exception as exc:  # noqa: BLE001 — пробрасываем как AIError
        logger.error("DeepSeek API error: %s", exc)
        raise AIError(f"Ошибка DeepSeek API: {exc}") from exc

    content = response.choices[0].message.content
    for attempt in (1, 2):
        try:
            parsed = json.loads(content)
            break
        except json.JSONDecodeError:
            logger.warning("DeepSeek: невалидный JSON (попытка %d/2)", attempt)
            if attempt == 2:
                raise AIError("DeepSeek вернул невалидный JSON")
            content = _repair_json(content)
    else:  # pragma: no cover
        raise AIError("DeepSeek вернул невалидный JSON")

    required = {"title", "seo_title", "seo_description", "edited_text", "changes", "section_id"}
    if not required.issubset(parsed.keys()):
        raise AIError(f"DeepSeek вернул JSON без обязательных полей: {sorted(required - parsed.keys())}")
    logger.debug("DeepSeek: JSON распарсен — title «%s», edited %d симв.",
                 parsed["title"], len(parsed["edited_text"]))
    return parsed


async def edit_article(
    text: str,
    user_command: str | None,
    services: list[tuple[str, str]],
    sections: list[tuple[int, str]],
    api_key: str,
) -> dict:
    """Прогнать статью через DeepSeek. Возвращает dict из JSON-схемы.

    ЕДИНСТВЕННАЯ точка гейта пика — все DeepSeek-функции проходят через неё.
    """
    if is_peak(datetime.now(timezone.utc)):
        logger.info("edit_article: пиковые часы — отказ (текст %d симв.)", len(text))
        raise PeakTimeError(
            "Сейчас пиковые часы DeepSeek (01:00–04:00 и 06:00–10:00 UTC, пн–пт)."
        )
    logger.debug(
        "edit_article: непик, текст %d симв., услуг %d, разделов %d, команда=%r",
        len(text), len(services), len(sections), user_command,
    )

    user_message = (
        f"Journal sections:\n{_sections_block(sections)}\n\n"
        f"Services available for internal links:\n{_services_block(services)}\n\n"
        f"Article text:\n\n{text}"
    )
    if user_command:
        user_message += f"\n\nEditor instruction: {user_command}"

    return await _call_deepseek(api_key, SYSTEM_PROMPT, user_message)


async def edit_existing(
    current_text: str,
    current_title: str,
    instruction: str,
    services: list[tuple[str, str]],
    sections: list[tuple[int, str]],
    api_key: str,
) -> dict:
    """Применить правку к существующей статье. Возвращает dict из JSON-схемы."""
    if is_peak(datetime.now(timezone.utc)):
        logger.info("edit_existing: пиковые часы — отказ")
        raise PeakTimeError(
            "Сейчас пиковые часы DeepSeek (01:00–04:00 и 06:00–10:00 UTC, пн–пт)."
        )
    logger.debug(
        "edit_existing: непик, current %d симв., instruction %d симв.",
        len(current_text), len(instruction),
    )

    user_message = (
        f"Journal sections:\n{_sections_block(sections)}\n\n"
        f"Services available for internal links:\n{_services_block(services)}\n\n"
        f"Current article title: {current_title}\n"
        f"Current article text:\n\n{current_text}\n\n"
        f"Revision instruction:\n{instruction}"
    )

    return await _call_deepseek(api_key, EDIT_SYSTEM_PROMPT, user_message)


def _repair_json(content: str) -> str:
    """Минимальный ремонт: обрезать markdown-обёртку ```json ... ```."""
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        return stripped.strip()
    return content
