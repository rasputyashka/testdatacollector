#!/usr/bin/env bash
set -euo pipefail

# --- Переменные окружения и пути ---
sender=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
root=$(cd -- "$sender/.." && pwd -P)

# Пути к компонентам
receiver=$root/data-collector
spec_file=$root/workload-collector.spec

# RPM Buildroot (используем стандартный путь в домашней директории)
rpmbuild_root=$HOME/rpmbuild

# --- Функции вывода ---
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
stage() { printf '\n==> %s\n' "$*"; }

# --- Проверки ---
check_python() {
    "$1" -c 'import sys; sys.exit(0 if sys.platform == "linux" and sys.version_info >= (3, 11) else 1)' ||
        fail "Linux and Python 3.11+ are required: $1"
}

need() { command -v "$1" >/dev/null || fail "Required command not found: $1"; }

# --- Подготовка дерева каталогов RPM ---
setup_rpmbuild_tree() {
    stage 'Preparing RPM build tree'
    mkdir -p "$rpmbuild_root"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
    
    # Копируем spec-файл в папку SPECS
    if [[ -f "$spec_file" ]]; then
        cp "$spec_file" "$rpmbuild_root/SPECS/"
    else
        fail "Spec file not found at $spec_file"
    fi

    # Версию берем из spec-файла, чтобы имя архива (Source0) всегда совпадало
    local version
    version=$(awk '/^Version:/{print $2; exit}' "$spec_file")
    [[ -n $version ]] || fail 'Version tag not found in spec file'
    local archive_name="workload-collector-$version.tar.gz"
    stage "Creating source archive: $archive_name"

    # Переходим в корень, чтобы архив не содержал лишних родительских папок
    # Используем --exclude-vcs, чтобы не тянуть .git
    tar --exclude-vcs --exclude='.ci-venv' --exclude='dist' --exclude='__pycache__' \
        --exclude='.claude' \
        -czf "$rpmbuild_root/SOURCES/$archive_name" \
        --transform "s,^\./,workload-collector-$version/," .
}

# --- Запуск тестов (аналог run_tests) ---
run_tests() {
    stage 'Running Python tests (Sender: unittest, Receiver: pytest)'
    local test_python
    
    if [[ ! -d "$receiver/.ci-venv" ]]; then
        need python3
        check_python python3
        python3 -m venv "$receiver/.ci-venv" || fail 'Could not create the venv'
    fi
    
    test_python=$receiver/.ci-venv/bin/python
    "$test_python" -m pip install --only-binary=:all: -r "$receiver/requirements-dev.txt"
    
    "$test_python" -m compileall -q "$sender/workload_collector.py" "$sender/tests" "$receiver/app" "$receiver/tests"
    
    (cd "$sender" && "$test_python" -m unittest discover -s tests -v)
    (cd "$receiver" && "$test_python" -m pytest -q)
}

# --- Сборка RPM ---
build_rpm() {
    stage 'Building RPM package'
    need rpmbuild

    # Очистка предыдущих сборок в BUILDROOT (на случай повторных запусков)
    rm -rf "$rpmbuild_root/BUILD"/*

    # Запуск rpmbuild
    # -ba: собрать бинарный и исходный пакеты
    # --clean: очистить BuildRoot после сборки
    # --nodeps: python-зависимости тестов из BuildRequires ставятся средствами CI
    # (dnf/apt), а rpmdb стороннего хоста их не отражает; от реальных проблем
    # это не защищает — секция %check всё равно упадет на отсутствующих модулях.
    rpmbuild -ba --clean --nodeps "$rpmbuild_root/SPECS/workload-collector.spec"

    # Поиск готового пакета
    local artifact
    artifact=$(find "$rpmbuild_root/RPMS" -name "workload-collector-*.rpm" -type f | head -n1)

    if [[ -z "$artifact" ]]; then
        fail "RPM build finished, but package not found in $rpmbuild_root/RPMS"
    fi

    # Кладем артефакт в dist/, как это делает deb-сборка (data-sender/ci.sh)
    mkdir -p -- "$root/dist"
    cp -f -- "$artifact" "$root/dist/"
    artifact="$root/dist/${artifact##*/}"

    printf 'Artifact: %s\n' "$artifact"
}

# --- Main ---
case ${1:-test} in
    -h|--help)
        printf 'Usage: bash ci_rpm.sh [test|build|full]\n'
        exit 0
        ;;
    test)
        run_tests
        ;;
    build)
        setup_rpmbuild_tree
        run_tests
        build_rpm
        ;;
    full)
        # В RPM-мире "full" обычно означает сборку в изолированном окружении (Mock).
        # Для простоты здесь мы делаем обычную сборку.
        setup_rpmbuild_tree
        run_tests
        build_rpm
        ;;
    *)
        printf 'Invalid command\n' >&2
        exit 2
        ;;
esac