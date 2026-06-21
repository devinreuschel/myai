#!/usr/bin/env sh
# myai end-user installer — curl -fsSL https://raw.githubusercontent.com/OWNER/myai/main/install.sh | sh
set -eu

MYAI_INSTALL_METHOD="${MYAI_INSTALL_METHOD:-git}"
MYAI_REPO="${MYAI_REPO:-https://github.com/OWNER/myai.git}"
MYAI_REF="${MYAI_REF:-main}"
MYAI_INSTALL_DIR="${MYAI_INSTALL_DIR:-$HOME/.local/share/myai}"
MYAI_BIN_DIR="${MYAI_BIN_DIR:-$HOME/.local/bin}"
MYAI_ASSUME_YES="${MYAI_ASSUME_YES:-0}"
MYAI_NO_INSTALL_DEPS="${MYAI_NO_INSTALL_DEPS:-0}"

PYTHON=""
OS=""
LINUX_DISTRO=""
ASSUME_YES=0
NO_INSTALL_DEPS=0
UNINSTALL=0
NEED_GIT_INSTALL=0
NEED_PYTHON_INSTALL=0

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

info() {
  printf '%s\n' "$*"
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

have_brew() {
  have_cmd brew
}

detect_os() {
  OS="$(uname -s)"
  case "$OS" in
    Darwin) OS=darwin ;;
    Linux) OS=linux ;;
    *) OS=unknown ;;
  esac

  LINUX_DISTRO=unknown
  if [ "$OS" = linux ] && [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}${ID_LIKE:-}" in
      *debian*|*ubuntu*) LINUX_DISTRO=debian ;;
      *fedora*|*rhel*|*centos*) LINUX_DISTRO=fedora ;;
    esac
  fi
}

python_version_ok() {
  py="$1"
  "$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 14) else 1)' 2>/dev/null
}

find_python() {
  PYTHON=""
  for cmd in python3.14 python3 python; do
    if have_cmd "$cmd" && python_version_ok "$cmd"; then
      PYTHON="$cmd"
      return 0
    fi
  done
  return 1
}

have_git() {
  have_cmd git
}

have_python() {
  find_python
}

confirm() {
  prompt="$1"
  if [ "$ASSUME_YES" = 1 ]; then
    return 0
  fi
  if [ ! -e /dev/tty ]; then
    die "Cannot prompt interactively. Use --yes or set MYAI_ASSUME_YES=1."
  fi
  printf '%s' "$prompt" >/dev/tty
  read -r ans </dev/tty || ans=""
  case "$ans" in
    y | Y | yes | YES) return 0 ;;
    *) return 1 ;;
  esac
}

plan_git_install() {
  NEED_GIT_INSTALL=1
  case "$OS" in
    darwin)
      if have_brew; then
        GIT_INSTALL_PLAN="Install git via Homebrew: brew install git"
        GIT_INSTALL_CMD="brew install git"
      else
        GIT_INSTALL_PLAN="Install Xcode Command Line Tools (system GUI dialog): xcode-select --install"
        GIT_INSTALL_CMD="xcode-select --install"
        GIT_INSTALL_WAIT=1
      fi
      ;;
    linux)
      case "$LINUX_DISTRO" in
        debian)
          GIT_INSTALL_PLAN="Install git via apt: sudo apt-get update && sudo apt-get install -y git"
          GIT_INSTALL_CMD="sudo apt-get update && sudo apt-get install -y git"
          ;;
        fedora)
          GIT_INSTALL_PLAN="Install git via dnf: sudo dnf install -y git"
          GIT_INSTALL_CMD="sudo dnf install -y git"
          ;;
        *)
          GIT_INSTALL_PLAN=""
          GIT_INSTALL_CMD=""
          ;;
      esac
      ;;
    *)
      GIT_INSTALL_PLAN=""
      GIT_INSTALL_CMD=""
      ;;
  esac
}

