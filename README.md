# dc-digest

按指定时间窗读取 Discord 频道历史消息，逐条入库，再把该窗口的聊天记录交给 `spi`（pi 的无头模式）总结成中文简报，落成本地 Markdown 并推送到飞书。

一条命令的完整链路：

```
fetch（REST 分页 + 拟人节奏 → SQLite） → media（图片按节奏下载到本地） → digest（库内窗口 + 附图 → spi 摘要 → Markdown） → push（飞书卡片）
```

需要判断某个频道 ID 怎么取、token 放哪里、简报为什么标「未抓取完整」时，按下面的章节读；日常使用只看「命令」一节。

## 安装

```bash
git clone <本仓库> ~/bin/dc-digest   # 或直接进入已有目录
cd ~/bin/dc-digest
cp config.example.toml config.toml    # config.toml 含本机信息，已被 .gitignore 忽略
uv sync
uv run dc-digest doctor               # 检查配置、token、飞书凭证、prompt 模板
```

依赖完全隔离在这个项目内，不影响系统或全局 Python：

| 机制 | 效果 |
| --- | --- |
| `pyproject.toml` 声明依赖，`uv.lock` 锁定版本 | 运行依赖只有 `httpx`，开发依赖 `pytest` / `ruff` 放在 `[dependency-groups] dev` |
| `uv sync` 在项目内建 `.venv` | `include-system-site-packages = false`，看不到宿主 site-packages |
| `.python-version` = `3.12` | 由 uv 自带的独立 CPython 提供，不碰 `/opt/homebrew/bin/python3` |
| 一律 `uv run <命令>` | 用的是项目 `.venv`，不要直接调 `python3` / `pip` |
| 新增或升级依赖 | `uv add <包名>`、`uv add --dev <包名>`、`uv lock --upgrade` |

验证隔离是否生效：

```bash
uv run python -c "import sys; print(sys.prefix, sys.base_prefix)"
```

`sys.prefix` 指向 `~/bin/dc-digest/.venv`，`sys.base_prefix` 指向 `~/.local/share/uv/python/...`。删掉 `.venv` 不影响任何全局环境，`uv sync` 可随时重建。

## 准备

### 1. 取服务器 ID 与频道 ID

方式 A，在 Discord 客户端里：

1. 用户设置 → 高级 → 打开「开发者模式」。
2. 右键服务器图标 → 复制服务器 ID。
3. 右键频道名 → 复制频道 ID。

方式 B，用本工具自带的命令（需要先完成第 2 步的 token）：

```bash
uv run dc-digest guilds                 # 列出服务器及 ID
uv run dc-digest guilds --guild <服务器ID>  # 列出该服务器下的文字频道及 ID
```

拿到 ID 后写进 `config.toml`：

```toml
[[channels]]
guild_id = "123456789012345678"
channel_id = "234567890123456789"
name = "#dev"
```

`guild_id` 只用于记账，`name` 只用于简报里的可读标签，抓取实际只用 `channel_id`。

### 2. Discord token

用户 token 的下发方式：浏览器登录 Discord，打开开发者工具 → Network，任意一个 `/api/` 请求的 `Authorization` 请求头就是 token，注意它不带 `Bot ` 前缀。

存进 macOS Keychain（推荐，token 不出现在命令行历史与配置里）：

```bash
security add-generic-password -a "$USER" -s dc-digest-discord-token -w '<token>'
```

或临时用环境变量：`export DC_DIGEST_DISCORD_TOKEN='<token>'`。

改成官方机器人 token 时把 `[discord] auth` 设为 `bot`，同时在 Discord Dev Portal 打开 Message Content privileged intent，否则消息正文为空。

### 3. 飞书推送

默认复用 pi-channel 扩展已经配好的飞书应用凭证（`feishu.appId` / `feishu.appSecret` / `feishu.defaultReceiver`）：

