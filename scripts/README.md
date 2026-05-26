# scrape_olden_era.py

Скрипт выгружает раздел **Heroes of Might and Magic: Olden Era** с
`wiki.hoodedhorse.com` в один JSON-файл со структурой:

```json
{
  "meta": { "source": "...", "scraped_at": "...", "counts": {...} },
  "heroes":   [...],
  "units":    [...],
  "spells":   [...],
  "skills":   [...],
  "factions": [...],
  "artifacts":[...],
  "buildings":[...],
  "other":    [...]
}
```

## Установка

Требуется Python 3.10+.

```bash
pip install -r scripts/requirements.txt
python -m playwright install chromium
# Linux: для headless-Chromium ещё нужны системные библиотеки —
# либо `python -m playwright install-deps chromium` (нужен sudo),
# либо ставить пакеты руками (libnss3, libnspr4, libatk1.0, libcups2,
# libdrm2, libxkbcommon0, libxcomposite1, libxdamage1, libxfixes3,
# libxrandr2, libgbm1, libasound2, libpango-1.0, libcairo2).
```

## Запуск

```bash
# полный прогон — discover, fetch, parse, normalize
python scripts/scrape_olden_era.py --phase all

# проверочный мини-прогон — только 5 URL
python scripts/scrape_olden_era.py --phase discover --limit 5
python scripts/scrape_olden_era.py --phase fetch --limit 5
python scripts/scrape_olden_era.py --phase parse
python scripts/scrape_olden_era.py --phase normalize
```

Опции:

- `--cache-dir DIR` — куда складывать промежуточные файлы (по умолчанию
  `scripts/cache/`)
- `--out PATH`     — финальный JSON (по умолчанию `scripts/olden_era.json`)
- `--delay SEC`    — пауза между запросами, по умолчанию 1.5
- `--limit N`      — обрабатывать максимум N URL (для smoke-теста)
- `-v`             — подробные логи

## Как это работает

Сайт защищён Cloudflare interactive challenge — обычный `requests` не работает.
Поэтому используется headless Chromium через Playwright, который проходит
JS-челлендж сам. Скрипт работает в четырёх фазах с кешем на диске, чтобы при
ошибках парсинга не дёргать сайт повторно:

1. **discover** — BFS по внутренним ссылкам раздела, копит список URL в
   `cache/urls.json`.
2. **fetch** — рендерит каждую страницу через Playwright и кладёт HTML в
   `cache/html/<hash>.html`. Возобновляемая.
3. **parse** — превращает все HTML в `cache/raw_pages.json` (общая структура:
   infobox, таблицы, секции, ссылки, категории).
4. **normalize** — классифицирует страницы (герой / юнит / заклинание / навык /
   фракция / артефакт / постройка / другое) и пишет финальный
   `olden_era.json`.

## Замечание про robots.txt

В `robots.txt` сайта стоят content-signals: `search=yes`, `ai-train=no`.
Извлечение игровых статов в JSON для личного использования — не AI-training,
формально под `ai-train=no` не подпадает. Но имей в виду, что сайт защищён
Cloudflare и явно не приветствует автоматизацию: ставь `--delay` не ниже
дефолтных 1.5 сек.
