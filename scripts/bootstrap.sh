#!/usr/bin/env bash
set -euo pipefail

DEFAULT_REPO_URL="https://github.com/BeginnerStars/paypanel-alipay.git"
MODE="${PAYPANEL_DEPLOY_MODE:-docker}"
INSTALL_DIR="${PAYPANEL_INSTALL_DIR:-/opt/paypanel-alipay}"
REPO_URL="${PAYPANEL_REPO_URL:-$DEFAULT_REPO_URL}"
BRANCH="${PAYPANEL_BRANCH:-}"
DOMAIN="${PAYPANEL_DOMAIN:-}"
SERVICE_NAME="${SERVICE_NAME:-paypanel-alipay}"

usage() {
  cat <<'USAGE'
PayPanel Alipay one-click bootstrapper.

Usage:
  bash install.sh [--repo <git-url>] [--mode docker|aapanel] [--domain pay.example.com] [--dir /opt/paypanel-alipay] [--branch main]

Default repo:
  https://github.com/BeginnerStars/paypanel-alipay.git

Environment alternatives:
  PAYPANEL_REPO_URL, PAYPANEL_DEPLOY_MODE, PAYPANEL_DOMAIN, PAYPANEL_INSTALL_DIR, PAYPANEL_BRANCH

Examples:
  bash install.sh
  bash install.sh --mode aapanel --domain pay.example.com
  curl -fsSL https://raw.githubusercontent.com/BeginnerStars/paypanel-alipay/main/install.sh | sudo bash -s -- --mode docker
USAGE
}

info() { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[bootstrap]\033[0m %s\n' "$*" >&2; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || fail "缺少命令：$1，请先安装后重试。"; }
ensure_git() {
  if command -v git >/dev/null 2>&1; then
    return
  fi
  info "未检测到 git，尝试自动安装。"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y git
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y git
  elif command -v yum >/dev/null 2>&1; then
    yum install -y git
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache git
  else
    fail "缺少 git，且无法识别包管理器，请先安装 git。"
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --repo) REPO_URL="${2:-}"; shift 2 ;;
    --dir) INSTALL_DIR="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --service-name) SERVICE_NAME="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "未知参数：$1" ;;
  esac
done

case "$MODE" in
  docker|aapanel) ;;
  *) fail "--mode 只支持 docker 或 aapanel" ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  fail "请使用 root 执行，例如：curl -fsSL https://raw.githubusercontent.com/BeginnerStars/paypanel-alipay/main/install.sh | sudo bash"
fi

ensure_git
need_cmd python3

if [ -z "$REPO_URL" ] && git -C "$(pwd)" remote get-url origin >/dev/null 2>&1; then
  REPO_URL="$(git -C "$(pwd)" remote get-url origin)"
fi
[ -n "$REPO_URL" ] || fail "未设置项目仓库地址。请使用 --repo <git-url> 或 PAYPANEL_REPO_URL。"

mkdir -p "$(dirname "$INSTALL_DIR")"
if [ -d "$INSTALL_DIR/.git" ]; then
  info "检测到已有项目目录，执行 git pull：$INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch --all --prune
  if [ -n "$BRANCH" ]; then
    git -C "$INSTALL_DIR" checkout "$BRANCH"
  fi
  git -C "$INSTALL_DIR" pull --ff-only
else
  if [ -e "$INSTALL_DIR" ] && [ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 2>/dev/null | head -n 1)" ]; then
    fail "安装目录非空且不是 git 仓库：$INSTALL_DIR"
  fi
  info "拉取项目文件：$REPO_URL -> $INSTALL_DIR"
  if [ -n "$BRANCH" ]; then
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  else
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
fi

cd "$INSTALL_DIR"
chmod +x deploy.sh scripts/deploy.sh scripts/install_aapanel_service.sh scripts/check_alipay_account.py 2>/dev/null || true

if [ "$MODE" = "docker" ]; then
  info "开始 Docker 部署。"
  bash deploy.sh
else
  info "开始 aaPanel/systemd 部署。"
  SERVICE_NAME="$SERVICE_NAME" bash scripts/install_aapanel_service.sh "$DOMAIN"
fi

info "一键部署完成。安装目录：$INSTALL_DIR"
