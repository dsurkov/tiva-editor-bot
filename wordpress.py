"""Асинхронный REST-клиент WordPress (httpx, Basic Auth по Application Password)."""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("wordpress")

TIMEOUT = 30.0


class WordPressError(Exception):
    """Ошибка WordPress REST API (non-2xx)."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        message = body if isinstance(body, str) else str(body)
        super().__init__(f"WordPress API {status}: {message}")


class WordPressClient:
    def __init__(self, base_url: str, username: str, app_password: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=(username, app_password),
            timeout=TIMEOUT,
            headers={"User-Agent": "tiva-editor-bot/1.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]] | dict[str, Any]:
        resp = await self._client.get(path, params=params)
        logger.debug("GET %s params=%s → %d", path, params, resp.status_code)
        if resp.status_code >= 300:
            raise WordPressError(resp.status_code, resp.text)
        return resp.json()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = await self._client.request(method, path, **kwargs)
        logger.debug("%s %s → %d", method, path, resp.status_code)
        if resp.status_code >= 300:
            raise WordPressError(resp.status_code, resp.text)
        return resp.json()

    async def get_sections(self) -> list[tuple[int, str]]:
        """Список разделов журнала: [(id, name)]."""
        data = await self._get("/wp-json/wp/v2/article-section", {"per_page": 100})
        return [(int(item["id"]), str(item["name"])) for item in data]

    async def get_services(self) -> list[tuple[str, str]]:
        """Список услуг для перелинковки: [(title, link)]."""
        data = await self._get(
            "/wp-json/wp/v2/services",
            {"per_page": 100, "_fields": "id,title,slug,link"},
        )
        return [(str(item["title"]["rendered"]), str(item["link"])) for item in data]

    async def get_post_dates(self) -> list[tuple[str, str]]:
        """Даты (YYYY-MM-DD) записей журнала во всех статусах: [(date, status)]."""
        data = await self._get(
            "/wp-json/wp/v2/journal",
            {"status": "publish,future,draft,pending,private", "per_page": 100, "_fields": "id,date,status"},
        )
        return [(str(item["date"])[:10], str(item["status"])) for item in data]

    async def get_post_date_counts(self) -> dict[str, int]:
        """{YYYY-MM-DD: количество постов, НАД КОТОРЫМИ работали в этот день} по всем статусам.

        Группировка по `modified` (дата создания/редактирования поста) — это день,
        когда автор создавал посты, а не дата публикации. С пагинацией.
        """
        counts: dict[str, int] = {}
        page = 1
        while True:
            resp = await self._client.get(
                "/wp-json/wp/v2/journal",
                params={
                    "status": "publish,future,draft,pending,private",
                    "per_page": 100, "page": page, "_fields": "id,modified",
                },
            )
            logger.debug("GET post-work-counts page=%d → %d", page, resp.status_code)
            if resp.status_code >= 300:
                raise WordPressError(resp.status_code, resp.text)
            data = resp.json()
            if not data:
                break
            for item in data:
                d = str(item["modified"])[:10]
                counts[d] = counts.get(d, 0) + 1
            total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
            if page >= total_pages:
                break
            page += 1
        return counts

    async def get_articles_modified_on_day(self, day: str) -> list[dict[str, Any]]:
        """Посты, над которыми работали в день day (modified в этот день)."""
        data = await self._get(
            "/wp-json/wp/v2/journal",
            {
                "status": "publish,future,draft,pending",
                "modified_after": f"{day}T00:00:00",
                "modified_before": f"{day}T23:59:59",
                "per_page": 100,
                "_fields": "id,title,date,status,link",
            },
        )
        return [
            {
                "id": int(item["id"]),
                "title": str(item["title"]["rendered"] or item["title"]["raw"]),
                "date": str(item["date"]),
                "status": str(item["status"]),
                "link": str(item.get("link", "")),
            }
            for item in data
        ]

    async def get_post_raw(self, post_id: int) -> dict[str, Any]:
        """Полные данные поста (context=edit): content.raw, meta, media, sections."""
        data = await self._get(
            f"/wp-json/wp/v2/journal/{post_id}",
            {"context": "edit", "_fields": "id,title,content,date,status,featured_media,meta,article-section,link"},
        )
        assert isinstance(data, dict)
        return data

    async def get_articles_on_day(self, day: str) -> list[dict[str, Any]]:
        """Записи журнала за конкретный день (YYYY-MM-DD): [{id, title, date, status, link}]."""
        data = await self._get(
            "/wp-json/wp/v2/journal",
            {
                "status": "publish,future,draft,pending",
                "after": f"{day}T00:00:00",
                "before": f"{day}T23:59:59",
                "per_page": 50,
                "_fields": "id,title,date,status,link",
            },
        )
        return [
            {
                "id": int(item["id"]),
                "title": str(item["title"]["rendered"] or item["title"]["raw"]),
                "date": str(item["date"]),
                "status": str(item["status"]),
                "link": str(item.get("link", "")),
            }
            for item in data
        ]

    async def upload_media(
        self, file_bytes: bytes, filename: str, content_type: str
    ) -> tuple[int, str]:
        """Загрузка медиафайла, возвращает (attachment id, source_url)."""
        files = {"file": (filename, file_bytes, content_type)}
        resp = await self._client.post("/wp-json/wp/v2/media", files=files)
        logger.debug(
            "POST /media file=%s type=%s size=%d → %d",
            filename, content_type, len(file_bytes), resp.status_code,
        )
        if resp.status_code >= 300:
            raise WordPressError(resp.status_code, resp.text)
        data = resp.json()
        return int(data["id"]), str(data["source_url"])

    async def create_post(
        self,
        title: str,
        content: str,
        status: str,
        date_iso: str,
        section_id: int,
        featured_media_id: int | None,
        seo_title: str,
        seo_description: str,
    ) -> tuple[int, str]:
        """Создание записи журнала. Возвращает (id, link)."""
        body: dict[str, Any] = {
            "title": title,
            "content": content,
            "status": status,
            "date": date_iso,
            "article-section": [section_id],
            "meta": {
                "slim_seo": {"title": seo_title, "description": seo_description},
                "_slim_seo_primary_term_article-section": section_id,
            },
        }
        if featured_media_id is not None:
            body["featured_media"] = featured_media_id
        logger.debug(
            "POST /journal status=%s date=%s section=%s media=%s",
            status, date_iso, section_id, featured_media_id,
        )
        data = await self._request("POST", "/wp-json/wp/v2/journal", json=body)
        return int(data["id"]), str(data["link"])

    async def update_post(self, post_id: int, body: dict[str, Any]) -> tuple[int, str]:
        """Обновление записи журнала (PUT). Возвращает (id, link)."""
        logger.debug("PUT /journal/%s fields=%s", post_id, sorted(body.keys()))
        data = await self._request("PUT", f"/wp-json/wp/v2/journal/{post_id}", json=body)
        return int(data["id"]), str(data["link"])

    async def delete_post(self, post_id: int) -> bool:
        """Удаление записи журнала (force)."""
        data = await self._request("DELETE", f"/wp-json/wp/v2/journal/{post_id}", params={"force": True})
        return bool(data.get("deleted", False))
