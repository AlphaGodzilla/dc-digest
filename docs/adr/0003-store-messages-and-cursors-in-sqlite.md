---
status: "accepted"
date: 2026-11-13
decision-makers: 用户
consulted: 无
informed: 无
---

# 消息与抓取游标存于 SQLite

## Context and Problem Statement

抓取受单轮预算限制会中途结束，需要跨运行续传；同一时间窗的简报需要可反复重算（换模型、改 prompt、补跑），而重算不应再触碰 Discord 接口。因此必须决定消息与游标的落地形态。

## Decision Drivers

* 预算中断后续传，不能重复抓取已入库消息
* 简报重算不得消耗网络配额与账号风险
* 单机、单用户、数据量小（每天数千条量级）
* 不引入额外依赖或外部服务

## Considered Options

* SQLite 单文件（选定）
* 按天导出 JSON / JSONL 文件
* 不落地，抓到就摘要

## Decision Outcome

选择「SQLite 单文件」，因为消息主键天然支持幂等 upsert，游标与运行记录可以和消息同库事务化保存，且标准库自带无需依赖。

表结构：`messages`（主键为消息 id）、`channel_state`（每频道的窗口与游标）、`runs`（窗口、状态、请求与消息计数）。

### Consequences

* Good, because 重复抓取不会产生重复行，续传点就是一次游标写入
* Good, because `digest` 只读库，同窗重算零网络开销，可以自由比较不同 prompt 与模型
* Good, because 运行记录保留了每轮窗口与请求量，便于核对节奏与补跑范围
* Bad, because 多了一个需要备份与清理的库文件，以及 WAL 附属文件
* Bad, because 改变时间窗可能丢弃游标并重新抓取，同一批消息会被重复请求

### Confirmation

`uv run dc-digest state` 显示各频道的窗口、游标与完整状态；`uv run pytest` 中的 `tests/test_store.py`、`tests/test_fetch.py` 覆盖幂等 upsert、续传与预算中断。

## Pros and Cons of the Options

### 按天导出 JSON / JSONL 文件

* Good, because 文件可直接查看与版本管理
* Bad, because 幂等 upsert、窗口查询与游标要自己用文件读写实现，重复抓取与跨文件去重容易出错

### 不落地，抓到就摘要

* Good, because 实现最短
* Bad, because 预算中断后无法增量续传，重算必须重抓，直接放大账号风险

## More Information

实现见 `src/dc_digest/store.py`。库文件位置由 `config.toml` 的 `database` 决定，相对路径按配置文件所在目录解析。