| 配置项 | 作用 |
| --- | --- |
| `credentials_file` | pi-channel 的 `config.json` 路径 |
| `receiver_value` | 留空时用凭证文件里的 `defaultReceiver.value` |
| `msg_type` | `interactive` 发卡片（支持 Markdown），`text` 发纯文本 |
| `domain` | `feishu`（open.feishu.cn）或 `lark`（open.larksuite.com） |
| `max_chars_per_message` | 单条消息字符上限，超长自动拆成多条 |

不需要推送时把 `[feishu] enabled` 设为 `false`，或用 `--no-push`。

## 命令

| 命令 | 作用 |
| --- | --- |
| `doctor` | 检查配置、token、飞书凭证、prompt 模板是否就绪 |
| `guilds [--guild ID]` | 列出服务器与文字频道 ID |
| `fetch` | 按时间窗抓取消息入库，预算内可续传 |
| `digest` | 用库内消息生成简报并落盘 |
| `media` | 下载窗口内的图片附件，预算内可续传 |
| `run` | fetch + media + digest + 推送 |
| `state` | 查看各频道的抓取窗口与游标 |
| `prune` | 按 `[store] retention_days` 清理过期消息与附件记录 |
| `schedule install\|status\|uninstall\|print` | 安装/查看/卸载 launchd 定时任务 |

时间范围的写法（`--from` / `--to`）：

```bash
uv run dc-digest run --from 2026-09-08 --to 2026-09-08          # 2026-09-08 整天
uv run dc-digest run --from 2026-09-08T10:30 --to 2026-09-08T18:00
uv run dc-digest run --from=-6h                                 # 最近 6 小时（值以 - 开头必须用 = 连接）
uv run dc-digest run --from yesterday --to today                 # 昨天整天
```

`--to` 只写日期时包含该整天（结束于次日零点）。都不传时：有历史运行记录就从上次结束点接着跑，没有就跑昨天整天。

单次抓取窗口跨度**最多 48 小时**：一旦 `--from` 到 `--to`（或到 `now`）超过 48 小时，起点会自动后移到「终点 − 48h」，并在日志里留一行 `窗口跨度超过 48 小时，起点从 … 调整到 …`。这条只作用于会联网的 `fetch` / `run` / `media`；`digest` 只读本地库，可以用任意长的历史区间出简报。

其他开关：

```bash
uv run dc-digest fetch --dry-run            # 只打印计划，不联网
uv run dc-digest digest --dry-run           # 不调用模型，只打印统计与首块片段
uv run dc-digest run --no-push              # 只落盘
uv run dc-digest run --no-media             # 本轮不下载图片
uv run dc-digest media --from yesterday --to today          # 单独补下载图片
uv run dc-digest media --dry-run --from yesterday           # 只列出待下载图片与 URL
uv run dc-digest digest --from ... --push    # 生成后单独推送
uv run dc-digest run --channel 123456789012345678   # 临时只跑某个频道
```

简报默认写入 `./digests/`，单日窗口的文件名是 `YYYY-MM-DD.md`，自定义窗口是 `起点__终点.md`。库文件默认在 `./data/messages.db`。这两个路径都相对 `config.toml` 所在目录解析，所以从任意 cwd 调用结果一致。

日志位置与体量由 `[logs]` 控制（默认 2 MB 上限、保留尾部 200 KB）：

```toml
[logs]
max_bytes = 2000000
keep_bytes = 200000
```

每次启动与退出前都会检查 `data/logs/launchd.log`，超限就就地截断到尾部 `keep_bytes`；`launchd` 以追加方式持有该文件句柄，截断后继续从新末尾写，所以不需要重启任务。

## 工作方式

### 抓取窗口与续传

每条频道记录一份抓取窗口（`--from` ~ `--to`）和一个游标。抓取从窗口末端开始向前翻页，与在客户端里往上滚动的方向一致；每次落页都把游标推进到已抓取的最早一条消息。窗口内抓完标为「完整」，下次同窗口直接跳过。

