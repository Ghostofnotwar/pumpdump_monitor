# GitHub main → Fly.io

Инструкция для первого деплоя и обновлений. Команды выполняются на вашем компьютере, секреты не отправляются в чат и не коммитятся.

## Архитектура и обязательные условия

- Один Fly Machine, один процесс, long polling. **Не включайте HA и не масштабируйте до двух машин.**
- `auto_stop_machines = "off"`, `min_machines_running = 1`: HTTP-запросы не должны быть условием работы бота.
- Volume `pumpdump_data` подключён к `/data`; там SQLite и PNG.
- Стратегия `immediate`: короткая остановка при обновлении вместо параллельного второго poller. Возможен конфликт polling на время замены и прогрев истории после старта.
- Регион в шаблоне — `ams`. Его можно заменить **до создания volume**; регион машины и volume должны совпадать.
- Нужны активный billing/подходящий тариф Fly и доступность API Bybit из выбранного региона. Бесплатность не предполагается.
- Один volume — не резервная копия. Настройте snapshots/внешние backup по возможностям вашего аккаунта.

## 1. Сохранить изменения в GitHub main

Работа подготовлена на ветке `arena/43efc6de-pumpdump-monitor`. Безопасный путь — PR в `main`, зелёный workflow **Checks**, затем merge через GitHub. Не копируйте `.env`, SQLite или токены в коммит.

После merge:

```bash
git checkout main
git pull --ff-only origin main
python -m pip install -r requirements-dev.txt
python -m pytest -q --timeout=30 --timeout-method=thread
```

Не выполняйте force-push в `main`. Старый `railway.json` не обеспечивает постоянный диск; не считайте его новой схемой хранения.

## 2. Подготовить Fly CLI и приложение

Установите `flyctl` официальным способом для своей ОС, затем:

```bash
fly auth login
fly apps create YOUR-UNIQUE-APP --org YOUR-ORG
```

Замените `pumpdump-monitor-replace-me` в `fly.toml` на созданное имя. Если меняете регион, измените `primary_region` до создания volume.

```bash
fly volumes create pumpdump_data --app YOUR-UNIQUE-APP --region ams --size 3 -n 1
```

Имя volume должно совпадать с `[[mounts]].source`. Не создавайте второй экземпляр приложения с тем же Telegram-токеном.

## 3. Секреты и URL

Использующийся токен можно посмотреть в BotFather. Для частного бота узнайте свой chat ID у Telegram `@userinfobot` или аналогичного доверенного инструмента; не передавайте токен сторонним сайтам.

```bash
fly secrets set BOT_TOKEN='YOUR_REAL_BOT_TOKEN' --app YOUR-UNIQUE-APP --stage
fly secrets set WEB_APP_URL='https://YOUR-UNIQUE-APP.fly.dev/webapp' --app YOUR-UNIQUE-APP --stage
# Рекомендуется для частного бота:
fly secrets set ALLOWED_CHAT_IDS='123456789' --app YOUR-UNIQUE-APP --stage
```

`--stage` применяет секреты при следующем deploy/restart. Если ваша версия CLI не поддерживает этот флаг, выполните команды без него. Не используйте `set -x` и не сохраняйте вывод секретов. `DATABASE_URL` и `SNAPSHOT_DIR` уже заданы безопасно в `fly.toml`.

Если нужен доверенный Bybit proxy, задайте `BYBIT_REST` как секрет. ứng dụng не нуждается в биржевых API-ключах.

## 4. Первый запуск

**До запуска нового poller остановите старого бота на Railway/VPS/другом Fly-приложении.** Иначе один токен будет опрашиваться из двух мест.

```bash
fly deploy --app YOUR-UNIQUE-APP --config fly.toml --remote-only --ha=false --strategy immediate
fly status --app YOUR-UNIQUE-APP
fly machine list --app YOUR-UNIQUE-APP
fly logs --app YOUR-UNIQUE-APP
```

Должна остаться **одна** машина. Если ранее были созданы две, сначала разберитесь с данными на их volumes и осознанно оставьте одну; командабез проверки  `scale count 1` может удалить нужный экземпляр. Workflow отказывается деплоить при уже существующих нескольких машинах.

Проверки:

```bash
curl https://YOUR-UNIQUE-APP.fly.dev/healthz
curl -i https://YOUR-UNIQUE-APP.fly.dev/readyz
fly ssh console --app YOUR-UNIQUE-APP -C 'id'
fly ssh console --app YOUR-UNIQUE-APP -C 'ls -ld /data /data/snapshots'
```

