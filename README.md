# Tiva Editor Bot — Telegram-бот «Редакция»

Приватный Telegram-бот для публикации и редактирования статей журнала TIVA BEAUTY
(WordPress, CPT `journal`, таксономия `article-section`).

## Возможности

- **Сбор контента**: тексты + фото + файлы-картинки вперемешку, закреплённая кнопка
  «✅ Finish Content».
- **AI-редактура** (DeepSeek `deepseek-v4-flash`): статья строго на английском
  (перевод с русского), без кнопок бронирования и служебной информации, только
  внутренняя перелинковка на услуги; раздел журнала AI выбирает сам.
- **Гейт пика DeepSeek**: пн–пт 01:00–04:00 и 06:00–10:00 UTC — AI-функции
  недоступны, остальное работает.
- **Публикация**: инлайн-календарь (можно выбрать любой день, вперёд и назад,
  отметки 📄 published / 🟢 future / 📝 draft / ⏳ pending / 🔒 private),
  время — первая половина дня [08:00–12:00) Edmonton; сегодня после обеда → сразу.
- **Edit Article**: календарь → день → список статей (кнопка «🌐 Открыть в браузере») →
  правка / новый текст / фото / удаление.
- **Авторизация**: allowlist + супер-админ по нику (`data/auth.json`), админ-команды.
- **Дебаг-логи**: `LOG_LEVEL` env + `/debug on|off`.

## Секреты (SOPS + age)

Секреты зашифрованы в репозитории через [SOPS](https://github.com/getsops/sops)
с age-ключом и НЕ хранятся в открытом виде:

- `.env.sops.yaml` — токен бота, DeepSeek, WP creds (`sops -d` → `.env`).
- `.deploy.sops.yaml` — host/user сервера для GitHub Actions.

Приватный age-ключ (`.age-key.txt`) никогда не коммитится; он хранится у владельца
и в GitHub Secrets (`AGE_SECRET_KEY`). Деплой расшифровывает файлы на лету.

### Расшифровать локально (редактирование секретов)

```bash
# правка .env
SOPS_AGE_KEY_FILE=.age-key.txt sops .env.sops.yaml   # откроет расшифрованную версию в $EDITOR, сохранит шифром

# или вручную:
SOPS_AGE_KEY_FILE=.age-key.txt sops -d --input-type dotenv --output-type dotenv .env.sops.yaml
```

### GitHub Secrets (нужны для деплоя)

| Secret | Назначение |
|---|---|
| `AGE_SECRET_KEY` | приватный age-ключ (содержимое `.age-key.txt`) |
| `EU_ROOT_SSH_KEY` | приватный SSH-ключ для доступа к серверу (deploy key) |

## Деплой

GitHub Actions (`.github/workflows/deploy.yml`) по push в `main`:
1. расшифровывает `.env` и host/user через sops+age;
2. rsync кода на `SERVER_HOST:SERVER_USER` (в `/opt/telegram-wp-bot/`);
3. загружает `.env` на сервер;
4. `docker compose up -d --build`.

## Локальные тесты

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest -q
```
