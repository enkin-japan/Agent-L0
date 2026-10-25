# 设计：iPhone 与 Mac 先握手、由 Mac 同意，再按设备单独发票据（Phase B）

> 状态：**第二稿，已获用户确认，已实现**（2026-10-25）。下文已按实现同步，例如查询接口改成了 POST，详见第 6 节。属于 build 76 计划
> （`docs/plans/2026-10-25-iphone-multi-device-build76.md`）的 Phase B。
> 第一稿在 iCloud 是否发票据、有效期、设备名三个问题上请用户拍板，用户的回答是：
> 1. 每部 iPhone 第一次连接都要和 Mac 握手一次，由 Mac 同意。同一 Apple ID 的不用扫码，不同 Apple ID 的要扫码。这样踢掉某一台的逻辑就顺了。
> 2. 有效期做成开关，默认 90 天；在设备列表里可以对单台设备取消期限，改成永久。
> 3. 有默认名字，iPhone 和 Mac 两端都可以给对方改名。
>
> 本稿按这三条重写。

## 1. 目标

- 每部 iPhone 都要经 Mac **明确同意**才能连上，并拿到**它自己的一张票据**。
- 踢掉一台 = 在 Mac 的设备列表里吊销它，立即生效，不牵连别的设备，也不用轮换共享密钥。
- **共享密钥（AGENT 密钥）不再离开 Mac**：iCloud 里不再放它，扫码也不再换出它。

## 2. 两条入口，同一个结局

| | 同一 Apple ID | 不同 Apple ID |
|---|---|---|
| iPhone 怎么找到 Mac | 读 iCloud 钥匙串里这台 Mac 的条目（Phase A 已做好多 Mac 列表） | 用相机扫 Mac 上的二维码 |
| Mac 怎么表示同意 | Mac 上弹出「『iPhone』请求连接」，你点**允许** | 你在 Mac 上亲手打开二维码，这本身就是同意（不再额外弹框） |
| 结局 | iPhone 拿到自己的设备票据 | 同左 |

### 2.1 同一 Apple ID：握手流程

1. Mac 写进 iCloud 的条目从 `{url, key, mac, macId}` 改为 `{url, mac, macId, ticket}`。
   - `ticket` 是一张「请求凭据」，随机 256 bit。它**只能用来提交连接请求**，不能调用任何别的接口。
   - 它的作用是证明「这台 iPhone 能读到这台 Mac 的 iCloud 条目，也就是同一个 Apple ID」，挡住 tailnet 上其他设备拿请求去骚扰 Mac。
2. iPhone 在列表里点这台 Mac 时，发送 `POST /api/v1/pair/request {ticket, name, platform, push_device_id?}`。
   - 后端校验 ticket，生成一条待处理请求，返回 `{request_id, poll_secret, code}`。
   - `code` 是 4 位核对码。
3. Mac app 发现有待处理请求，弹出窗口（app 不在前台时发本机通知）：
   - 内容：「『iPhone』请求连接这台 Mac。核对码 4821。」，按钮为「允许」和「拒绝」。
   - iPhone 上同时显示「请在 Mac 上点允许，核对码 4821」。核对码用来确认 Mac 上弹出的就是你手上这部 iPhone 发的请求。
4. iPhone 用 `poll_secret` 轮询 `POST /api/v1/pair/request/{id}/poll`，大约每 2 秒一次，最多等 5 分钟。poll_secret 放在请求体里，不放在 URL 里。
   - Mac 点了允许：返回一次 `{token, device_id, instance_id}`，之后这条请求立即作废。
   - Mac 点了拒绝，或超时：返回明确的失败原因，iPhone 提示可以重试。
5. 防骚扰：
   - 同时最多 3 条待处理请求，同一 ticket 每分钟最多 3 次请求；
   - 无 ticket 或 ticket 错误一律 403，Mac 端不弹任何东西；
   - Mac 设置里可以「重新生成请求凭据」，旧 ticket 和所有待处理请求随即作废。

### 2.2 不同 Apple ID：扫码

- 现有流程不变：Mac 显示二维码（一次性配对码，144 bit，5 分钟有效，用一次即作废），iPhone 扫码后确认完整地址。
- 变化在于 `POST /api/v1/pair/claim` 的返回值：从共享密钥改成**为这部 iPhone 新签的设备票据** `{token, device_id, instance_id}`。

## 3. 设备票据与存储

- 票据：随机 256 bit。只在发放的那一次响应里出现，响应带 `Cache-Control: no-store`。
- 后端存储位置：数据根下 `auth/devices.json`。目录 0700、文件 0600，写入用「临时文件 → fsync → `os.replace`」原子替换。

```json
{"v": 1, "default_ttl_days": 90, "devices": [
  {"id": "d_7Kq…", "token_sha256": "<64 hex>", "name": "iPhone（10-25 连接）",
   "via": "icloud", "platform": "ios", "created": 1761400000, "last_used": 1761400500,
   "expires": 1769176000, "push_device_id": "…"}
]}
```

- 文件里**只存哈希**，从不存明文票据。日志只记 `device_id`。
- `last_used` 先在内存里更新，最多每 10 分钟落一次盘。
- **文件损坏时一律 fail-closed**：
  - 所有设备票据都返回 401；共享密钥照常可用，Mac 本身不受影响。
  - 损坏的文件改名为 `devices.json.corrupt-<时间>` 留存，再从空表开始。
  - 启动日志和 Mac 设置页明确提示「已连接设备记录损坏，需要重新连接」。
- 旧的 `auth.devices` 配置（明文票据）保留兼容。

