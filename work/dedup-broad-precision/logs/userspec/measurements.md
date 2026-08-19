# Замеры на проде, на которых стоит user-spec

Все цифры получены на живой прод-БД (`45.90.216.165`, `/data/news.db`) и живом
парсере 19.08.2026. Здесь сохранены и результаты, и способ их воспроизвести —
чтобы тех-спека не переизмеряла с нуля и чтобы любую цифру можно было оспорить.

---

## Замер 1. Масштаб проблемы и счёт нового правила

**Источник данных:** ключи `softflag_pair:` в таблице `bot_state` (постоянная
запись каждого софт-флага) + отпечатки из `published_articles` /
`pending_articles`.

**Что померено:** 31 софт-флаг за полтора месяца. У 22 уцелели отпечатки обеих
сторон — остальные 9 вычищены вместе со строками отменённых статей.

**Ключевое наблюдение:** все 22 сработали при пересечении **0–7 %** (медиана
~2 %). Документированный порог ветки set-overlap — 30–49 %, то есть по нему не
прошёл НИ ОДИН. Все пришли по ветке broad-пары, где порога нет вовсе.

**Статьи-хабы** (сколько раз стали «дублем» / размер отпечатка):

| статья | пар | флагов |
|---|---|---|
| Unboxing: 10 Affordable Cars for July | 296 | 4 |
| 5 Unsung Hot Wheels Heroes | 99 | 4 |
| Last Car Culture Set for 2026 | 56 | 6 |
| Premium Collector Set Is a Mooneyes Diorama | 40 | 4 |
| 11 New Hot Wheels Collectibles to Start Hunting | 28 | 3 |
| New Hot Wheels Pop Culture Set Is Coming | 16 | 3 |

Эти шесть статей участвуют в 24 флагах из 31 — то есть дают почти весь мусор.

**Примеры того, что признавалось дублем** (полный список — в выводе запроса ниже):

| статья A | статья B | по какой паре совпали |
|---|---|---|
| New Hot Wheels Mercedes-Benz 6x6 Will Likely Sell Like Hotcakes | 5 Unsung Hot Wheels Heroes | `porsche 911 gt2 evo|red line club|B` |
| 2026 Hot Wheels Case Q Unboxing: STH Is a Pontiac | Premium Collector Set Is a Mooneyes Diorama | `mini series|boulevard|B` |
| 2026 Hot Wheels Collectors Convention Malaysia | Last Car Culture Set for 2026 | `ferrari 250 gto|team transport|B` |
| New Hot Wheels RLC Exclusive '21 Ford Bronco | 11 New Hot Wheels Collectibles | `ferrari f40|red line club|B` |

Ни в одном случае общая линия не является предметом обеих статей — это
упоминание внутри перечисления.

**Счёт правила «линия названа в заголовках обеих статей»:**

| | сейчас | с правилом |
|---|---|---|
| настоящих дублей поймано | 3 из 3 | 3 из 3 |
| ложных флагов | 21 | 1 |

Вердикты оператора получены 19.08: пара «Novo lote da série Car Culture»
(t-hunted 21.07) × «Last Car Culture Set for 2026» (autoevolution 18.07) —
**дубли**; пара «Car Culture 2-Pack Mix 4» × тот же хаб — **не дубли**.

Два дубля вне этих 22 (отпечатки вычищены, заголовки взяты из
`processed_news`): 14.08 `team transport` в обоих заголовках, 15.08 `boulevard`
в обоих — правило ловит оба.

**Поправка к базовой линии.** «Статья опубликована» ≠ «дублем не была». Пара
Car Culture вышла обеими сторонами и является настоящим дублем: система уже
пропустила дубль в канал, потому что пинг утонул в шуме.

### Воспроизведение

```python
# на прод-хосте: docker exec -i hw-news-bot python3 -
import sqlite3, json
c = sqlite3.connect("/data/news.db"); c.row_factory = sqlite3.Row
fp, titles = {}, {}
for t in ("published_articles", "pending_articles"):
    for r in c.execute(f"SELECT link, title, model_fingerprint FROM {t}"):
        d = json.loads(r["model_fingerprint"]) if r["model_fingerprint"] else None
        if isinstance(d, dict): fp[r["link"]] = d
        titles[r["link"]] = r["title"] or ""
rows = [k[len("softflag_pair:"):].split("\n")
        for k, in c.execute("SELECT key FROM bot_state WHERE key LIKE 'softflag_pair:%'")]
# правило: серия общей пары названа в заголовках ОБЕИХ статей
```

---

## Замер 2. Страховка первым абзацем инертна

**Зачем мерили:** счёт замера 1 получен на правиле, читающем ТОЛЬКО заголовок.
Страховку первым абзацем добавили после, и она расширяет область поиска — есть
риск вернуть часть ложных срабатываний, ради устранения которых всё затевалось.

**Как мерили:** шесть постов t-hunted из корпуса прогнаны живым парсером
`news_bot.fetch_full_article({'link': ...})`, затем `model_extractor.extract_series()`
отдельно по заголовку и по первому абзацу.

| статья | серии в заголовке | серии в 1-м абзаце | добавила страховка |
|---|---|---|---|
| Mais fotos do lote da série Boulevard com a inédita Enzo | `boulevard` | `boulevard` | — |
| Mais fotos de dois lotes da série Team Transport | `team transport` | — | — |
| Novo lote da série Car Culture, carros alemães | `car culture` | — | — |
| Mais um novo lote da série Pop Culture | `pop culture` | — | — |
| A inédita Ferrari F40 no Red Line Club | `red line club` | — | — |
| O carro exclusivo do Red Line Club para o Natal | `red line club` | `red line club` | — |

**Результат: ноль новых серий во всех шести случаях.**

**Два вывода:**

1. Счёт замера 1 остаётся в силе для финального правила — страховка ложных не
   возвращает.
2. Страховка **инертна на всех доступных данных**: в реальных постах t-hunted
   линия всегда уже названа в заголовке. Оставлена как защита от будущей формы
   поста, но она НЕ несущая часть правила, и тест на неё будет синтетическим.

**Побочно подтверждено:** португальский работает без отдельной поддержки —
`extract_series("Novo lote da série Car Culture")` → `['car culture']`, потому
что Mattel названия линий не переводит.

---

## Замер 3. Объём правки тестов

`tests/test_integration.py`: хелпер `_seed_published` (:1100) по умолчанию сеет
кандидату `title='Existing Article'`, никак не связанный с подставляемыми
`pairs`. **25 вызовов** при 37 упоминаниях `pairs`. Под новым правилом такие
кандидаты провалят проверку предмета — тесты, ожидающие флаг, перевернутся.
Это самая объёмная часть работ по фиче.
