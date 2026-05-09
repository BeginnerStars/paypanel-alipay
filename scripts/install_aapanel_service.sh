#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-paypanel-alipay}"
DOMAIN="${1:-${APP_PANEL_DOMAIN:-}}"
PORT="${APP_PORT:-8000}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"

info() { printf '\033[1;34m[aapanel]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[aapanel]\033[0m %s\n' "$*" >&2; exit 1; }
replace_env_value() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "$PROJECT_DIR/.env"; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "$PROJECT_DIR/.env"
  else
    printf '%s=%s\n' "$key" "$value" >> "$PROJECT_DIR/.env"
  fi
}
random_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 48 | tr -d '\n'
  else
    "$PYTHON_BIN" - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
  fi
}

[ -n "$PYTHON_BIN" ] || fail "未找到 python3，请先在 aaPanel 软件商店或系统中安装 Python。"
[ "$(id -u)" -eq 0 ] || fail "请使用 root 执行，以便写入 systemd 服务。"

cd "$PROJECT_DIR"
if [ ! -f .env ]; then
  cp .env.example .env
  info "已从 .env.example 创建 .env"
fi

if grep -q '^APP_SECRET_KEY=change-me-to-a-long-random-string$' .env; then
  replace_env_value APP_SECRET_KEY "$(random_secret)"
fi
if grep -q '^APP_ADMIN_PASSWORD=change-me-now$' .env; then
  password="$($PYTHON_BIN - <<'PY'
import secrets
print(secrets.token_urlsafe(18))
PY
)"
  replace_env_value APP_ADMIN_PASSWORD "$password"
  info "已生成随机管理员密码：$password"
fi

replace_env_value APP_HOST "127.0.0.1"
replace_env_value APP_PORT "$PORT"
replace_env_value APP_SSL_ENABLED "0"
if [ -n "$DOMAIN" ]; then
  replace_env_value APP_BASE_URL "https://${DOMAIN}"
  replace_env_value APP_PANEL_DOMAIN "$DOMAIN"
  replace_env_value APP_ENFORCE_PANEL_DOMAIN "1"
fi

mkdir -p data
chmod 700 data || true

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<SERVICE
[Unit]
Description=PayPanel Alipay
After=network.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${PYTHON_BIN} -m app.main
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

info "systemd 服务已启动：${SERVICE_NAME}"
info "本机监听：http://127.0.0.1:${PORT}"
if [ -n "$DOMAIN" ]; then
  info "请在 aaPanel Website 中为 ${DOMAIN} 添加站点并反向代理到 http://127.0.0.1:${PORT}，SSL 建议在 aaPanel 中配置。"
else
  info "请在 aaPanel Website 中添加域名，并反向代理到 http://127.0.0.1:${PORT}。"
fi
info "Nginx 配置示例：${PROJECT_DIR}/docs/aapanel-nginx.conf"
