# Archived: Facebook Hot Wheels Source

**Дата архивации:** 2026-04-20

## Причина отказа

Фича "Facebook Hot Wheels как источник новостей" отменена после исследования доступности Facebook API и публичного HTML.

### Что выяснилось
- **RSS** (`facebook.com/hotwheels/rss`) — 404, Facebook давно отключил RSS для публичных страниц
- **Публичный HTML** (`facebook.com/hotwheels/`) — 302 редирект на страницу логина, весь контент требует авторизации
- **Mobile-версия** (`mbasic.facebook.com`) — тот же редирект на логин
- **Graph API без токена** — 403 `"Provide valid app ID"`; требует Facebook Developer аккаунт, создание App, прохождение ревью, и Page Access Token с обновлением каждые 60 дней
- **Instagram** (альтернатива) — аналогичная ситуация, Instagram Basic Display API полностью закрыт в декабре 2024

Вывод: получать посты с публичной Facebook-страницы без авторизации невозможно. Официальный путь через Graph API требует значительных операционных затрат (создание приложения, ревью, обновление токенов), не оправданных для задачи получения редких анонсов.

### На что заменили
- Парсинг `corporate.mattel.com/news` с фильтром по Hot Wheels (новая фича `mattel-news-source`)
- Дополнительный RSS-источник `lamleygroup.com/category/hot-wheels/feed/` (добавлен в `feeds.json`)

## Что в папке

- `facebook_source.py` — реализация загрузки конфига, RSS и Graph API fetchers (Task 1, частично Task 2)
- `facebook_source.json` — пример конфига страницы
- `test_facebook_source.py` — 17 unit-тестов с моками HTTP-запросов

Полная история работы над фичей: [../work/archived/facebook-hotwheels-source/](../work/archived/facebook-hotwheels-source/)
