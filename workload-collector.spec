Name:           workload-collector
Version:        0.1.0
Release:        1%{?dist}
Summary:        Linux system metrics HTTP sender
License:        MIT
URL:            https://github.com/rasputyashka/testdatacollector
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

# Портативность: на сборочном хосте вне полноценного дистрибутивного окружения
# (rpm на Debian/Ubuntu, минимальная Fedora) макросы _unitdir/_pkgdocdir
# и скриптлеты %systemd_* могут быть не определены — задаем фолбэки.
%{!?_unitdir:%global _unitdir /usr/lib/systemd/system}
%{!?_docdir:%global _docdir %{_datadir}/doc}
%{!?_pkgdocdir:%global _pkgdocdir %{_docdir}/%{name}}

Requires:       python3 >= 3.11
Requires:       systemd
Requires:       ca-certificates
BuildRequires:  python3-pytest
BuildRequires:  python3-fastapi
BuildRequires:  python3-pydantic
BuildRequires:  python3-uvicorn
BuildRequires:  python3-jinja2
BuildRequires:  python3-httpx
BuildRequires:  systemd
%description
Collect CPU, memory, filesystem and network metrics and deliver JSON reports
to an HTTP(S) collector. Runs as an unprivileged systemd service with a
persistent installation identity and TOML configuration.

%prep
%setup -q

%build
python3 -m compileall -q .

%check
# Секция %check выполняется после %build, но до %install.
# Это прямой аналог стадий 'unittest' и 'pytest' из ci.sh.
# Сборка упадет здесь, если тесты не пройдут.
python3 -m unittest discover -s data-sender/tests -v
# pytest должен запускаться из data-collector, чтобы conftest видел пакет app
(cd data-collector && python3 -m pytest -q)

%install
# Макрос %make_install не используется, так как это скрипт.
# Копируем файлы в виртуальную корневую директорию %{buildroot}

# 1. Исполняемый скрипт (аналог /usr/lib/workload-collector/ в DEB, но для бинарников в RPM это %{_bindir})
install -D -m 0755 data-sender/workload_collector.py %{buildroot}%{_bindir}/workload-collector

# 2. Конфигурационный файл (всегда в /etc)
install -D -m 0644 data-sender/config.toml %{buildroot}%{_sysconfdir}/data-collector/config.toml

# 3. Systemd unit
install -D -m 0644 data-sender/systemd/workload-collector.service %{buildroot}%{_unitdir}/workload-collector.service

# 4. Документация
install -D -m 0644 data-sender/README.md %{buildroot}%{_pkgdocdir}/README.md
install -D -m 0644 data-sender/packaging/copyright %{buildroot}%{_pkgdocdir}/copyright

%files
%license data-sender/packaging/copyright
%doc %{_pkgdocdir}/README.md
%doc %{_pkgdocdir}/copyright
%{_bindir}/workload-collector
%{_unitdir}/workload-collector.service
%config(noreplace) %{_sysconfdir}/data-collector/config.toml

%post
# Скриптлеты дублируют %systemd_post/%systemd_preun/%systemd_postun_with_restart,
# не полагаясь на systemd-rpm-macros. Юнит включается по пресетам.
systemctl preset workload-collector.service >/dev/null 2>&1 || :

%preun
# При удалении ($1 = 0) останавливаем и отключаем службу
if [ "$1" = 0 ]; then
    systemctl disable --now workload-collector.service >/dev/null 2>&1 || :
fi

%postun
systemctl daemon-reload >/dev/null 2>&1 || :
# При обновлении ($1 >= 1) перезапускаем, если служба была активна
if [ "$1" -ge 1 ]; then
    systemctl try-restart workload-collector.service >/dev/null 2>&1 || :
fi

%changelog
* Sat Nov 21 2026 Konstantin Rasputin <rasputyashka@users.noreply.github.com> - 0.1.0-1
- Initial package release
- Added %check section to run unittest and pytest during build