单轮预算是硬上限（默认 40 次请求 / 2000 条消息 / 15 分钟墙钟）。触顶即保存游标并结束，`state` 里显示「未完成」，再跑一次 `fetch` 从游标继续。简报复算不消耗网络配额，因为它只读库。

### 拟人化取数

用户 token 的行为模式需要像人，而不是像爬虫，相关参数都在 `[fetch]` 里：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `page_size` | 50 | 每页条数，用客户端实际页大小，不用接口上限 100 |
| `min_page_delay` ~ `max_page_delay` | 2.5 ~ 6.0 | 页间随机间隔（三角分布，不是固定周期） |
| `pause_every_pages` / `pause_min` ~ `pause_max` | 5 / 10 ~ 25 | 每 N 页插入一次长停顿 |
| `channel_cooldown_min` ~ `channel_cooldown_max` | 45 ~ 90 | 切换频道前的冷却，频道严格串行 |
| `max_requests_per_run` / `max_messages_per_run` / `max_minutes_per_run` | 40 / 2000 / 15 | 单轮预算 |
| `rate_limit_stop_after` / `rate_limit_backoff_minutes` | 2 / 45 | 连续限流即结束本轮并退避，不重试风暴 |

遇到 429 时：`retry_after` 短则等待后重试，超过 90 秒或累计达到 `rate_limit_stop_after` 就保存游标结束本轮。

### 请求头

REST 请求头全部由 `discord_api.build_headers()` 生成，`doctor` 会回显最终形态：

| 头 | 取值 | 说明 |
| --- | --- | --- |
| `Authorization` | 用户 token 用裸值；机器人 token 用 `Bot <token>` | 前缀加错会直接 401，这是 Discord 区分的唯一方式 |
| `Accept` | `application/json` | |
| `User-Agent` | `dc-digest/0.1.0` | 如实标注本工具，不伪装浏览器 |

不发 `Content-Type`（GET 没有请求体），也不伪造 `X-Super-Properties`、`X-Discord-Locale` 这类客户端指纹：伪造一套与真实客户端版本不一致的指纹，比不发送更容易被识别。`tests/test_discord_api.py` 把这些约束固定成了断言。

### 摘要

transcript 只保留时间、昵称、正文、附件名与回复对象，正文压成单行并截断到 1000 字符。整窗字符数不超过 `summarize.max_chars_per_call` 时一次调用出稿；超过时按块先摘要再合并（`prompts/chunk.md` + `prompts/reduce.md`）。

摘要后端的调用形态是：

```bash
spi -p --no-tools --no-session --no-sandbox --mode text --model opencode-go/deepseek-v4.1-flash @/path/to/图片.png
```

stdin 收 prompt，stdout 出 Markdown，图片以 `@路径` 追加在参数末尾，非零退出或不输出即中断。换模型只改 `[summarize] args`；`--no-sandbox` 关掉 pi 的沙箱扩展，`--no-tools` 让模型没有工具可用，只做纯文本与图片判读。若某台机器没装 sandbox 扩展，pi 会把它当未知参数报错，此时删掉 `--no-sandbox` 即可。

简报正文固定五节：主要议题、结论与决议、待办、链接与资源、值得我回复的点。文件头部带 frontmatter（窗口、时区、频道、消息数、图片数、抓取是否完整、生成时间），供 Obsidian 等工具检索。

### 图片与多模态摘要

聊天里的截图、图表会被真正读进摘要，而不是只记一句「某人发了图」。流程分三段：

| 阶段 | 行为 |
| --- | --- |
| `fetch` | 只记录附件元数据（URL、文件名、大小、是否图片）入库为 `pending`，不下载 |
| `media` | 按节奏下载窗口内待取的图片，落到 `./data/media/<频道ID>/<消息ID>-<序号>-<文件名>`，标记 `ok` |
| `digest` | 窗口内已下载的图片按时间顺序编号，消息行尾标 `[图片 N]`，摘要时把本地文件以 `@路径` 附给 spi |

