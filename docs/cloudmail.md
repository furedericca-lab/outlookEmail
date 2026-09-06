# cloud-mail 临时邮箱（maillab/cloud-mail）

把自建的 [cloud-mail](https://github.com/maillab/cloud-mail) 接成本系统的临时邮箱提供商。
它与 GPTMail / DuckMail / Cloudflare Temp Email 并列，创建出来的地址同样进入临时邮箱列表、
按同一套接口读取邮件与详情。

## 前提

- cloud-mail 已部署，并且**开放接口可用**（`/api/public/genToken`、`/api/public/addUser`、`/api/public/emailList`）。
- cloud-mail 的所有接口都在 `/api` 前缀下；鉴权头是 `Authorization`，值为 `genToken` 返回的公开令牌。
- cloud-mail **即使业务失败也返回 HTTP 200**，成败只看响应体信封 `{code, message, data}`，本渠道按此判断。
- 收信域名必须在 cloud-mail 服务端的 `domain` 配置里，否则创建地址会被拒绝。

## 配置

按 `mail_provider` 的既有习惯，本渠道用设置项而不是渠道表（cloud-mail 一个实例只有一套开放接口）。
配置全部走接口，不写入仓库、不出现在日志里：

| 设置项 | 说明 |
|---|---|
| `cloudmail_enabled` | `true` / `false`，未开启时不允许创建地址 |
| `cloudmail_base_url` | **服务地址**，必须是 cloud-mail 的**接口地址**，例如 `https://mail-api.example.com` |
| `cloudmail_api_prefix` | 默认 `/api`，一般不用改 |
| `cloudmail_admin_email` | cloud-mail 的管理员邮箱（`genToken` 用它换令牌） |
| `cloudmail_admin_password` | 管理员密码，落库加密 |
| `cloudmail_domain` | 默认收信域名，创建时可被单次请求覆盖 |

~~~bash
# 保存配置（只回是否已配置，不回密码与令牌）
curl -sS -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"enabled":true,"base_url":"https://mail-api.example.com","admin_email":"admin@example.com","admin_password":"<管理密码>","domain":"mail.example.com"}' \
  https://outlookemail.example.com/api/cloudmail/settings

# 自检：取一次令牌；带 email 时再拉一次该地址的邮件
curl -sS -b cookies.txt -H 'Content-Type: application/json' -d '{}' \
  https://outlookemail.example.com/api/cloudmail/test
~~~

## 使用

~~~bash
# 创建（username 留空则随机生成，domain 留空用默认收信域名）
curl -sS -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"provider":"cloudmail","username":"reader01"}' \
  https://outlookemail.example.com/api/temp-emails/generate

# 列表 / 详情与其它提供商完全一致
GET /api/temp-emails/<邮箱地址>/messages
GET /api/temp-emails/<邮箱地址>/messages/<消息ID>
~~~

### 删除地址

开放接口本身没有删除能力，所以删除走**管理员登录态**：`POST /api/login` 拿 JWT（与开放接口令牌分开，
单独密文缓存在设置里），再 `GET /api/user/list` 把邮箱解析成 `userId`，最后 `DELETE /api/user/delete?userIds=<id>`。
服务端是**硬删除**，因此加了三条硬规则：

- `/user/list` 的 `email` 参数是**前缀 LIKE**，适配器会在本地再精确比一次，只有完全相等才算命中；
- 命中不唯一（同名多行）直接拒绝，绝不“猜一个 id”去删；
- 等于配置里管理员邮箱的地址一律拒删，不会删掉管理员账号本身。

上游删除失败不会阻断本地删除（与其它提供商一致），但会写一条 WARNING 日志说清原因。
改密码或换服务地址时，两个缓存令牌（开放接口 + 管理员）会一起作废。

## 分页

`emailList` 只有页码 `num` 与页大小 `size`（服务端把 `size` 卡在 50），没有 offset，适配器负责把
调用方的 `limit/offset` 翻译正确：

- offset 是页大小整数倍 → 直接取对应那一页（一次请求）；
- offset 与页边界不对齐 → 从前往后逐页走到位再切片，最多 10 页，宁可少给也不要无限翻页；
- 单页上限 200，超过部分不往上游发。

## 边界
- **域名由服务端决定**：`cloud-mail` 的 `domain` 里没有的域名会被拒绝创建。
- 界面入口尚未加入 cloud-mail 选项，当前通过上面的接口完成配置与创建。
- 管理员 JWT 通过 `/api/login` 获取并密文缓存；如果你的 cloud-mail 开了 Turnstile 人机验证，
  登录可能需要额外参数，这种情况下管理员登录会失败，删除与用户列表功能会退化为“仅本地”。

## 常见错误

把**前端页面域名**当成 `cloudmail_base_url` 是最容易踩的坑：页面域名对任意路径都返回
`200 + HTML`，此时接口会明确报

> cloud-mail 返回的不是 JSON。请确认服务地址填的是接口地址（例如 https://mail-api.example.com），而不是前端页面域名

看到这条就改地址，不用去怀疑令牌或密码。
