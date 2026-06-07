# HeroSMS Local API

这是一个纯本地 API 服务，不再依赖前端页面。

服务职责：

- 按配置文件购买新号码
- 获取当前配置对应的已购号码
- 按手机号获取验证码
- 按手机号取消/结束使用号码
- 所有号码状态以上游 HeroSMS 为准

## 1. 配置

### 环境变量

直接创建或编辑 `.env`，至少填写：

```env
HERO_SMS_API_KEY=你的HeroSMS密钥
```

可选：

```env
HOST=0.0.0.0
PORT=3030
ADMIN_PASSWORD=你的控制面板管理员密码
PURCHASE_CONFIG_FILE=purchase_config.json
TEMP_MAIL_API_URL=https://mail-api.example.com
TEMP_MAIL_ADMIN_PASSWORD=CHANGE_ME_TEMP_MAIL_PASSWORD
CPA_BASE_URL=https://cpa-admin.example.com
CPA_MANAGEMENT_KEY=CHANGE_ME_CPA_KEY
SIGNUP_PASSWORD=ChangeMe123456
SIGNUP_NAME=Test User
SIGNUP_AGE=22
BROWSER_PROXY=http://127.0.0.1:8080
UC_SIGNUP_PROXY=http://127.0.0.1:8080
```

如果设置了 `ADMIN_PASSWORD`，访问 `/ui` 后需要先输入管理员密码，控制面板调用的 API 也会要求同一登录会话。未设置时保持无密码模式。

默认服务已经内置为 OpenAI / `dr`。现在推荐优先用启动器里的“设置”页维护购买组，保存后会写入 `purchase_config.json`。

购买相关配置现在不再依赖 `.env`，统一由 `purchase_config.json` 持久化保存并驱动购买顺序与回退。

### 购买参数配置文件

编辑 [purchase_config.json](C:/Users/admin/Desktop/sms/purchase_config.json)：

```json
{
  "serviceName": "OpenAI",
  "serviceCode": "dr",
  "purchaseGroups": [
    {
      "label": "Brazil VIVO exact 0.0262",
      "enabled": true,
      "countryName": "Brazil",
      "countryCode": "73",
      "operator": "vivo",
      "fixedPrice": true,
      "exactPrice": "0.0262",
      "maxPrice": ""
    },
    {
      "label": "Brazil TIM max 0.03",
      "enabled": true,
      "countryName": "Brazil",
      "countryCode": "73",
      "operator": "tim",
      "fixedPrice": false,
      "exactPrice": "",
      "maxPrice": "0.03"
    }
  ]
}
```

说明：

- 服务端会按 `purchaseGroups` 数组顺序依次购买；前一组没号或失败时会自动试下一组
- `serviceCode` 默认就是 `dr`，但现在建议显式写在顶层，避免配置含义不清
- `fixedPrice=true` 时，服务会走 `getNumber` + `fixedPrice=true`
- `exactPrice` 是你要锁定的价位
- `GET /api/current-phone` 会按所有已启用购买组一起筛选当前号码
- 如果不想锁定精确价位，把 `fixedPrice` 改成 `false`，然后填写 `maxPrice`

## 2. 启动

### Linux 网页控制面板 + VNC 浏览器

Linux 下不再依赖 Windows Tkinter 启动器。后端会直接提供网页控制面板：

```bash
python3 server.py
```

默认访问：

```text
http://127.0.0.1:3030/ui
```

控制面板包含左侧菜单：注册任务查看、号码工具、设置、输出。移动端会使用可折叠抽屉菜单；注册任务页只保留任务状态、邮箱生成、开启/停止任务和日志，不再嵌入 VNC。

如果需要在远程 VNC 里显示浏览器，先安装系统依赖：

```bash
sudo apt-get update
sudo apt-get install -y python3 xvfb x11vnc novnc websockify openbox chromium-browser
```

然后用脚本启动 Xvfb、x11vnc、noVNC 和本地服务：

```bash
./scripts/start_linux_vnc.sh
```

常用环境变量：