plan_python_install() {
  NEED_PYTHON_INSTALL=1
  case "$OS" in
    darwin)
      if have_brew; then
        PYTHON_INSTALL_PLAN="Install Python 3.14 via Homebrew: brew install python@3.14"
        PYTHON_INSTALL_CMD="brew install python@3.14"
        PYTHON_POST_PATH="/opt/homebrew/bin/python3.14 /usr/local/bin/python3.14"
      else
        PYTHON_INSTALL_PLAN=""
        PYTHON_INSTALL_CMD=""
      fi
      ;;
    linux)
      case "$LINUX_DISTRO" in
        debian)
          PYTHON_INSTALL_PLAN="Install Python 3.14 via deadsnakes PPA:
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt-get update
  sudo apt-get install -y python3.14 python3.14-venv"
          PYTHON_INSTALL_CMD="sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt-get update && sudo apt-get install -y python3.14 python3.14-venv"
          ;;
        fedora)
          PYTHON_INSTALL_PLAN="Install Python 3.14 via dnf (best effort): sudo dnf install -y python3.14"
          PYTHON_INSTALL_CMD="sudo dnf install -y python3.14"
          ;;
        *)
          PYTHON_INSTALL_PLAN=""
          PYTHON_INSTALL_CMD=""
          ;;
      esac
      ;;
    *)
      PYTHON_INSTALL_PLAN=""
      PYTHON_INSTALL_CMD=""
      ;;
  esac
}

run_shell_cmd() {
  info "Running: $*"
  sh -c "$*"
}

install_git() {
  if [ -z "${GIT_INSTALL_CMD:-}" ]; then
    print_manual_git_instructions
    exit 1
  fi

  if [ "${GIT_INSTALL_WAIT:-0}" = 1 ]; then
    info "$GIT_INSTALL_PLAN"
    run_shell_cmd "$GIT_INSTALL_CMD" || true
    info ""
    info "Complete the Xcode Command Line Tools installer dialog."
    if [ -e /dev/tty ]; then
      printf 'Press Enter when installation is finished (or Ctrl+C to abort)... ' >/dev/tty
      read -r _ </dev/tty || true
    else
      die "Complete the installer, then re-run this script."
    fi
  else
    run_shell_cmd "$GIT_INSTALL_CMD"
  fi

  if ! have_git; then
    die "git is still not available after install attempt"
  fi
}

install_python() {
  if [ -z "${PYTHON_INSTALL_CMD:-}" ]; then
    print_manual_python_instructions
    exit 1
  fi

  run_shell_cmd "$PYTHON_INSTALL_CMD"

  if ! find_python; then
    if [ -n "${PYTHON_POST_PATH:-}" ]; then
      for candidate in $PYTHON_POST_PATH; do
        if [ -x "$candidate" ] && python_version_ok "$candidate"; then
          PYTHON="$candidate"
          return 0
        fi
      done
    fi
    die "Python 3.14+ is still not available after install attempt"
  fi
}

print_manual_git_instructions() {
  info "git is required but cannot be installed automatically on this system."
  info ""
  case "$OS" in
    darwin)
      info "Install git manually:"
      info "  xcode-select --install"
      info "  or install Homebrew (https://brew.sh) then: brew install git"
      ;;
    linux)
      info "Install git manually using your system package manager, e.g.:"
      info "  sudo apt-get install git    (Debian/Ubuntu)"
      info "  sudo dnf install git        (Fedora)"
      ;;
    *)
      info "Install git using your platform package manager."
      ;;
  esac
}

print_manual_python_instructions() {
  info "Python 3.14+ is required but cannot be installed automatically on this system."
  info ""
  case "$OS" in
    darwin)
      info "Install Python 3.14 manually:"
      info "  brew install python@3.14"
      info "  or download from https://www.python.org/downloads/"
      ;;
    linux)
      info "Install Python 3.14 manually, e.g.:"
      info "  deadsnakes PPA on Ubuntu: https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa"
      info "  pyenv: pyenv install 3.14"
      ;;
    *)
      info "Install Python 3.14+ from https://www.python.org/downloads/"
      ;;
  esac
}

