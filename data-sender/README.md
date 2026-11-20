# Workload Collector

Отправитель системных метрик Linux, **Python 3.11+**, только стандартная библиотека.
Пакет `workload-collector` для Debian 12+ / Ubuntu 24.04+ с systemd, архитектура `all`.
Приёмник FastAPI — [data-collector](../data-collector/README.md); он не входит в пакет отправителя.

## Bash CI

Скрипт находится в [`data-sender/ci.sh`](ci.sh). Команды ниже запускаются из корня репозитория. Makefile и Docker не нужны.

### Локальная проверка (включая Arch)

```bash
bash data-sender/ci.sh test
```

Скрипт компилирует исходники и тесты в байткод, запускает `unittest` отправителя и `pytest` приёмника, включая отправку отчёта по локальному HTTP и проверку записи в SQLite. Нужен Python 3.11+ с `venv`: по умолчанию зависимости тестов скачиваются в `data-collector/.ci-venv`. Если они уже установлены в `data-collector/.venv`, можно обойтись без скачивания:

```bash
CI_TEST_PYTHON=./data-collector/.venv/bin/python bash data-sender/ci.sh test
```

`test` не обновляет Git и не устанавливает системный пакет.

### Сборка и установка (Debian 12+ / Ubuntu 24.04+)

Подготовьте зависимости на целевой машине:

```bash
sudo apt-get install git python3 python3-venv dpkg
```

Для сборки `.deb` из текущих файлов без обновления Git и установки:

```bash
bash data-sender/ci.sh build
```

Пакет создаётся в `dist/` (например, `dist/workload-collector_0.1.0_all.deb`); версия берётся из [DEBIAN/control](packaging/DEBIAN/control).

Для полного цикла запустите скрипт из чистого клона, в котором `data-sender/ci.sh` уже есть, а текущая ветка отслеживает ветку Git-сервера:

```bash
bash data-sender/ci.sh full
```