```env
BROWSER_DISPLAY=:1
BROWSER_COMMAND=chromium-browser
VNC_WEB_URL=http://你的服务器IP:6080/vnc.html?autoconnect=1&resize=remote
VNC_PORT=5901
NOVNC_PORT=6080
VNC_RESOLUTION=1440x900x24
```

网页控制面板提供：

- 查看服务状态、余额、当前号码
- 按 `purchase_config.json` 购买号码、获取验证码、取消/完成号码
- 编辑并保存购买组配置
- 启动/停止 Linux 图形浏览器，浏览器画面通过 VNC/noVNC 查看

浏览器自动化脚本 `chatgpt_signup_to_code.py` 原本依赖 Windows Edge 和 `uiautomation`，Linux 下不作为自动注册入口使用。Linux 版本建议通过 `/ui` 启动浏览器，在 VNC 中人工操作页面，接码和验证码通过控制面板完成。

### 仅启动 API

```bash
python3 server.py
```

默认监听：

```text
http://127.0.0.1:3030
```

局域网内其他机器访问：

```text
http://你的局域网IP:3030
```

## 3. API 总览

基础地址：

```text
http://127.0.0.1:3030/api
```

### 3.1 健康检查

```http
GET /api/health
```

### 3.2 查看当前购买配置

```http
GET /api/config
```

### 3.3 查看余额

```http
GET /api/balance
```

### 3.4 按配置购买新号码

```http
POST /api/purchase
Content-Type: application/json
```

空请求体即可直接按 `purchase_config.json` 里当前保存的购买组顺序购买：

```json
{}
```

返回示例：

```json
{
  "filters": {
    "serviceCode": "dr",
    "countryCode": "73",
    "operator": "vivo",
    "fixedPrice": "true",
    "exactPrice": "0.0262"
  },
  "item": {
    "id": "407155419",
    "phoneNumber": "5521979950988"
  }
}
```

### 3.5 获取当前配置对应的最新号码

```http
GET /api/current-phone
```

这个接口会按 `purchase_config.json` 中所有已启用购买组的配置：

- `serviceCode`
- `countryCode`
- `operator`
- `exactPrice`

去上游活跃号码里筛，返回最新一条。

### 3.6 按手机号查看当前号码信息

```http
GET /api/phones/{phone}
```

示例：

```http
GET /api/phones/5521979950988
```

### 3.7 按手机号获取验证码

```http
GET /api/phones/{phone}/code
```

示例：

```http
GET /api/phones/5521979950988/code
```

### 3.8 按手机号取消使用号码

```http
POST /api/phones/{phone}/cancel
```

示例：

```http
POST /api/phones/5521979950988/cancel
```

### 3.9 按手机号结束使用号码

```http
POST /api/phones/{phone}/finish
```

### 3.10 按手机号重置为等待验证码

```http
POST /api/phones/{phone}/ready
```

## 4. 推荐调用流程

### 购买新号码

```bash
curl -X POST http://127.0.0.1:3030/api/purchase ^
  -H "Content-Type: application/json" ^
  -d "{}"
```

### 获取当前买到的号码

```bash
curl http://127.0.0.1:3030/api/current-phone
```

### 用手机号获取验证码

```bash
curl http://127.0.0.1:3030/api/phones/5521979950988/code
```

### 用手机号取消号码

```bash
curl -X POST http://127.0.0.1:3030/api/phones/5521979950988/cancel
```

## 5. 说明

- 现在页面是否打开已经不重要，核心是本地 API。
- 查询、验证码、取消等操作全部以上游 HeroSMS 当前活跃号码为准。
- 本地不再要求你自己传 `activationId`；本地 API 会先用手机号映射回上游激活单，再代你调用 HeroSMS。
- 如果某个手机号在上游已经不再活跃，按手机号查询/取码/取消会返回 `404`。

## 6. 临时邮箱 API 集成

这部分是对你部署的临时邮箱后端做本地代理，当前支持：

- 创建邮箱
- 按邮箱地址读取邮件列表
- 按邮箱地址读取最新一封邮件
- 删除邮箱