print_changes_summary() {
  info ""
  info "=== Changes this install will make ==="
  info ""
  info "myai application:"
  info "  Clone/update source:  $MYAI_INSTALL_DIR"
  info "  Python venv:          $MYAI_INSTALL_DIR/.venv  (isolated; no system pip changes)"
  info "  CLI symlink:          $MYAI_BIN_DIR/myai -> venv console script"
  info ""
  info "Later, myai commands may create runtime state in ~/.myai/ (not created by this script)."
  info ""
  info "This script will NOT modify:"
  info "  - shell config files (~/.bashrc, ~/.zshrc, etc.)"
  info "  - system Python or global pip packages"
  info ""

  if [ "$NEED_GIT_INSTALL" = 1 ] || [ "$NEED_PYTHON_INSTALL" = 1 ]; then
    info "Dependency installs (approved above, not yet applied):"
    if [ "$NEED_GIT_INSTALL" = 1 ] && [ -n "${GIT_INSTALL_PLAN:-}" ]; then
      info "  git: $GIT_INSTALL_PLAN"
    fi
    if [ "$NEED_PYTHON_INSTALL" = 1 ] && [ -n "${PYTHON_INSTALL_PLAN:-}" ]; then
      info "  Python 3.14+:"
      printf '%s\n' "$PYTHON_INSTALL_PLAN" | sed 's/^/    /'
    fi
    info ""
  fi
}

ensure_prerequisites() {
  NEED_GIT_INSTALL=0
  NEED_PYTHON_INSTALL=0
  GIT_INSTALL_PLAN=""
  GIT_INSTALL_CMD=""
  GIT_INSTALL_WAIT=0
  PYTHON_INSTALL_PLAN=""
  PYTHON_INSTALL_CMD=""
  PYTHON_POST_PATH=""

  missing=""
  if ! have_git; then
    missing="${missing}git "
    plan_git_install
  fi
  if ! have_python; then
    missing="${missing}Python3.14+ "
    plan_python_install
  fi

  if [ -z "$missing" ]; then
    return 0
  fi

  info "Missing prerequisites: $missing"
  info ""

  if [ "$NO_INSTALL_DEPS" = 1 ]; then
    info "Automatic dependency install disabled (--no-install-deps)."
    if ! have_git; then print_manual_git_instructions; fi
    if ! have_python; then print_manual_python_instructions; fi
    exit 1
  fi

  can_auto=1
  if ! have_git && [ -z "${GIT_INSTALL_CMD:-}" ]; then can_auto=0; fi
  if ! have_python && [ -z "${PYTHON_INSTALL_CMD:-}" ]; then can_auto=0; fi

  if [ "$can_auto" = 0 ]; then
    if ! have_git; then print_manual_git_instructions; info ""; fi
    if ! have_python; then print_manual_python_instructions; info ""; fi
    exit 1
  fi

  info "The following dependencies would be installed:"
  if ! have_git && [ -n "${GIT_INSTALL_PLAN:-}" ]; then
    info "  git: $GIT_INSTALL_PLAN"
  fi
  if ! have_python && [ -n "${PYTHON_INSTALL_PLAN:-}" ]; then
    info "  Python:"
    printf '%s\n' "$PYTHON_INSTALL_PLAN" | sed 's/^/    /'
  fi
  info ""

  if ! confirm "Install missing dependencies? [y/N] "; then
    info "Aborted. Install dependencies manually and re-run this script."
    if ! have_git; then print_manual_git_instructions; info ""; fi
    if ! have_python; then print_manual_python_instructions; fi
    exit 1
  fi
}

install_missing_prerequisites() {
  if ! have_git; then install_git; fi
  if ! have_python; then install_python; fi
  if ! have_git; then die "git is required"; fi
  if ! find_python; then die "Python 3.14+ is required"; fi
}

path_contains() {
  dir="$1"
  case ":${PATH}:" in
    *:"$dir":*) return 0 ;;
    *) return 1 ;;
  esac
}

do_uninstall() {
  info "Removing myai install..."
  if [ -d "$MYAI_INSTALL_DIR" ]; then
    info "  deleting $MYAI_INSTALL_DIR"
    rm -rf "$MYAI_INSTALL_DIR"
  fi
  if [ -e "$MYAI_BIN_DIR/myai" ]; then
    info "  deleting $MYAI_BIN_DIR/myai"
    rm -f "$MYAI_BIN_DIR/myai"
  fi
  info "Done."
}

