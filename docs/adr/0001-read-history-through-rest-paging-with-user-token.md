---
status: "accepted"
date: 2026-11-13
decision-makers: 用户
consulted: 无
informed: 无
---

# 用 REST 分页加用户 token 读取 Discord 历史

## Context and Problem Statement

需要一个每日本地运行的命令行工具，读取指定 Discord 服务器频道的历史消息并生成简报。目标频道是服务器里的文字频道，历史深度受「每日简报」限制在一天以内。读取方式决定凭证形态、被封风险、请求节奏的可控性与依赖面，必须在实现前定下来。

## Decision Drivers

* 只读历史，不需要实时事件流
* 请求节奏必须能像真人客户端一样被完全控制（用户明确要求避免批量猛拉）
* 需要覆盖私聊群的可能性，且不想依赖服务器管理权限
* 单机运行，依赖越少越好

## Considered Options

* 官方机器人 token + Gateway 常驻连接
* 官方机器人 token + REST 轮询
* 用户 token + REST 分页（选定）
* 现成工具 DiscordChatExporter 导出，脚本只做摘要

## Decision Outcome

选择「用户 token + REST 分页」，因为它在满足「节奏完全自控 + 不依赖服务器管理权限 + 少依赖」的同时，把读取收敛成一次可续传的倒序翻页，不需要事件流、不需要常驻进程。

具体形态：`GET /api/v10/channels/{channel_id}/messages?before=<id>&limit=50`，从窗口末端向前翻页，用 snowflake 边界裁剪窗口；页间随机间隔、每若干页长停顿、跨频道冷却、单轮请求/消息/墙钟三重预算，429 时短期等待、长期退避结束本轮。

### Consequences

* Good, because 不需要 Gateway、不需要 privileged intent 审批、也不需要把机器人拉进服务器
* Good, because 节奏与预算完全由本地配置决定，可以按账号行为调保守程度
* Good, because 依赖只有 `httpx`，不引入 Discord SDK 的网关与事件模型
* Bad, because 用户 token 属于自动化账号行为，违反 Discord ToS，存在封号与限流风险，需要专用账号
* Bad, because 只能看到该账号可见的频道，权限变化会直接导致 403
* Bad, because 编辑与删除消息无法通过倒序翻页增量感知，只能靠重抓窗口修正

### Confirmation

`uv run dc-digest state` 能显示每个频道的窗口与游标；`--verbose` 日志给出每次请求的等待明细（label/秒数），可据此核对节奏是否符合配置。

## Pros and Cons of the Options

### 官方机器人 token + Gateway 常驻连接

* Good, because 合规，无封号风险
* Bad, because 需要服务器管理权限与 Message Content privileged intent，且必须常驻进程维持事件流；对「一天读一次」的负载明显过重

### 官方机器人 token + REST 轮询

* Good, because 合规且无需常驻
* Bad, because 仍需把机器人拉进服务器并申请 intent；用户当前不掌握服务器管理权限

### 用户 token + REST 分页

* Good, because 不需要任何服务器侧操作，覆盖面最大
* Bad, because 违反 ToS，凭证即账号，泄露或滥用代价高

### DiscordChatExporter 导出

* Good, because 现成实现已处理限流与格式
* Bad, because 多一层外部进程与中间文件，节奏参数无法按本工具语义控制，时间窗与入库逻辑仍要另写

## More Information

实现细节见 `src/dc_digest/discord_api.py` 与 `src/dc_digest/pace.py`。把 `[discord] auth` 改成 `bot` 即可切到官方机器人 token，读取路径不变。
