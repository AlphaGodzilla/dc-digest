---
status: "accepted"
date: 2026-11-13
decision-makers: 用户
consulted: 无
informed: 无
---

# 抓取失败必须通过飞书提醒，不静默失败

## Context and Problem Statement

工具按日运行、无人值守地挂在后台时，封号、token 失效、代理中断都会让当天没有简报。此前这些情况只写 stderr 日志、退出码 2，用户不看日志就完全不知道当天缺数据。用户要求：因封号、凭证或网络问题导致无法获取聊天信息时，必须通过飞书告知。

## Decision Drivers

* 无人值守下「没有简报」与「简报为空」在用户视角无法区分
* 提醒要能说明根因与下一步动作，而不是只报一句失败
* 提醒通道自身可能不可用，不能因此掩盖原始故障
* 提醒必须可控，调试时不能被自己的提醒打扰

## Considered Options

* 只写日志与退出码，由外部调度器负责告警
* 硬失败与降级都走飞书提醒 + 退出码（选定）
* 引入独立告警通道（邮件/短信/其他 IM）

## Decision Outcome

选择「硬失败与降级都走飞书提醒 + 退出码」，因为飞书通道已经存在（推送简报用的同一套应用凭证），无需新建告警基建。

具体形态：`alerts.py` 把异常翻译成含命令、时间、窗口、错误类型、原始错误与建议的短消息；`cli._notify_failure` 在捕获 `DiscordError`/`FeishuError`/`SummarizeError`/`TimeWindowError`/`ConfigError` 后调用；`cli._notify_degraded` 在抓取被限流或预算耗尽（`stop_reason` 非空）时调用，标题为「抓取不完整」；两者都受 `[alerts] enabled` 与 `--no-alert` 控制；提醒自身失败只记日志。

### Consequences

* Good, because 缺数据这件事从「静默」变成「有明确原因与建议的通知」
* Good, because 401/403/404/网络异常各有针对性建议，用户不必翻代码判断
* Good, because 降级（限流、预算耗尽）与硬失败区分开，避免把可续传的情况误报成故障
* Bad, because 提醒与推送共用飞书通道，飞书侧故障时两类消息一起失效
* Bad, because 配置读不出来时无从得知提醒配置，这种情况仍只有日志
* Bad, because 定时任务持续失败会每次运行都发一条提醒，需要用户处理后自行关闭

### Confirmation

`tests/test_alerts.py` 覆盖建议映射、消息排版、`--no-alert` 与 `alerts.enabled=false` 的抑制；手工演练用无效 token 触发 401，日志应出现 `已发送飞书消息 1/1` 与 `已通过飞书发出提醒（1 条）`。

## Pros and Cons of the Options

### 只写日志与退出码

* Good, because 工具不关心告警渠道，职责最单一
* Bad, because 依赖用户自建调度器告警，当前是手动或简单定时运行，等于没有告警

### 独立告警通道

* Good, because 与简报通道解耦，互不影响
* Bad, because 需要再申请一套凭证与维护成本，收益不足以抵消

## More Information

实现见 `src/dc_digest/alerts.py`、`src/dc_digest/cli.py`（`_send_alert`、`_notify_failure`、`_notify_degraded`）；配置见 `config.toml` 的 `[alerts]`。