配置项在 `[media]`：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `enabled` | `true` | 关掉后既不下载图片，也不做多模态摘要 |
| `max_files_per_run` / `max_bytes_per_run` | 40 / 40 MB | 单轮下载预算，触顶后下次续传 |
| `max_bytes_per_file` | 8 MB | 超过即标记 `skipped`，不截断 |
| `min_download_delay` ~ `max_download_delay` | 1.0 ~ 3.0 | 下载间隔随机抖动 |
| `max_images_per_call` | 8 | 单次 spi 调用最多附几张图 |
| `max_images_per_digest` | 30 | 一份简报最多用几张图，超出按时间取前 N 张并在备注里说明 |

行为约定：

- 图片没下完时简报照常生成，但 `fetch_note` 会写「窗口内还有 N 张图片未下载，先运行 media 可补齐」，先补跑 `media` 再重算。
- 摘要提示词要求读出截图里的报错、图表结论与页面要点，看不清时写「图片无法判读」，并禁止把本地文件路径写进简报。
- embed 内嵌图（`image` / `thumbnail`）与普通附件一起收集；PDF 等非图片附件只留文件名。
- 下载失败分两类：`401/403` 立刻中断（凭证或权限问题）；`404/410` 记 `failed` 并继续，不阻塞整轮。
- 网络异常（代理拒绝、连接超时）会在错误信息里给出「优先检查代理是否放行 discord 域名」的提示，图片条目记 `failed` 但简报照常生成。

### 网络与代理

httpx 只读环境变量、**不读 macOS 系统代理设置**，因此在 `[http]` 显式指定：

```toml
[http]
proxy = "http://127.0.0.1:7890"
```

留空则沿用 `HTTPS_PROXY` / `ALL_PROXY`。判断出口是否可达：

```bash
curl -sS -x http://127.0.0.1:7890 -o /dev/null -w "%{http_code}\n" https://discord.com/api/v10/gateway   # 期望 200
curl -sS -x http://127.0.0.1:7890 -o /dev/null -w "%{http_code}\n" https://cdn.discordapp.com/attachments/1/2/x.png  # 期望 404（可达即可）
```

代理规则需要同时放行 `discord.com`、`discord.gg`、`cdn.discordapp.com`、`media.discordapp.net`；只放行主域名时图片下载会失败而消息抓取正常。

### 图片缓存生命周期

图片只是缓存，库里以 Discord 链接为准（见 ADR-0005）：

```bash
uv run dc-digest media --clean --from yesterday --to today   # 删除本地缓存并把记录打回待下载
```

缓存目录被系统清理后，`digest` 会发现文件缺失并自动按节奏重下；离线或不想下载时加 `--no-media`，简报会退化为纯文本版本。

### 库留存

消息与附件记录按 `[store] retention_days`（示例配置 60 天）清理。`run --prune` 会在跑完后顺带清理，`schedule` 安装的定时任务用的就是这条命令；也可以单独触发：

```bash
uv run dc-digest prune --dry-run          # 只统计：将删除多少条消息/附件记录
uv run dc-digest prune                    # 真删
uv run dc-digest prune --retention-days 30
```

图片缓存本身在临时目录，系统会自动回收，不依赖清理。

### 定时任务（launchd）

```bash
uv run dc-digest schedule install --interval-hours 3   # 安装：每 3 小时跑一次 run --prune
uv run dc-digest schedule status                       # 查看是否加载、上次退出码
uv run dc-digest schedule print                        # 只打印 plist，不安装
uv run dc-digest schedule uninstall                    # 卸载
```

行为约定：

