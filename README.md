# PayPanel Alipay

一个轻量化的支付宝收款面板，面向自部署场景：输入金额即可创建收款订单、生成支付二维码，并通过支付宝异步通知和主动轮询监控订单状态。

## 功能

- **发起收款**：支持支付宝开放平台的当面付 `alipay.trade.precreate`、电脑网站支付 `alipay.trade.page.pay`、手机网站支付 `alipay.trade.wap.pay`。
- **二维码付款**：当面付展示支付宝返回的 `qr_code`；网站支付展示内部跳转链接二维码，客户扫码后进入支付宝收银台。
- **订单管理**：订单列表、关键字搜索、状态筛选、概览统计、手动查询状态，并支持按时段清理或一键清空订单。
- **多账户接入**：可配置多个支付宝开放平台应用，支持按失败次数轮询，请求失败自动切换下一个可用账户，支付密钥会加密后存入 SQLite。
- **订单监控**：可开启后台轮询 `alipay.trade.query`，同时支持支付宝异步通知 `/alipay/notify`。
- **订单超时**：可在设置页配置订单超时关闭分钟数，并同步写入支付宝请求的 `timeout_express`。
- **域名与 HTTPS**：可绑定面板访问域名、限制 Host 访问、配置内置 HTTPS 证书，并单独设置支付宝异步通知回调域名。
- **登录保护**：后台需要账号密码登录，可选开启基于 TOTP 的 2FA。
- **轻量部署**：仅依赖 Python 标准库、SQLite 和系统 `openssl` 命令，不再需要通过 pip 安装运行时依赖；迁移时复制 `.env` 与 `data/` 即可。

## 快速开始

### 一键 Docker 部署

```bash
./deploy.sh
```

脚本会自动创建 `.env`、生成随机 `APP_SECRET_KEY`，当检测到默认管理员密码时会替换为随机密码，然后执行 `docker compose up -d --build`。

### aaPanel / 宝塔国际面板部署

aaPanel（宝塔国际版）官网说明其提供网站、SSL、Docker/Compose 和反向代理等可视化管理能力：https://www.aapanel.com/ 。本项目推荐在 aaPanel 中让 Nginx/SSL 作为前置反向代理，PayPanel 只监听本机 `127.0.0.1:8000`。

1. 将项目上传或克隆到服务器，例如：`/www/wwwroot/paypanel-alipay`。
2. 在项目目录执行一键 systemd 安装脚本（把域名替换成你的收款面板域名）：

```bash
sudo bash scripts/install_aapanel_service.sh pay.example.com
```

3. 进入 aaPanel：**Website** -> **Add site**，添加 `pay.example.com`。
4. 在站点设置中开启 SSL（Let's Encrypt 或手动证书均可）。反向代理目标填写：`http://127.0.0.1:8000`。如果需要手动写 Nginx 配置，可参考 `docs/aapanel-nginx.conf`。
5. 支付宝开放平台异步通知地址配置为：`https://pay.example.com/alipay/notify`。

> 使用 aaPanel/Nginx 终止 HTTPS 时，保持 `.env` 中 `APP_SSL_ENABLED=0` 即可；如果不用反向代理，才需要在面板设置里配置内置 HTTPS 证书。

常用维护命令：

```bash
systemctl status paypanel-alipay
systemctl restart paypanel-alipay
journalctl -u paypanel-alipay -f
```

### 本地运行

```bash
cp .env.example .env
python -m app.main
```

打开 `http://localhost:8000`，默认登录信息来自 `.env`：

```env
APP_ADMIN_USERNAME=admin
APP_ADMIN_PASSWORD=change-me-now
```

请务必修改 `APP_SECRET_KEY` 和 `APP_ADMIN_PASSWORD` 后再部署到公网。

> 说明：面板自身不需要 pip 依赖。支付宝 RSA2 签名/验签通过系统 `openssl` 命令完成，常见 Linux 发行版与 Docker 镜像均已提供。

### 手动 Docker Compose

