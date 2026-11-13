---
status: "accepted"
date: 2026-11-13
decision-makers: 用户
consulted: 无
informed: 无
---

# 只读不发言，通信维持 REST 轮询而非 Gateway 常驻

## Context and Problem Statement

用户要求本工具**禁止在群组内发言**，并提供了参考项目 `tensakulabs/discord-mcp`（用户 token 直连 Discord Gateway 的常驻自建机器人）要求评估其通信方式是否值得借鉴。需要回答两件事：发言能力如何从代码层面排除，以及是否需要改成 Gateway 常驻。

## Decision Drivers

* 只读是硬约束，不能依赖「不调用那个函数」的自觉
* 一日一次读历史，没有实时性需求
* Gateway 常驻会带来在线状态、心跳、IDENTIFY 指纹等额外暴露面
* 库长期运行会无限增长，需要留存策略

## Considered Options

* 维持 REST 轮询 + 代码层只读守卫（选定）
* 改为参考项目的 Gateway WebSocket 常驻 + SQLite 落库
* 两者并存：Gateway 收实时、REST 补历史

## Decision Outcome

选择「维持 REST 轮询 + 代码层只读守卫」，因为参考项目的通信路径对本案是负优化，而它真正值得吸收的只有留存清理。

只读守卫：`DiscordClient` 所有请求都收敛到 `_request(method, …)`，`READ_ONLY_METHODS` 只含 `GET`，其余方法直接抛错；`download` 同样固定 `GET`。另有一条源码级测试断言 `discord_api.py` 中不出现 `POST`/`PUT`/`PATCH`/`DELETE`，防止后续改动悄悄引入写入路径。

对参考项目的评估结论：它用 `wss://gateway.discord.gg/?v=10&encoding=json` + op2 IDENTIFY（intents `GUILDS|GUILD_MESSAGES|MESSAGE_CONTENT`）+ 自定义 `properties` 指纹，只处理 `MESSAGE_CREATE`，未处理 `MESSAGE_UPDATE`/`MESSAGE_DELETE`，因此连「实时追踪编辑」这个 Gateway 唯一优势都没拿到；其 429 处理为 `min(retry_after, 8s)` 重试 4 次，比本工具的预算式退避激进；它还提供 `send_message` 工具，直接违反本次只读要求。可吸收的部分只有「按留存期清理库」— 已落为 `[store] retention_days` 与 `dc-digest prune`。

### Consequences

* Good, because 工具在代码层不可能发言，而不是靠约定
* Good, because 不引入 WebSocket 常驻、心跳与 IDENTIFY 指纹，暴露面保持最小
* Good, because 一次运行的行为可预测：读若干页、下若干图、跑一次摘要
* Bad, because 编辑与删除的消息无法被增量感知，只能重抓窗口修正
* Bad, because 留存清理需要外部触发（手动或定时），工具内不自带调度

### Confirmation

`tests/test_discord_api.py` 覆盖只读守卫（非 GET 抛错）、源码无写方法、状态码映射、限流与网络异常；`uv run dc-digest prune --dry-run` 打印留存天数与将删除的行数；`uv run pytest` 全量通过。

## Pros and Cons of the Options

### Gateway WebSocket 常驻

* Good, because 能实时拿到消息事件，理论上可追踪编辑与删除
* Bad, because 需要常驻进程与心跳、暴露在线状态，IDENTIFY 指纹本身就是识别特征；参考实现也没有处理更新事件

### Gateway + REST 并存

* Good, because 实时与历史都能覆盖
* Bad, because 维护两套读取路径与两套游标，收益对本工具为零

## More Information

实现见 `src/dc_digest/discord_api.py`（`READ_ONLY_METHODS`、`_request`）、`src/dc_digest/store.py`（`prune`）与 `src/dc_digest/cli.py`（`cmd_prune`）。参考项目：`https://github.com/tensakulabs/discord-mcp`。