- `/healthz` — процесс и HTTP живы. Используется для автоматического рестарта.
- `/readyz` — SQLite + polling + свежие данные рынка. `503` сразу после старта нормален; для сигналов нужно прогреть окно, по умолчанию около 5 минут.
- Если рынок/Telegram временно недоступны, liveness остаётся `200`, чтобы не устраивать бесконечные рестарты из-за внешнего сбоя.
- `id` внутри контейнера должен показывать непривилегированного `app`, не root.
- Откройте `/webapp` кнопкой бота, не обычным браузером: без Telegram-подписи личные API закрыты.

В Telegram: `/start` → `/test` → `/settings` → `/help`. Выберите PUMP/DUMP/оба, порог и оборот. Отсутствие реального сигнала сразу после запуска не означает поломку.

## 5. Автоматический деплой из main

В GitHub: **Settings → Environments → New environment → production**.

В environment `production` задайте:

- Secret `FLY_API_TOKEN`: деплой-токен с правами только этого приложения. Создать можно через `fly tokens create deploy --app YOUR-UNIQUE-APP`; точную область действия проверьте в своей организации.
- Variable `FLY_APP_NAME`: реальное имя Fly-приложения.
- Variable `FLY_DEPLOY_ENABLED`: строка `true`.

Не кладите токен в `fly.toml`, `.env` репозитория или логи. При ротации обновите GitHub secret и отзовите старый токен.

Workflow `.github/workflows/deploy.yml`:

1. запускает тесты и Docker build;
2. проверяет конфигурацию и число машин;
3. выполняет одиночный deploy с `--ha=false`;
4. не отменяет уже начатый production deploy при новом push.

До установки `FLY_DEPLOY_ENABLED=true` автоматический деплой на push выключен. Ручной запуск: **Actions → Deploy to Fly → Run workflow**, ветка `main`. Первый деплой и создание volume рекомендуется выполнить вручную по шагам выше.

## 6. Данные и перенос

Новый volume начинает с пустой БД. После переключения пользователям нужно снова отправить `/start`; старые настройки автоматически с другого хостинга не переносятся.

Перед остановкой старого хостинга сделайте согласованную копию его SQLite:

```bash
python backup_db.py --database /path/to/old/bot.db --output ./old-bot-backup.db
```

На Fly:

```bash
fly ssh console --app YOUR-UNIQUE-APP -C 'python /app/backup_db.py'
```

Скрипт создаёт `/data/backups/bot-UTC.db` и проверяет integrity. Выгрузите копию через доступный вам Fly SFTP/SSH-инструмент и храните вне этой машины. Автоматической выгрузки и очистки backup в приложении нет.

**Не перезаписывайте работающий `/data/bot.db` скачанным файлом и не копируйте только `.db` без WAL.** Перенос существующей БД между хостингами требует отдельной остановки писателей и проверки схемы; в этой итерации автоматический импорт не реализован. Для большинства небольших установок безопаснее начать с новой БД и повторно настроить чаты.

## 7. Обновления и откат

1. Изменения проходят PR и Checks.
2. Merge в `main` запускает deploy, если он включён.
3. Проверяются `/readyz`, логи, `/test`, одна машина и наличие volume.
4. Для отката приложения выберите предыдущий успешный образ/деплой Fly. Не удаляйте volume ради отката кода.

Миграции SQLite аддитивные. Старый образ может не знать новые колонки, но не должен удалять данные. Перед значительными изменениями схемы делайте внешнюю копию.

## Диагностика

| Симптом | Проверка |
|---|---|
| Configuration error | Секреты/числовые env; не использовать пример токена |
| Telegram Conflict / два poller | Старый хостинг, локальный запуск, лишняя Fly Machine |
| `/readyz` 503, `market_data=false` | Логи Bybit, доступность региона/proxy, прогрев окна |
| `/readyz` 503, `polling=false` | Токен, сеть Telegram, логи запуска; подождать завершения старта |
| `/readyz` 503, `database=false` | Права `/data`, наличие volume, свободное место |
| Mini App просит открыть заново | Срок подписи истёк; закрыть и открыть из Telegram |
| Нет алертов | Пауза, направление, порог, оборот, лимит списка, прогрев, cooldown |
| Нет картинки, но есть текст | Временная ошибка OHLCV/рендера; сигнал всё равно доставлен |
| Пропал старый PNG | Retention; текстовая запись хранится дольше |

Логи приложения не должны содержать токен. Не публикуйте полный дамп окружения, `initData`, секреты Fly или приватную БД в issue.