install_myai_git() {
  ensure_prerequisites
  print_changes_summary

  if ! confirm "Proceed with myai install? [y/N] "; then
    info "Aborted."
    exit 0
  fi

  install_missing_prerequisites

  if [ -d "$MYAI_INSTALL_DIR/.git" ]; then
    info "Updating existing install at $MYAI_INSTALL_DIR"
    if git -C "$MYAI_INSTALL_DIR" remote get-url origin >/dev/null 2>&1; then
      git -C "$MYAI_INSTALL_DIR" fetch origin
      git -C "$MYAI_INSTALL_DIR" checkout "$MYAI_REF"
      git -C "$MYAI_INSTALL_DIR" pull --ff-only origin "$MYAI_REF"
    else
      info "No git origin configured; reinstalling from existing source tree"
    fi
  else
    if [ -e "$MYAI_INSTALL_DIR" ]; then
      die "$MYAI_INSTALL_DIR exists but is not a git repo; remove it or set MYAI_INSTALL_DIR"
    fi
    info "Cloning $MYAI_REPO (ref: $MYAI_REF) into $MYAI_INSTALL_DIR"
    git clone --depth 1 --branch "$MYAI_REF" "$MYAI_REPO" "$MYAI_INSTALL_DIR"
  fi

  info "Creating venv at $MYAI_INSTALL_DIR/.venv"
  "$PYTHON" -m venv "$MYAI_INSTALL_DIR/.venv"

  pip="$MYAI_INSTALL_DIR/.venv/bin/pip"
  "$pip" install --upgrade pip
  "$pip" install "$MYAI_INSTALL_DIR"

  mkdir -p "$MYAI_BIN_DIR"
  ln -sf "$MYAI_INSTALL_DIR/.venv/bin/myai" "$MYAI_BIN_DIR/myai"

  info ""
  info "myai installed successfully."
  info "  binary: $MYAI_BIN_DIR/myai"
  info "  source: $MYAI_INSTALL_DIR"

  if ! path_contains "$MYAI_BIN_DIR"; then
    info ""
    info "$MYAI_BIN_DIR is not on your PATH. Add it to your shell, e.g.:"
    info "  export PATH=\"$MYAI_BIN_DIR:\$PATH\""
  fi

  info ""
  info "Run: myai --help"
}

install_dispatch() {
  case "$MYAI_INSTALL_METHOD" in
    git) install_myai_git ;;
    pypi) die "pypi install not supported yet; use MYAI_INSTALL_METHOD=git" ;;
    release) die "release tarball install not supported yet" ;;
    *) die "unknown MYAI_INSTALL_METHOD: $MYAI_INSTALL_METHOD" ;;
  esac
}

usage() {
  cat <<EOF
myai installer

Usage:
  curl -fsSL https://raw.githubusercontent.com/OWNER/myai/main/install.sh | sh
  install.sh [--yes] [--no-install-deps] [--uninstall] [--help]

Options:
  --yes, -y           Skip confirmation prompts
  --no-install-deps   Do not offer to install missing git/Python
  --uninstall         Remove install dir and CLI symlink
  --help              Show this help

Environment:
  MYAI_INSTALL_METHOD   Install backend (default: git)
  MYAI_REPO             Git clone URL
  MYAI_REF              Branch or tag (default: main)
  MYAI_INSTALL_DIR      Install location (default: ~/.local/share/myai)
  MYAI_BIN_DIR          CLI symlink dir (default: ~/.local/bin)
  MYAI_ASSUME_YES       Set to 1 to skip prompts (same as --yes)
EOF
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --yes | -y)
        ASSUME_YES=1
        ;;
      --no-install-deps)
        NO_INSTALL_DEPS=1
        ;;
      --uninstall)
        UNINSTALL=1
        ;;
      --help | -h)
        usage
        exit 0
        ;;
      *)
        die "unknown option: $1 (try --help)"
        ;;
    esac
    shift
  done

  if [ "$MYAI_ASSUME_YES" = 1 ]; then
    ASSUME_YES=1
  fi
  if [ "$MYAI_NO_INSTALL_DEPS" = 1 ]; then
    NO_INSTALL_DEPS=1
  fi
}

main() {
  parse_args "$@"
  detect_os

  if [ "$UNINSTALL" = 1 ]; then
    do_uninstall
    exit 0
  fi

  install_dispatch
}

main "$@"
