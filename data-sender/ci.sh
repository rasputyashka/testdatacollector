#!/usr/bin/env bash
set -euo pipefail

sender=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
root=$(cd -- "$sender/.." && pwd -P)
cd -- "$root"
receiver=$root/data-collector
metadata=$sender/packaging/DEBIAN

usage() {
    cat <<'EOF'
Usage: bash data-sender/ci.sh [test|build|full]
  test   Compile and test the current checkout (default).
  build  Test and create the sender .deb in dist/; no installation.
  full   Fast-forward from Git, test, build and install on Debian/Ubuntu.

Requires Python 3.11+ on Linux. By default, receiver dependencies are installed
in data-collector/.ci-venv. Set CI_TEST_PYTHON to a Python with the test
requirements installed (command name or path relative to the repo root,
or an absolute path) to skip pip.
EOF
}

fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || fail "Required command not found: $1"; }
stage() { printf '\n==> %s\n' "$*"; }

check_python() {
    "$1" -c 'import sys; sys.exit(0 if sys.platform == "linux" and sys.version_info >= (3, 11) else 1)' ||
        fail "Linux and Python 3.11+ are required: $1"
}

run_tests() {
    local test_python
    stage 'Prepare Python test environment'
    if [[ -n ${CI_TEST_PYTHON:-} ]]; then
        test_python=$(type -P -- "$CI_TEST_PYTHON") || fail "Python not found: $CI_TEST_PYTHON"
        [[ $test_python == /* ]] || test_python=$root/$test_python
    else
        need python3
        check_python python3
        test_python=$receiver/.ci-venv/bin/python
        if [[ ! -x $test_python ]]; then
            python3 -m venv "$receiver/.ci-venv" || fail 'Could not create the venv; on Debian/Ubuntu install python3-venv'
        fi
    fi
    check_python "$test_python"
    if [[ -z ${CI_TEST_PYTHON:-} ]]; then
        "$test_python" -m pip install --only-binary=:all: -r "$receiver/requirements-dev.txt"
    fi

    stage 'Compile Python sources and tests'
    "$test_python" -m compileall -q "$sender/workload_collector.py" "$sender/tests" "$receiver/app" "$receiver/tests"
    stage 'Sender: unittest'
    (cd "$sender" && "$test_python" -m unittest discover -s tests -v)
    stage 'Receiver: pytest'
    (cd "$receiver" && "$test_python" -m pytest -q)
}

stage_package() (
    local work tree name version arch output script
    umask 022
    work=$(mktemp -d "$root/dist/.package.XXXXXXXX")
    # Work only inside the staging directory created by mktemp.
    trap 'rm -r -- "$work"' EXIT
    tree=$work/tree
    install -d -m 0755 "$tree"
    install -Dm0644 "$metadata/control" "$tree/DEBIAN/control"
    install -Dm0644 "$metadata/conffiles" "$tree/DEBIAN/conffiles"
    for script in postinst prerm postrm; do
        sh -n "$metadata/$script"
        install -Dm0755 "$metadata/$script" "$tree/DEBIAN/$script"
    done
    install -Dm0644 "$sender/workload_collector.py" "$tree/usr/lib/workload-collector/workload_collector.py"
    install -Dm0644 "$sender/config.toml" "$tree/etc/data-collector/config.toml"
    install -Dm0644 "$sender/systemd/workload-collector.service" "$tree/lib/systemd/system/workload-collector.service"
    install -Dm0644 "$sender/README.md" "$tree/usr/share/doc/workload-collector/README.md"
    install -Dm0644 "$sender/packaging/copyright" "$tree/usr/share/doc/workload-collector/copyright"

    dpkg-deb --build --root-owner-group "$tree" "$work/package.deb" >&2
    name=$(dpkg-deb --field "$work/package.deb" Package)
    version=$(dpkg-deb --field "$work/package.deb" Version)
    arch=$(dpkg-deb --field "$work/package.deb" Architecture)
    [[ $name == workload-collector && $arch == all && $version =~ ^[0-9][a-zA-Z0-9.+~-]*$ ]] ||
        fail 'Unexpected binary package metadata'
    output=$root/dist/${name}_${version}_${arch}.deb
    mv -fT -- "$work/package.deb" "$output"
    printf '%s\n' "$output"
)

build_package() {
    stage 'Build Debian installer'
    mkdir -p -- "$root/dist"
    # Keep errexit enabled inside command substitution as well.
    shopt -s inherit_errexit
    artifact=$(stage_package)
    printf 'Artifact: %s\n' "$artifact"
}

require_install_host() {
    [[ -r /etc/os-release ]] || fail 'Installation requires Debian or Ubuntu'
    local ID=''
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ $ID == debian || $ID == ubuntu ]] || fail 'Installation requires Debian or Ubuntu; use test or build here'
    need apt-get
    if (( EUID != 0 )); then need sudo; fi
}

require_clean_checkout() {
    local status
    status=$(git status --porcelain --untracked-files=normal --ignore-submodules=none)
    [[ -z $status ]] || fail 'The checkout has local changes; use test/build or use a clean clone for full'
}

refresh() {
    local branch remote source current latest tracked
    require_clean_checkout
    branch=$(git symbolic-ref --quiet --short HEAD) || fail 'Check out a branch with an upstream first'
    remote=$(git config --get "branch.$branch.remote") || fail 'No remote configured for the current branch'
    [[ $remote != . ]] || fail 'The upstream must be on a remote Git server'
    source=$(git config --get-all "branch.$branch.merge") || fail 'No upstream configured for the current branch'
    [[ $source == refs/heads/* ]] && git check-ref-format "$source" || fail 'Configure exactly one upstream branch'
    stage "Fetch $remote ${source#refs/heads/}"
    # Fetch the exact branch so a stale tracking ref cannot mask a missing remote branch.
    git fetch --no-tags -- "$remote" "$source"
    latest=$(git rev-parse --verify 'FETCH_HEAD^{commit}') || fail 'No commit returned by fetch'
    tracked=$(git rev-parse --verify '@{upstream}^{commit}') || fail 'No tracking ref for the fetched upstream branch'
    [[ $tracked == "$latest" ]] || fail 'Tracking ref does not match the fetched commit'
    current=$(git rev-parse HEAD)
    git merge-base --is-ancestor "$current" "$latest" || fail 'Local branch is ahead of or diverged from upstream; refusing to install'
    require_clean_checkout
    git merge --ff-only "$latest"
    [[ $(git rev-parse HEAD) == "$latest" ]] || fail 'Updated HEAD does not match upstream'
    # Use the newly fetched script for the rest of the pipeline.
    exec bash "$sender/ci.sh" --after-update "$latest"
}

install_package() {
    local -a elevate=()
    if (( EUID != 0 )); then elevate=(sudo); fi
    stage 'Install the freshly built sender package'
    "${elevate[@]}" apt-get install -y --reinstall -o Dpkg::Options::=--force-confold -- "$artifact"
}

case ${1:-test} in
    -h|--help) usage; exit 0 ;;
    test)
        [[ $# -le 1 ]] || fail 'Usage: bash data-sender/ci.sh test'
        run_tests
        ;;
    build)
        [[ $# == 1 ]] || fail 'Usage: bash data-sender/ci.sh build'
        need dpkg-deb
        run_tests
        build_package
        ;;
    full)
        [[ $# == 1 ]] || fail 'Usage: bash data-sender/ci.sh full'
        require_install_host
        need git
        need dpkg-deb
        refresh
        ;;
    --after-update)
        [[ $# == 2 ]] || fail 'Missing updated commit'
        require_install_host
        need dpkg-deb
        require_clean_checkout
        [[ $(git rev-parse HEAD) == "$2" && $(git rev-parse '@{upstream}') == "$2" ]] ||
            fail 'HEAD or upstream changed after refresh'
        printf 'Commit: %s\n' "$2"
        run_tests
        build_package
        install_package
        ;;
    *) usage >&2; exit 2 ;;
esac