| 项 | 说明 |
| --- | --- |
| 执行命令 | `uv run --project <仓库> dc-digest run --prune` |
| 时间窗 | 不传 `--from` 时自动取上次成功运行的结束时间到现在，所以两次运行之间不留缝、不重复 |
| 间隔 | `StartInterval`，默认 3 小时；睡眠错过的会在唤醒后补跑一次 |
| 清理 | 跑完顺带执行 `prune`（留存期看 `[store] retention_days`） |
| PATH | 安装时快照登录 shell 的 PATH，再加上摘要命令所在目录；`spi` 内部还要 `exec pi`、`pi` 又要 `node`（常在 nvm 目录），只写系统目录会 127 |
| 日志 | `data/logs/launchd.log`：launchd 的 stdout 与 stderr 合并写入同一文件 |
| 日志轮转 | 每次启动与结束时检查该文件，超过 `[logs] max_bytes`（默认 2 MB）就地保留尾部 `keep_bytes`（默认 200 KB），长期无人值守磁盘占用有上限 |
| 失败 | 通过飞书提醒（见上一节），退出码可在 `schedule status` 里看到 |

plist 落在 `~/Library/LaunchAgents/local.dc-digest.plist`，内含本机绝对路径，因此不进仓库；仓库里只有生成它的 `schedule` 命令。若执行环境禁止写 `~/Library`（例如在受限沙箱里），先 `schedule print > data/launchd/local.dc-digest.plist` 生成，再手动 `cp` 到该目录并 `launchctl bootstrap gui/$(id -u) <plist>`。

排查定时任务时只需盯一个文件：

```bash
tail -f data/logs/launchd.log
grep -n "ERROR\|WARNING" data/logs/launchd.log | tail -20
```

日志由 launchd 直接接收进程的 stdout 与 stderr，工具本身不另开日志文件；`uv` 的同步信息也会混在里面，属于正常现象。

定时任务里最常见的两类报错都跟 PATH 有关，改完配置或换了 node 版本后重新执行 `schedule install` 即可刷新快照：

| 日志里的症状 | 原因 | 处理 |
| --- | --- | --- |
| `找不到摘要命令 'spi'` | PATH 里没有 `~/bin` | 重新安装刷新 PATH；或把 `[summarize] command` 写成绝对路径 |
| `spi 退出码 127` / `pi: not found` / `node: No such file or directory` | `spi` 找到但它的下游（`pi`、`node`）不在 PATH | 重新 `schedule install`，让 plist 抄下当前登录 shell 的完整 PATH |

### 失败通知

拿不到聊天信息时不会静默失败：任何硬失败都会通过飞书发一条提醒，然后把退出码设为 2。

| 触发场景 | 是否推送提醒 |
| --- | --- |
| token 失效 / 账号被封或强制重登（401） | ✅ 附「重新取 token 并更新 Keychain」建议 |
| 无权访问频道 / 账号受限（403）、频道不存在（404） | ✅ 附对应排查方向 |
| 代理或网络异常、连接超时 | ✅ 附「检查代理是否放行 discord 域名」 |
| 飞书推送本身失败、摘要子进程失败 | ✅ 走同一个提醒通道 |
| 抓取被限流或预算耗尽（未抓齐） | ✅ 标题为「抓取不完整」，含未抓齐的频道与续传建议 |
| 配置读不出来 | ❌ 此时无从得知飞书配置，只写日志 |
| 简报正文推送（正常流程） | 由 `--no-push` 控制，与提醒无关 |

开关：`[alerts] enabled`（默认 true）关闭全部提醒，`--no-alert` 只关闭当次运行。提醒发出后终端日志会多一行 `已通过飞书发出提醒（N 条）`；若连提醒都发不出去，会记 `飞书提醒发送失败：…` 且不影响原始错误信息。

自测提醒通道（把 token 换成无效值，制造一次 401）：

```bash
DC_DIGEST_DISCORD_TOKEN=invalid uv run dc-digest fetch --from=-10m --to=now
```

### 外部链接处理

工具绝不访问外部链接，也不替消息里的 URL 发请求（见 ADR-0006）。下载只允许 `[media] allowed_hosts` 里的域名（默认 Discord 自家 CDN）：

