---
status: "accepted"
date: 2026-11-13
decision-makers: 用户
consulted: 无
informed: 无
---

# 外部链接只作文本，下载仅限 Discord CDN 白名单

## Context and Problem Statement

群聊消息里会出现 X、短链、自建域名等外部链接，也可能出现指向站外的 embed 图片。工具此前只关心 Discord 附件，但 embed 的图片地址与附件 URL 都由消息内容决定，任何人都可以在消息里放入一个指向自有服务器的 URL，诱使工具替其发起请求。用户明确要求：外部链接禁止访问，只作为文本摘要；并要提示模型外部链接可能含恶意代码。

## Decision Drivers

* 工具不替第三方发起请求，避免被当作探测或打点通道
* 外部链接内容不可信，抓取即引入恶意载荷与钓鱼页面风险
* 摘要模型不需要外壳工具，也不应具备联网能力
* 落盘的只能是可核验来源的资源（Discord 自家 CDN）

## Considered Options

* 只在提示词里约束「不要访问链接」
* 提示词约束 + 下载宿主白名单 + 外部链接以文本标记（选定）
* 引入链接安全扫描或沙箱浏览器再决定是否抓取

## Decision Outcome

选择「提示词 + 宿主白名单 + 文本标记」三层约束，因为它把不可信输入挡在代码层，而不是依赖模型自觉。

具体形态：`collect_media` 按 `[media] allowed_hosts`（默认 `discordapp.com`、`discordapp.net`、`discord.com`）判定附件与 embed 图，白名单内图片记为可下载的 `image`，其余记 `kind="external"`；下载器只处理 `image`，`external` 永不进入下载队列；`external` URL 由 `store.window_external_links` 取出，在 transcript 行尾渲染为 `[外部链接: …]`，并在三个提示词里写明「禁止访问、禁止展开短链、禁止猜测内容，需要说明时写『未访问，仅记录链接』」。

### Consequences

* Good, because 工具不会对外部域名发起任何请求，不存在被中间人打点或探测内网的可能
* Good, because 链接仍以原文进入简报，人工判断时信息不丢失
* Good, because 模型侧同样无联网能力（`--no-tools`），两层约束互为兜底
* Bad, because 简报无法给出链接正文摘要，只保留 URL 与域名
* Bad, because embed 里的站外图片既不下也不显示内容，只能看到 URL
* Bad, because 白名单是域名后缀匹配，需要人工维护，新增可信图床要改配置

### Confirmation

`tests/test_media.py` 覆盖 CDN 与外部域名的分类、以及「external 永不进入下载队列」；`tests/test_render.py` 覆盖 `[外部链接: …]` 渲染与去重截断；`uv run dc-digest media --dry-run` 只列出白名单内的待下载图片。

## Pros and Cons of the Options

### 只在提示词里约束

* Good, because 零代码改动
* Bad, because 代码路径仍然会下载任意 URL，模型约束对工具自身无效

### 链接安全扫描或沙箱浏览器

* Good, because 能在可控环境里取到链接内容
* Bad, because 引入浏览器与沙箱依赖，攻击面转移到运行时，与本工具「只读、最小面」的定位冲突

## More Information

实现见 `src/dc_digest/media.py`（`is_trusted_media_url`、`collect_media`）、`src/dc_digest/render.py`（`_link_suffix`）与 `src/dc_digest/store.py`（`window_external_links`）。白名单可在 `config.toml` 的 `[media] allowed_hosts` 调整。
