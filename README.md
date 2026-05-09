# PayPanel Alipay

一个轻量化的支付宝收款面板，面向自部署场景：输入金额即可创建收款订单、生成支付二维码，并通过支付宝异步通知和主动轮询监控订单状态。

## 功能

- **发起收款**：支持当面付 `alipay.trade.precreate`、电脑网站支付 `alipay.trade.page.pay`、手机网站支付 `alipay.trade.wap.pay`。
- **二维码付款**：当面付直接展示支付宝返回的 `qr_code`；网站支付展示内部跳转链接二维码，客户扫码后进入支付宝收银台。
- **订单管理**：订单列表、关键字搜索、状态筛选、概览统计、手动查询状态。
- **多账户接入**：可配置多个支付宝开放平台应用，支持按失败次数轮询，请求失败自动切换下一个可用账户。
- **订单监控**：可开启后台轮询 `alipay.trade.query`，同时支持支付宝异步通知 `/alipay/notify`。
- **登录保护**：后台需要账号密码登录，可选开启基于 TOTP 的 2FA。
- **轻量部署**：FastAPI + SQLite，默认数据保存在 `data/paypanel.db`，迁移时复制 `.env` 与 `data/` 即可。

## 快速开始

### 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

打开 `http://localhost:8000`，默认登录信息来自 `.env`：

```env
APP_ADMIN_USERNAME=admin
APP_ADMIN_PASSWORD=change-me-now
```

请务必修改 `APP_SECRET_KEY` 和 `APP_ADMIN_PASSWORD` 后再部署到公网。

### Docker Compose

```bash
cp .env.example .env
# 编辑 .env
APP_SECRET_KEY=$(python - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
) docker compose up -d --build
```

## 支付宝开放平台配置

1. 在支付宝开放平台创建应用并开通所需产品：当面付、电脑网站支付、手机网站支付。
2. 在面板的 **账户** 页面新增账户，填写：
   - App ID
   - 应用私钥（PKCS8 PEM 或去掉头尾后的 Base64）
   - 支付宝公钥（PEM 或 Base64）
   - 如使用证书模式，可填写应用公钥证书 SN、支付宝根证书 SN
3. 将支付宝应用中的异步通知地址配置为：

```text
https://你的域名/alipay/notify
```

4. 如果面板部署在反向代理后，请在 `.env` 中设置公网访问地址：

```env
APP_BASE_URL=https://你的域名
```

## 订单状态说明

- `WAIT_BUYER_PAY`：等待买家付款
- `TRADE_SUCCESS` / `TRADE_FINISHED`：支付成功
- `TRADE_CLOSED`：交易关闭
- `FAILED`：创建收款请求失败，详情页会显示错误

## 迁移与备份

该项目默认只依赖环境变量与 SQLite 文件：

```text
.env
/data/paypanel.db
```

迁移到新机器时，停止服务后复制以上文件，再启动服务即可。

## 安全建议

- 使用 HTTPS 暴露面板和回调地址。
- 修改默认管理员密码并设置高强度 `APP_SECRET_KEY`。
- 开启 2FA 前先在设置页生成 TOTP 密钥并用认证器保存。
- 私钥仅保存在你的服务器 SQLite 数据库中，建议限制文件权限并定期备份。