`full` загружает отслеживаемую ветку, обновляет её только fast-forward, запускает компиляцию и тесты, собирает `.deb` и устанавливает его через `apt-get` (с `sudo`, если скрипт запущен не от root). При локальных изменениях, опережении или расхождении веток скрипт останавливается. Для Git нужны уже настроенные права на чтение репозитория. Пакет устанавливает только отправитель; [приёмник запускается отдельно](../data-collector/README.md#запуск).

Лицензия upstream пока не объявлена: пакет предназначен для собственного использования;
перед распространением третьим лицам необходимо определить лицензию.

## Установка готового пакета

Из корня репозитория (укажите версию своего пакета):

```bash
sudo apt install ./dist/workload-collector_0.1.0_all.deb
```

Скрипты пакета через системные хелперы Debian настраивают автозапуск, запускают службу, при обновлении перезапускают её, при удалении останавливают. Они учитывают системную политику запуска и сохраняют явно отключённый автозапуск. После установки на машине с systemd проверьте статус службы. В контейнере без запущенного systemd пакет устанавливается без запуска службы.

- Код: `/usr/lib/workload-collector/workload_collector.py`.
- Vendor-unit: `/lib/systemd/system/workload-collector.service` на Debian 12
  (на merged-/usr это тот же системный каталог через ссылку).
- **Конфиг: `/etc/data-collector/config.toml`**, dpkg conffile.
- UUID: `/var/lib/workload-collector/host_id`.

## Конфигурация

Пример поставляется в [config.toml](config.toml):

```toml
[collector]
url = "http://127.0.0.1:8080/api/v1/reports"
interval_seconds = 60
http_timeout_seconds = 10

[agent]
state_directory = "/var/lib/workload-collector"
mountpoints = ["/"]
```

Укажите настоящий URL приёмника в `/etc/data-collector/config.toml`, затем проверьте конфиг:

```bash
python3 /usr/lib/workload-collector/workload_collector.py --check-config
```

```bash
sudo systemctl restart workload-collector
```

Файл конфигурации обязателен; отсутствующие ключи получают значения по умолчанию.
Ошибочные типы, TOML, URL и относительные пути вызывают ошибку старта, а не скрытый fallback.
`mountpoints` — массив точек монтирования, например `["/", "/home"]`, не произвольные папки.
URL — HTTP(S) без credentials, пробелов и fragment. HTTPS проверяет сертификат;
HTTP redirects не выполняются, авторизация пока отсутствует.

Конфиг доступен DynamicUser на чтение (root:root, 0644): секреты в него не помещайте.
`bash data-sender/ci.sh full` сохраняет локально изменённый конфиг (`--force-confold`).
При ручной установке dpkg может попросить выбрать версию, если изменились и локальный
файл, и пакетный вариант. Не используйте `--force-confnew`, если нужно сохранить настройки.

Служба работает с `DynamicUser=yes`, `StateDirectory=workload-collector`, без root и capabilities.
На хосте состояние может быть ссылкой на `/var/lib/private/workload-collector`.
UUID сохраняется при перезагрузке, обновлении, remove и purge. Не меняйте `state_directory`
для пакетной службы без согласованного systemd override `StateDirectory`/`WorkingDirectory`:
`ProtectSystem=strict` не позволяет писать в произвольные каталоги.

## Управление и удаление

```bash
systemctl status workload-collector
```

```bash
journalctl -u workload-collector -f
```

```bash
sudo systemctl disable --now workload-collector
```

```bash
sudo apt remove workload-collector
```

```bash
sudo apt purge workload-collector
```

Remove оставляет конфиг и UUID; purge удаляет пакетный конфиг, но **не UUID и состояние**.
Очистка идентичности — отдельное осознанное действие администратора: новый UUID создаст
новый хост на приёмнике. Пользовательские systemd overrides не удаляются пакетом.

## Переход с прежнего install.sh

Старый установщик писал unit в `/etc/systemd/system/workload-collector.service`:
он перекрывает vendor-unit пакета. Перед установкой проверьте и сохраните старый unit
и `/usr/lib/workload-collector/config.toml`, если он существует. Перенесите настройки
в новый `/etc/data-collector/config.toml`. Удалите вручную только подтверждённый старый
installer-unit, сохранив собственные overrides/masks; выполните `systemctl daemon-reload`.
Пакет намеренно не удаляет произвольные администраторские файлы в `/etc`.
Каталог состояния не переносите и не очищайте. `install.sh` не используется пакетом.

## Запуск из исходников без установки

Из `data-sender/`, с отдельным UUID и без HTTP:

```bash
python3 workload_collector.py --config config.toml --print-once --state-dir .state
```

Для запуска отправки:

```bash
python3 workload_collector.py --config config.toml --state-dir .state
```

`--config` явно меняет путь только для этого процесса; по умолчанию всегда используется
`/etc/data-collector/config.toml`. `--check-config` не создаёт состояние и не отправляет HTTP.
Не запускайте ручной экземпляр от root с состоянием работающей службы.

## Поведение демона

- Первый отчёт отправляется сразу после запуска, следующие — каждые 60 секунд.
- CPU вычисляется между последовательными сборами, в том числе после ошибки HTTP.
  100% — заняты все логические процессоры. I/O wait не считается работой CPU.
  Время гостевых CPU не прибавляется дважды. При сбросе счётчиков или смене числа
  логических CPU процент временно становится `null`.
- Расписание использует монотонные часы. Время запроса не прибавляется к каждому
  интервалу; при долгой задержке пропущенные такты не догоняются серией запросов.
- Ошибки доставки пишутся в journal. Следующая попытка — свежий отчёт на следующем
  такте. Очереди на диске и повторной доставки старых отчётов пока нет.
- Таймаут HTTP ограничивает блокирующие сетевые операции; это не абсолютный
  дедлайн всего запроса, в частности разрешение DNS может длиться дольше.
- SIGTERM/SIGINT завершают цикл. Ожидание следующего такта прерывается сразу;
  выполняющийся сетевой запрос заканчивается или получает таймаут. systemd ограничивает
  остановку 20 секундами. Неожиданное падение процесса приводит к перезапуску службы.
- Ошибка отдельного источника метрик не обнуляет показатели: соответствующий блок
  или значение становится `null`, ошибка попадает в журнал. Пустой массив означает,
  что элементов нет, а не ошибку чтения. Повреждённый `host_id` вызывает ошибку старта,
  чтобы незаметно не потерять идентичность установки.

## Формат отчёта

Чтобы получить JSON-отчёт без отправки, запустите агент с `--print-once`.
Поле `sample_duration_seconds` не передаётся.

| Поле | Смысл / источник |
| --- | --- |
| `schema_version` | Версия JSON, сейчас 1 |
| `agent_version` | Версия программы |
| `host_id` | UUID установки, создаётся один раз и сохраняется в `host_id` |
| `hostname` | Имя компьютера из `socket.gethostname()` |
| `boot_id` | `/proc/sys/kernel/random/boot_id` |
| `collected_at` | Начало сбора отчёта, UTC в ISO 8601 |
| `uptime_seconds` | `/proc/uptime`, целые секунды |
| `env` | `systemd-detect-virt`: `type` и `tech`; `unknown` при ошибке |
| `cpu` | `/proc/stat`: `logical_count` и `usage_percent` от 0 до 100 |
| `memory` | `/proc/meminfo`: `MemTotal`, `MemAvailable`, перевод KiB в байты |
| `swap` | `SwapTotal` и `SwapTotal - SwapFree`, байты; без swap — нули |
| `filesystems` | Настроенные точки монтирования: тип из `/proc/self/mountinfo`, объёмы через `statvfs` |
| `network` | `/proc/net/dev`: счётчики RX/TX по интерфейсам, кроме `lo` |

Для памяти занятость рассчитывается на коллекторе:
`100 * (total_bytes - available_bytes) / total_bytes`.

Для файловых систем `used_bytes = (f_blocks - f_bfree) * f_frsize`,
`available_bytes = f_bavail * f_frsize`: зарезервированное место не включается в доступное.

Для скорости сети коллектор сравнивает отчёты по `(host_id, interface)`:
`(новый_счётчик - старый_счётчик) / разница_времени_в_секундах`.
При смене `boot_id`, уменьшении счётчика или неположительной разнице времени
предыдущую точку нужно отбросить. Отсутствующее значение `boot_id` не означает перезагрузку.
Не суммируйте слепо трафик мостов и виртуальных интерфейсов: он может учитываться повторно.

`env` определяется при старте. При вложенной виртуализации возвращается ближайшая среда.
Текущее назначение — системные метрики на физическом Linux или в VM. Детектор умеет
обнаруживать контейнер, но CPU/RAM через `/proc` могут отражать хост, а сеть и диски —
видимую процессу среду. Потребление и лимиты контейнера через cgroups пока не реализованы.

## Проверки

```bash
python3 -m unittest discover -s tests -v
```

Тесты используют временные каталоги и локальный HTTP, не меняют службы хоста.

Справочные материалы:
- https://docs.kernel.org/filesystems/proc.html
- https://www.kernel.org/doc/html/latest/networking/statistics.html
- https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