```bash
cp .env.example .env
# 编辑 .env 后启动
docker compose up -d --build
```

## 域名、HTTPS 与回调

- **绑定访问域名**：在设置页填写 `pay.example.com` 并开启“仅允许绑定域名访问面板”后，非绑定 Host 会返回 421。
- **自定义回调域名**：在设置页填写 `https://notify.example.com` 后，默认支付宝异步通知地址会变为 `https://notify.example.com/alipay/notify`；单个支付宝账户仍可在账户页覆盖通知 URL。
- **内置 HTTPS**：可在设置页启用并填写证书/私钥路径，例如 Docker 部署时将证书放到 `./certs`，填写 `/app/certs/fullchain.pem` 与 `/app/certs/privkey.pem`，保存后重启容器生效。
- 如果已经在 Nginx/Caddy/Traefik 等反向代理上终止 TLS，通常保持内置 HTTPS 关闭，只设置 `APP_BASE_URL=https://你的域名` 即可。

## 支付宝请求方式核对

已按支付宝开放平台文档核对当前实现：

- 服务端接口（当面付预创建、交易查询）使用 `POST` 提交到网关。
- 电脑网站支付与手机网站支付使用自动提交的 `POST` HTML 表单跳转支付宝收银台。
- 异步通知地址由公共参数 `notify_url` 传入，通知到达后会先验签，再返回 `success`。
- 支付宝 OpenAPI JSON 响应如果包含 `sign`，会按 `xxx_response` 节点原始 JSON 值进行验签。

## 支付宝账户连通性检查

可在不写入数据库、不保存密钥的情况下，用环境变量临时检查应用私钥、应用公钥和支付宝公钥是否可用：

```bash
ALIPAY_APP_ID=2021000000000000 \
ALIPAY_MERCHANT_PRIVATE_KEY='你的应用私钥' \
ALIPAY_APP_PUBLIC_KEY='你的应用公钥' \
ALIPAY_PUBLIC_KEY='支付宝公钥' \
python scripts/check_alipay_account.py
```

脚本会先做本地 RSA2 签名/验签，再用随机不存在的商户订单号调用 `alipay.trade.query`。如果返回 `ACQ.TRADE_NOT_EXIST`，表示签名、网关连通性和支付宝响应验签均通过，测试订单不存在是预期结果。

## 支付宝开放平台配置

支付宝开放平台公共能力文档入口：https://opendocs.alipay.com/common

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

## 运维检查

- 健康检查端点：`/healthz`，正常返回 `ok`。
- `docker-compose.yml` 已内置健康检查，便于反向代理或容器平台判断服务状态。

## 订单状态说明

- `WAIT_BUYER_PAY`：等待买家付款
- `TRADE_SUCCESS` / `TRADE_FINISHED`：支付成功
- `TRADE_CLOSED`：交易关闭，或超过面板设置的订单超时时间后自动关闭
- `FAILED`：创建收款请求失败，详情页会显示错误

## 密钥存储与订单清理

- 支付宝应用私钥和支付宝公钥写入账户配置时会以 `enc:v1:` 格式加密存储，启动时也会自动迁移旧的明文账户密钥。
- 加密密钥派生自 `APP_SECRET_KEY`；部署后请妥善备份 `.env`，如果更换 `APP_SECRET_KEY`，已加密的账户密钥将无法解密。
- 订单页底部提供“订单清理”，可选择开始/结束时间清理对应时段订单，也可以一键清除所有订单记录。

## 迁移与备份

该项目默认只依赖环境变量与 SQLite 文件：

```text
.env
data/paypanel.db
```

迁移到新机器时，停止服务后复制以上文件，再启动服务即可。

## 安全建议

- 使用 HTTPS 暴露面板和回调地址。
- 修改默认管理员密码并设置高强度 `APP_SECRET_KEY`。
- 开启 2FA 前先在设置页生成 TOTP 密钥并用认证器保存。
- 私钥仅保存在你的服务器 SQLite 数据库中，建议限制文件权限并定期备份。