## 4. 有效期

- 全局开关在 Mac 设置里：「新设备的连接有效期」，**默认开，90 天**。关掉表示新设备默认永久有效。
- 设备列表里每台设备都显示剩余天数，可以单独改成「永久」，也可以改回有一个期限。
- 到期后后端返回 401，原因是 `device_expired`。iPhone 提示「与『Mac 名』的连接已到期」：
  - 同一 Apple ID：点一下重新发起请求，由 Mac 同意；
  - 不同 Apple ID：重新扫码。
- 有效期从 Mac 同意的那一刻开始算，**不随使用自动延长**。这样到期确实意味着需要重新确认一次。

## 5. 名字

- 默认名：
  - iPhone 在 Mac 设备列表里显示为「iPhone（10-25 连接）」，iPad 同理。iOS 16 起拿不到用户起的设备名，所以只能用机型加日期。
  - Mac 在 iPhone 的列表里显示 tailnet 名，例如「m6」。
- 改名：
  - Mac 端给 iPhone 改名：改的是后端设备记录，接口为 `PATCH /api/v1/pair/devices/{id} {name}`。
  - iPhone 端给 Mac 改名：只改 iPhone 本地的显示名（Phase A 的 SavedMac.name），不影响别的设备。
  - 一旦改过名，iCloud 同步不再覆盖这个名字。

## 6. 接口一览

| 接口 | 鉴权 | 说明 |
|---|---|---|
| `POST /api/v1/pair/request` | iCloud ticket | 新增：提交连接请求 |
| `POST /api/v1/pair/request/{id}/poll` | poll_secret | 新增：查询结果，同意后只返回一次票据 |
| `POST /api/v1/pair/claim` | 一次性配对码 | 返回值改为设备票据 |
| `POST /api/v1/pair/start` | **仅共享密钥** | 设备票据不能生成配对码 |
| `GET /api/v1/pair/requests` · `POST …/{id}/approve` · `POST …/{id}/deny` | 仅共享密钥 | 新增：Mac 处理待处理请求 |
| `GET /api/v1/pair/devices` · `PATCH …/{id}` · `DELETE …/{id}` | 仅共享密钥 | 新增：列表、改名或改有效期、吊销（吊销会同时注销推送） |
| `GET /api/v1/pair/ticket` · `POST /api/v1/pair/ticket/rotate` | 仅共享密钥 | 新增：Mac 读取请求凭据写进 iCloud；重新生成（Mac 关闭同步或停止开放时也会自动重新生成） |
| `PUT /api/v1/pair/settings {default_ttl_days}` | 仅共享密钥 | 新增：新设备默认有效期（必须显式给出，null = 永久） |
| `DELETE /api/v1/pair/self` | 设备票据 | 新增：iPhone 删除这台 Mac 时吊销自己的票据 |

- 「仅共享密钥」的含义：通过 `verify_api_key` 之后，还要满足 `request.state.device_id == "default"`，否则返回 403。新增依赖 `require_owner` 来做这件事。
- 免鉴权白名单从「只有 claim」扩展为 **claim、request、request/{id}/poll 三条**。这三条各自凭一次性码、ticket 或 poll_secret 校验，`test_pairing.py` 的白名单测试同步更新。
- PROTOCOL.md §2.9 相应更新。claim 的返回值不再兼容 build 75，影响只在 TestFlight 内部，届时 iPhone 一并升级到 76 即可。

## 7. 迁移（已经拿着共享密钥的 iPhone）

- build 75 和 Phase A 的 iPhone 手里拿的是共享密钥。升级到 76 后，app 会自动调用 `POST /api/v1/pair/upgrade`（共享密钥鉴权），换一张自己的设备票据。
  - 这一步不再弹出同意框：这部 iPhone 早就被信任过了。
  - 换到票据后，iPhone 删掉本机保存的共享密钥。
- Mac 设备列表里显示「仍有 N 台设备用旧方式连接」。等全部换完，建议轮换一次共享密钥，彻底收回散落在外的副本。
- iCloud 里 75 格式的全局条目（含共享密钥）在 76 中**不再双写**，本机写的那条直接删除。
  - 这会推翻 Phase A 审查后为兼容 75 版 iPhone 加的过渡双写。理由：共享密钥不应该再出现在 iCloud 里，而 75 只在 TestFlight 内部，iPhone 跟着升到 76 即可。

## 8. 推送与设备的关联

- 设备票据鉴权的 `POST /push/register` 请求，会把 `request.state.device_id` 记进推送注册表条目。
- 吊销一台设备时，同时删掉它的推送登记；它已开着的灵动岛活动也不会再收到更新。

## 9. 验收（每条都做变异证明）

- 没有 ticket 或 ticket 错误：request 返回 403，Mac 端不出现任何待处理请求。
- 待处理请求未经同意：查询结果永远不含票据；被拒绝或超时后作废；同意后票据只返回一次。
- 待处理请求和请求频率的上限都生效。
- 吊销后立即 401；过期后 401 且原因是 `device_expired`；改成永久后不再过期。
- 文件里只有哈希；文件损坏、结构或字段类型不对时设备票据一律 401，共享密钥照常可用。
- 用设备票据访问 `require_owner` 保护的接口一律 403。
- 免鉴权白名单恰好是上面三条。
- 响应带 `no-store`；日志里不出现任何票据、ticket 或 poll_secret。
- 吊销会连带删除推送登记；并发写入不丢记录。

属于 CLAUDE.md 所说的密钥处理类改动，当前会话就是 Opus。出现分类器降级就停下。
