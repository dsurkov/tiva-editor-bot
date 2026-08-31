"""Авторизация бота: allowlist + супер-админ, хранилище data/auth.json.

Формат файла:
{
  "admins": [123456789],
  "allowed": [987654321],
  "pending": {"987654321": {"username": "johndoe", "requested_at": "..."}}
}
Запись атомарная: временный файл + os.replace.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("auth")

DEFAULT_AUTH = {"admins": [], "allowed": [], "pending": {}}


class AuthStore:
    def __init__(self, path: str | Path = "data/auth.json") -> None:
        self.path = Path(path)

    def _read(self) -> dict:
        if not self.path.exists():
            return {
                "admins": [],
                "allowed": [],
                "pending": {},
            }
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("admins", [])
        data.setdefault("allowed", [])
        data.setdefault("pending", {})
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # --- чтение ---
    def is_admin(self, user_id: int) -> bool:
        return user_id in self._read()["admins"]

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self._read()["allowed"]

    def is_pending(self, user_id: int) -> bool:
        return str(user_id) in self._read()["pending"]

    def list_allowed(self) -> list[int]:
        return list(self._read()["allowed"])

    def list_pending(self) -> dict[str, dict]:
        return dict(self._read()["pending"])
    def list_admins(self) -> list[int]:
        return list(self._read()["admins"])

    # --- запись ---
    def ensure_admin_by_username(self, user_id: int, username: str | None, admin_usernames: frozenset[str]) -> bool:
        """Если @username в списке ADMIN_USERNAMES (env) — сделать админом. True, если стал админом."""
        if not username or not admin_usernames:
            return False
        if username.lower().lstrip("@") not in admin_usernames:
            return False
        data = self._read()
        if user_id not in data["admins"]:
            data["admins"].append(user_id)
            self._write(data)
        return True

    def add_allowed(self, user_id: int) -> None:
        data = self._read()
        if user_id not in data["allowed"]:
            data["allowed"].append(user_id)
        data["pending"].pop(str(user_id), None)
        self._write(data)

    def remove_user(self, user_id: int) -> None:
        data = self._read()
        if user_id in data["allowed"]:
            data["allowed"].remove(user_id)
        data["pending"].pop(str(user_id), None)
        self._write(data)

    def add_pending(self, user_id: int, username: str | None) -> None:
        data = self._read()
        data["pending"][str(user_id)] = {
            "username": username or "",
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(data)

    def resolve_user(self, key: str) -> int | None:
        """Разрешить аргумент команды /allow|/remove: id или @username."""
        data = self._read()
        key = key.strip().lstrip("@").lower()
        if key.isdigit():
            return int(key)
        for uid, rec in data["pending"].items():
            if rec.get("username", "").lower().lstrip("@") == key:
                return int(uid)
        return None
