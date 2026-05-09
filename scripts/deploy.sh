#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

info() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deploy]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[deploy]\033[0m %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少命令：$1，请先安装后重试。"
}

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    printf 'docker compose'
  elif command -v docker-compose >/dev/null 2>&1; then
    printf 'docker-compose'
  else
    fail "未找到 Docker Compose。请安装 docker compose 插件或 docker-compose。"
  fi
}

random_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 48 | tr -d '\n'
  else
    python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
  fi
}

replace_env_value() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" .env; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

need_cmd docker
need_cmd python3

if [ ! -f .env ]; then
  info "未发现 .env，已从 .env.example 创建。"
  cp .env.example .env
fi

if grep -q '^APP_SECRET_KEY=change-me-to-a-long-random-string$' .env; then
  info "生成随机 APP_SECRET_KEY。"
  replace_env_value APP_SECRET_KEY "$(random_secret)"
fi

if grep -q '^APP_ADMIN_PASSWORD=change-me-now$' .env; then
  password="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(18))
PY
)"
  replace_env_value APP_ADMIN_PASSWORD "$password"
  warn "已生成随机管理员密码：$password"
  warn "请保存该密码，后续可在 .env 中修改 APP_ADMIN_PASSWORD。"
fi

mkdir -p data certs
chmod 700 data || true

COMPOSE="$(compose_cmd)"
info "开始构建并启动 PayPanel Alipay。"
# shellcheck disable=SC2086
$COMPOSE up -d --build

info "部署完成。"
info "默认访问地址：http://127.0.0.1:8000"
info "如部署到公网，请设置 .env 中的 APP_BASE_URL 并在反向代理上启用 HTTPS。"
