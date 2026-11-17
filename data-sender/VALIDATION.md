# Результаты проверки

Проверено 17 ноября 2026 года в среде Windows, на Python из доступного runtime.

`python -m unittest discover -s tests -v`: **16 тестов, 13 пройдено, 3 пропущено**.

Пройдены расчёты CPU (включая guest/iowait, сброс и смену числа процессоров),
памяти и swap, зарезервированного места, разбор mountinfo и сетевых счётчиков,
формат отчёта, работа с недоступными источниками, чтение сохранённого UUID,
обработка повреждённого UUID, определение среды на фикстурах,
формирование HTTP POST и таймаута на подменённом транспорте,
обработка статуса ошибки, продолжение цикла после сбоя отправки и остановка цикла.

Пропущено:
- HTTP-доставка полных отчётов и проверка HTTP-ошибок/перенаправлений через реальный
  локальный сервер: Windows sandbox запрещает socket connect, WinError 10013.
- Атомарное создание UUID при конкурентных стартах: проверяется на целевом Linux;
  Windows sandbox запрещает создание жёстких ссылок.

Запуск под настоящим systemd, установка и чтение настоящего Linux `/proc` здесь
не проверены: доступного Linux/WSL окружения нет. Для проверки на Debian/Ubuntu:

```bash
python3 -m unittest discover -s tests -v
python3 workload_collector.py --print-once --state-dir /tmp/workload-collector-demo
sudo sh install.sh
sudo systemd-analyze verify systemd/workload-collector.service
systemctl status workload-collector
journalctl -u workload-collector -n 30 --no-pager
```

Перед установкой замените `COLLECTOR_URL` на адрес работающего тестового коллектора.
По умолчанию используется `http://127.0.0.1:8080/api/v1/reports`.