### 6.1 环境变量

在 `.env` 里增加：

```env
TEMP_MAIL_API_URL=https://mail-api.example.com
TEMP_MAIL_ADMIN_PASSWORD=CHANGE_ME_TEMP_MAIL_PASSWORD
```

### 6.2 获取邮箱站点设置

```http
GET /api/temp-mail/settings
```

### 6.3 创建邮箱

```http
POST /api/temp-mail/address
Content-Type: application/json
```

请求体可选：

```json
{
  "name": "apitest001",
  "domain": "example.com",
  "enablePrefix": true
}
```

如果不传，服务会自动取临时邮箱站点的默认域名，并生成一个随机前缀名。

### 6.4 查看邮件列表

```http
GET /api/temp-mail/address/{address}/mails
```

示例：

```http
GET /api/temp-mail/address/apitest001@example.com/mails
```

支持查询参数：

- `limit`
- `offset`

例如：

```http
GET /api/temp-mail/address/apitest001@example.com/mails?limit=10&offset=0
```

### 6.5 查看最新一封邮件

```http
GET /api/temp-mail/address/{address}/mails/latest
```

示例：

```http
GET /api/temp-mail/address/apitest001@example.com/mails/latest
```

### 6.6 删除邮箱

```http
DELETE /api/temp-mail/address/{address}
```

示例：

```http
DELETE /api/temp-mail/address/apitest001@example.com
```

### 6.7 curl 示例

创建邮箱：

```bash
curl -X POST http://127.0.0.1:3030/api/temp-mail/address ^
  -H "Content-Type: application/json" ^
  -d "{\"name\":\"apitest001\",\"domain\":\"example.com\",\"enablePrefix\":true}"
```

查看最新邮件：

```bash
curl http://127.0.0.1:3030/api/temp-mail/address/apitest001@example.com/mails/latest
```

删除邮箱：

```bash
curl -X DELETE http://127.0.0.1:3030/api/temp-mail/address/apitest001@example.com
```

## 7. Codex OAuth 管理 API 代理

这部分是对你提供的 CPA 管理接口做本地代理，方便你统一从当前服务调用。

前提：

- CPA 服务本身已启动
- 你的 CPA 管理端地址是 `https://cpa-admin.example.com`
- `.env` 已配置：

```env
CPA_BASE_URL=https://cpa-admin.example.com
CPA_MANAGEMENT_KEY=CHANGE_ME_CPA_KEY
```

### 7.1 发起 Codex OAuth

```http
GET /api/codex-oauth/url
```

返回示例：

```json
{
  "status": "ok",
  "url": "https://auth.openai.com/oauth/authorize?...",
  "state": "xxxx"
}
```

### 7.2 手工回填 OAuth 回调

```http
POST /api/codex-oauth/callback
Content-Type: application/json
```

请求体支持两种：

完整回调地址：

```json
{
  "provider": "codex",
  "redirect_url": "http://localhost:1455/auth/callback?code=XXX&state=YYY"
}
```

或者显式字段：

```json
{
  "provider": "codex",
  "code": "XXX",
  "state": "YYY"
}
```

### 7.3 查询认证状态

```http
GET /api/codex-oauth/status?state=YYY
```

### 7.4 查看已落盘凭证

```http
GET /api/codex-oauth/files
```

### 7.5 推荐流程

1. 发起登录

```bash
curl http://127.0.0.1:3030/api/codex-oauth/url
```

2. 浏览器打开返回里的 `url`

3. 登录成功后，复制浏览器地址栏里的完整回调地址：

```text
http://localhost:1455/auth/callback?code=...&state=...
```

4. 手工回填：

```bash
curl -X POST http://127.0.0.1:3030/api/codex-oauth/callback ^
  -H "Content-Type: application/json" ^
  -d "{\"provider\":\"codex\",\"redirect_url\":\"http://localhost:1455/auth/callback?code=XXX&state=YYY\"}"
```

5. 查询状态：

```bash
curl "http://127.0.0.1:3030/api/codex-oauth/status?state=YYY"
```

