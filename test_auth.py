"""Юнит-тесты хранилища авторизации."""
import json
from pathlib import Path

from auth import AuthStore


def _fresh_store(tmp_path: Path) -> AuthStore:
    return AuthStore(tmp_path / "data" / "auth.json")


def test_bootstrap_admin_by_username(tmp_path):
    store = _fresh_store(tmp_path)
    assert store.is_admin(1) is False
    assert store.ensure_admin_by_username(1, "OwnerNick", frozenset({"ownernick"})) is True
    assert store.is_admin(1) is True
    # повторный вызов не дублирует
    assert store.ensure_admin_by_username(1, "ownernick", frozenset({"ownernick"})) is True
    data = store._read()
    assert data["admins"] == [1]
    # чужой ник не делает админом
    assert store.ensure_admin_by_username(2, "hacker", frozenset({"ownernick"})) is False
    assert store.is_admin(2) is False


def test_pending_and_allow_flow(tmp_path):
    store = _fresh_store(tmp_path)
    store.add_pending(999, "johndoe")
    assert store.is_pending(999) is True
    assert store.list_pending()["999"]["username"] == "johndoe"

    store.add_allowed(999)
    assert store.is_allowed(999) is True
    assert store.is_pending(999) is False
    assert 999 in store.list_allowed()


def test_remove_user(tmp_path):
    store = _fresh_store(tmp_path)
    store.add_allowed(999)
    store.add_pending(888, "bob")
    store.remove_user(999)
    store.remove_user(888)
    assert store.is_allowed(999) is False
    assert store.is_pending(888) is False


def test_resolve_user(tmp_path):
    store = _fresh_store(tmp_path)
    store.add_pending(777, "alice")
    assert store.resolve_user("777") == 777
    assert store.resolve_user("@alice") == 777
    assert store.resolve_user("alice") == 777
    assert store.resolve_user("nobody") is None


def test_atomic_write(tmp_path):
    store = _fresh_store(tmp_path)
    store.add_allowed(42)
    # файл читается как валидный JSON и содержит id
    raw = json.loads((tmp_path / "data" / "auth.json").read_text(encoding="utf-8"))
    assert 42 in raw["allowed"]
    # временных файлов не остаётся
    leftovers = [p for p in (tmp_path / "data").iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        test_bootstrap_admin_by_username(Path(d))
        test_pending_and_allow_flow(Path(d))
        test_remove_user(Path(d))
        test_resolve_user(Path(d))
        test_atomic_write(Path(d))
    print("auth: все тесты OK")
