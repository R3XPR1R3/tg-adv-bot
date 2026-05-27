# Olden Era data scrapers

## `scrape_paradrew.py` — основной скрапер (paradrew.com)

Тащит структурированные данные раздела **Heroes of Might and Magic: Olden Era**
с `paradrew.com` (русский справочник от стримера `paradrew101`). Сайт открыт без
Cloudflare, поэтому работаем простым `urllib`.

```bash
python scripts/scrape_paradrew.py            # полный прогон
python scripts/scrape_paradrew.py --limit 3  # smoke-тест
```

### Что выгружается (в `scripts/data/`)

| Файл | Что внутри |
|---|---|
| `units.json`   | 62 юнита × 3 формы (базовая / улучшение / альт. улучшение); статы, способности, теги, лор |
| `heroes.json`  | 108 героев: фракция, класс, девиз, стартовые статы, армия, доступные навыки с шансами |
| `skills.json`  | 30 навыков с тирами `basic/advanced/expert`, под-перками, списком героев |
| `spells.json`  | 88 заклинаний × 4 уровня (мана, описание, механики — Цель/Область/Длительность) |
| `laws.json`    | 193 закона (фракционные перки) с `data-*` метаданными — фракция, тир, ветка, стоимость, max уровень |
| `guides.json`  | 8 PvP-гайдов (полный текст + структура) |
| `_index.json`  | каунты и список файлов |

Каждая запись содержит:
- структурированные поля (где спарсилось);
- `sections` — иерархия h1→h6 со списками/таблицами/изображениями (на случай, если структурное поле что-то упустило);
- `_full_text` — весь текст страницы со снятой разметкой (safety net — гарантия, что ничего не потерялось);
- `_images` — все картинки с alt.

### Источник истины: paradrew

В wikitable от Hooded Horse и на paradrew статы расходятся (≈64% совпадение на
выборке 31 юнита — разные снапшоты патчей Early Access). Для этого проекта
выбран **paradrew как канон** — у него консистентная структура страниц,
свежее обновление и активное сообщество.

## `scrape_olden_era.py` — Playwright-скрапер для wiki.hoodedhorse.com (deprecated)

Каркас на Playwright. **Не работает** из этого окружения: Cloudflare Turnstile
блокирует headless Chromium из дата-центрового IP. Оставлен в репо как
референс — может пригодиться при запуске с реальной машины (Cloudflare там
обычно проходит).

## Зависимости

`scrape_paradrew.py` использует только stdlib (`urllib`) — ничего ставить
не нужно. `scrape_olden_era.py` требует `pip install -r requirements.txt` +
`python -m playwright install chromium`.