6. 查看凭证文件：

```bash
curl http://127.0.0.1:3030/api/codex-oauth/files
```

### 7.6 说明

- 当前代理层只保留“手工回填回调 URL”的远程模式。
- 不支持同机浏览器自动回调模式。
- `status=ok` 通常表示流程已完成，但最终最好再查一次 `/api/codex-oauth/files` 确认凭证真的已经落盘。

## 8. Edge 自动注册与 Codex 授权

仓库里还有一个独立脚本：

```text
chatgpt_signup_to_code.py
```

作用：

- 打开真实的 Edge 无痕窗口
- 进入 ChatGPT 手机号注册流程
- 如果你不传 `--phone`，会自动调用本地 API 购买新号码
- 自动从本地 API 轮询短信验证码
- 如果你不传 `--email`，会自动调用本地临时邮箱 API 创建邮箱
- 自动从本地临时邮箱 API 轮询邮箱验证码
- 自动填写密码、姓名、年龄
- 到 Codex 这一步时，如果你没传 `--oauth-url`，脚本会自动从本地 `/api/codex-oauth/url` 获取授权链接
- 浏览器跳到本地回调地址后，脚本会自动把完整 `redirect_url` 回填到本地 `/api/codex-oauth/callback`
- 然后脚本会自动查询 `/api/codex-oauth/status` 和 `/api/codex-oauth/files`
- 最终在终端打印 `http://localhost:1455/auth/callback?...` 回调地址

依赖：

```bash
python -m pip install uiautomation
```

只跑注册，手机号和邮箱都走本地 API：

```bash
python chatgpt_signup_to_code.py
```

如果你想直接用一个简单桌面启动器：

```bash
python launcher.py
```

启动器行为：

- 打开后会自动检查并启动 `server.py`
- 窗口默认固定在屏幕右侧并保持置顶，尽量不挡住浏览器中间区域
- UI 会显示当前进度 `已完成/总数`，以及当前正在注册的邮箱
- 支持两种邮箱列表生成方式：顺序前缀、随机前缀
- 顺序前缀模式下，输入第一个邮箱例如 `user001@example.com` 和总数后，会自动生成 `user001@example.com`、`user002@example.com` 这样的列表
- 随机前缀模式下，不需要输入第一个邮箱，只要填域名和总数，就会自动生成随机前缀邮箱列表
- 生成后的邮箱列表仍可手动编辑，一行一个
- 全部留空时会自动创建临时邮箱跑 1 次
- 每个邮箱注册完成后，会自动关闭这次无痕窗口并继续下一个
- 日志会实时显示注册脚本输出

注册脚本默认轮询策略：

- 短信验证码：每 15 秒查询 1 次，最多 6 次；还没有就取消手机号并重新开始这一轮
- 如果配置了多个购买组，某个手机号取消后，下一次买号会优先从列表里的下一组继续，按顺序循环
- 邮箱验证码：每 10 秒查询 1 次，最多 3 次；还没有就关闭当前无痕窗口并跳过这个邮箱

注册后继续跑 Codex OAuth：

```bash
python chatgpt_signup_to_code.py
```

如果你想强制指定外部生成的授权链接，也仍然可以手动传：

```bash
python chatgpt_signup_to_code.py ^
  --oauth-url "这里填完整 authorize 链接"
```

说明：

- 这个脚本依赖 Windows 下 Edge 的真实界面和当前中文控件文案。
- 如果 OpenAI 改了注册流程、页面文案或控件结构，脚本可能需要跟着改。
- 默认情况下，不需要你手动提供 `--oauth-url`；脚本会从本地 Codex OAuth 管理 API 自动获取。
- 如果 `.env` 里已配置 `SIGNUP_PASSWORD`、`SIGNUP_NAME`、`SIGNUP_AGE`，脚本会直接使用，不需要再传这三个参数。
- 如果 `.env` 和命令行里都没有这些值，脚本才会在终端里向你询问。
- 本地 API 没有准备好时，脚本会先尝试自动拉起同目录下的 `server.py`。