| 输入 | 处理 |
| --- | --- |
| Discord CDN 上的图片附件 / embed 图 | 下载到临时缓存，作为附图交给 spi |
| 非白名单域名的图片（含站外 embed） | 记 `kind=external`，永不下载，URL 在 transcript 行尾标为 `[外部链接: …]` |
| 正文里的普通链接（X、短链、文档地址） | 原样保留在 transcript，只作文本引用 |

提示词同时要求模型：禁止访问、请求、抓取、展开短链接或猜测链接内容，链接可能含恶意代码；需要说明时写「未访问，仅记录链接」并给出域名。模型侧另有 `--no-tools` 兜底，本身没有联网能力。

## 风险与注意

- 用户 token 属于自动化账号行为，违反 Discord ToS，存在封号与限流风险。建议挂在专用小号上，并保持默认的保守节奏。
- **只读**：代码层只有 `GET`（`DiscordClient._request` 拒绝其它方法，另有源码级测试把关），不会在群组内发言、改昵称或做任何写入。见 ADR-0007。
- token 不进 `config.toml`、不进日志；`doctor` 只报告是否存在，不打印内容。
- 简报头部「未抓取完整」提示表示该窗口仍有频道没抓完，先补跑 `fetch` 再信结论。
- 分块摘要按字符预算切段，话题跨越切分点时，合并后的简报可能丢连接上下文；日常一天的记录量通常用不到分块。
- 图片会以本地文件形式发送给 `[summarize]` 指定的模型通道，等于把截图内容交给出题方模型；含敏感信息的频道可把 `[media] enabled` 设为 `false`，或用 `--no-media` 单次跳过。
- 图片目录 `./data/media` 会随时间增长，需要定期清理或纳入备份策略；库里只记录元数据与本地路径，删文件不会重下，除非把对应行的 `status` 改回 `pending`。

## 排查

| 现象 | 处理 |
| --- | --- |
| `token 无效或已过期（401）` | 重新取 token 并更新 Keychain 条目 |
| `无权访问该资源（403）` | 该账号看不到这个频道，或缺「读取历史消息」权限 |
| `接口限流 ... retry_after=NNN` | 正常进入退避；调小 `max_requests_per_run` 或加大间隔 |
| `分页未前进` | 接口返回异常分页，停止以免重复抓取；确认 token 与频道 ID 后重试 |
| `获取 tenant_access_token 失败` / `发送飞书消息失败` | 沿用错误里的 `code` / `msg` 排查应用凭证与机器人权限 |
| `spi 没有输出` / 退出码非 0 | 手动执行 `[summarize] args` 里的命令，确认模型可用 |
| `无权下载附件（403）` | CDN 鉴权失败，检查 token 是否过期；这类错误会立刻中断下载 |
| `附件已失效（404）` | 图片被删除或签名 URL 过期，该条记 `failed` 并继续 |
| `附件超过 N 字节上限` | 调大 `media.max_bytes_per_file`，或保持跳过 |
| 简报里图片信息写成「图片无法判读」 | 图片可能损坏或过小，先手动 `spi -p @<图片> "描述这张图"` 验证模型能否读 |

## 结构

```
src/dc_digest/
  cli.py          子命令与抓取主循环
  config.py       配置与凭证（Keychain / 环境变量）
  discord_api.py  REST 客户端与 snowflake 换算
  pace.py         拟人节奏、预算与限流退避
  store.py        SQLite 消息、附件、游标、运行记录
  media.py        图片附件解析与拟人化下载
  render.py       transcript 渲染与分块
  summarize.py    spi 调用与 map-reduce
  feishu.py       飞书卡片推送与长文拆分
  alerts.py       失败与降级提醒内容
  schedule.py     launchd 任务生成与安装
config.example.toml  配置模板（config.toml 由它复制而来，已被 .gitignore 忽略）
data/             库、日志、预生成的 launchd plist（不进版本控制）
digests/          生成的简报（不进版本控制）
prompts/          digest / chunk / reduce 三个模板
docs/adr/         架构决策记录
tests/            单元测试（uv run pytest）
```
