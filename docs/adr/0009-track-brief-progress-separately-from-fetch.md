---
status: "accepted"
date: 2026-11-13
decision-makers: 用户
consulted: 无
informed: 无
---

# 简报进度与抓取进度分开记账

## Context and Problem Statement

`runs` 表原本在抓取结束时就写下最终状态，而简报与推送在之后才执行。于是 `spi` 不可用、模型报错或飞书拒收时，进程以失败退出、飞书收到告警，`runs` 里却记着 `ok`；更要紧的是下次运行的窗口起点取自上一条 `ok` 的右边界，于是这一段的简报永远不会被生成——消息在库里，但没有任何机制再去 brief 它。

一条记录同时承担「抓取到哪了」和「这一轮整体成功了吗」两个含义，是矛盾的根源。

## Decision Drivers

* 摘要或推送失败后，缺的那段简报必须能被自动补上，不能依赖人工发现
* 补简报不能重新抓取：消息已经在库里，重复请求既浪费配额也增加账号风险
* 飞书长时间故障时，抓取窗口必须继续推进，不能让「重抓旧段、预算耗尽、又失败」形成活锁
* 状态字段不能与事实矛盾：日志、退出码、库三者要能对上

## Considered Options

* 保持现状：抓取成功即 `ok`，简报缺口靠人工补跑
* 把 `finish_run` 挪到整轮结束：任何阶段失败都记为 `error`，窗口不推进
* 拆成两本台账：`runs` 只记抓取，新增 `briefs` 记简报进度（选定）

## Decision Outcome

选择「拆成两本台账」，因为只有它同时满足上面四条驱动因素：抓取与简报各自有一条独立的推进线，互不牵连。

* `runs.status` 只描述抓取：`ok` / `incomplete` / `error`，摘要与推送失败追加到 `note`（`简报未完成：…`），不再冒充 `ok`
* 新增 `briefs` 表记录每次落盘的简报（窗口、路径、是否已推送）；`path` 为空表示没写出文件，不算完成
* `_resolve_window(kind="brief")` 取 `MAX(window_to_ms) WHERE path IS NOT NULL` 作为简报窗口起点，抓取窗口仍取上次 `ok` / `incomplete` 的右边界
* 落盘即推进台账：推送失败不回退，避免同一段消息反复生成并重复推送

### Consequences

* Good, because 摘要失败后下一次运行会自动把缺的时段一起 brief，零网络、零重抓
* Good, because 抓取窗口照常推进，飞书故障不会拖慢追新消息
* Good, because `state` 能同时显示「上次抓取」与「上次简报」两个时间点，缺口一眼可见
* Good, because `runs.note` 保留了失败原因，日志、退出码与库不再互相矛盾
* Bad, because 多了一张表与一条查询，`digest` 与 `run` 需要各自解析窗口
* Bad, because 简报与抓取窗口可能不同（简报窗口更早），日志里要显式提示，否则读者会以为抓取出错
