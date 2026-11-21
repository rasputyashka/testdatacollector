Name:           workload-collector
Version:        0.1.0
Release:        1%{?dist}
Summary:        Linux system metrics HTTP sender
License:        MIT
URL:            https://github.com/rasputyashka/testdatacollector
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

Requires:       python3 >= 3.11
Requires:       systemd
Requires:       ca-certificates
BuildRequires:  python3-devel
BuildRequires:  python3-pytest
BuildRequires:  systemd
%description
Collect CPU, memory, filesystem and network metrics and deliver JSON reports
to an HTTP(S) collector. Runs as an unprivileged systemd service with a
persistent installation identity and TOML configuration.

%prep
%setup -q

%build
%{__python3} -m compileall -q .

%check
# Секция %check выполняется после %build, но до %install.
# Это прямой аналог ваших стадий 'unittest' и 'pytest' из ci.sh.
# Сборка упадет здесь, если тесты не пройдут.
%{__python3} -m unittest discover -s data-sender/tests -v
%{__python3} -m pytest -q data-collector

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
%license data-sender/LICENSE
%doc %{_pkgdocdir}/README.md
%doc %{_pkgdocdir}/copyright
%{_bindir}/workload-collector
%{_unitdir}/workload-collector.service
%config(noreplace) %{_sysconfdir}/data-collector/config.toml

%post
# Перезагрузка systemd для подхвата нового юнита
%systemd_post workload-collector.service

%preun
# Остановка и отключение службы перед удалением
%systemd_preun workload-collector.service

%postun
# Перезагрузка systemd после удаления/обновления
%systemd_postun_with_restart workload-collector.service

%changelog
* Sat Nov 21 2026 Konstantin Rasputin <rasputyashka@users.noreply.github.com> - 0.1.0-1
- Initial package release
- Added %check section to run unittest and pytest during build