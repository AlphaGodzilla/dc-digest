---
status: "accepted"
date: 2026-11-13
decision-makers: 用户
consulted: 无
informed: 无
---

# 摘要后端用 spi 子进程

## Context and Problem Statement

简报由聊天记录生成，需要一个大模型摘要步骤。摘要步骤要嵌进命令行管线（抓取 → 摘要 → 落盘 → 推送），需要用户已经可用的模型通道，同时避免在本工具里再维护一套凭证、重试与模型选择逻辑。

## Decision Drivers

* 用户已有配置好的多 provider 模型通道（pi / spi）与 OpenAI 兼容端点
* 不想在工具里落第二份 API key
* 摘要步骤需要可替换模型、可在失败时立即中断而不是降级
* 管线形态要求「标准输入喂 prompt，标准输出拿 Markdown」

## Considered Options

* 调用 `spi -p` 子进程（选定）
* 工具内直接调用模型 HTTP API，自管 key 与重试
* 本地常驻 ollama 服务

## Decision Outcome

选择「调用 `spi -p` 子进程」，因为它是唯一同时满足「零新增凭证」「模型可配置」「天然适配 stdin/stdout 管线」的方案。

调用形态：`spi -p --no-tools --no-session --mode text --model opencode-go/deepseek-v4.1-flash`，stdin 写 prompt，stdout 取 Markdown，非零退出码或空输出即抛错终止。

### Consequences

* Good, because 不再引入第二套 API key，换模型只改配置里的 `args`
* Good, because `--no-tools --no-session` 让输出干净、不产生会话文件
* Good, because 摘要失败与抓取失败一样是硬失败，不会产出半成品简报
* Bad, because 每次调用多约 3 秒进程启动开销，分块摘要时开销累积
* Bad, because 产出质量依赖外部 CLI 的 stdout 纪律，模型若输出寒暄也只能靠 prompt 约束
* Bad, because 摘要子进程的行为不受本工具超时以外的手段控制（无法注入重试策略）

### Confirmation

`uv run dc-digest digest --dry-run` 打印首块片段但不调用模型；正常 `digest` 的日志会打印实际执行的命令行与输入字符数，可直接复制到终端复现。

## Pros and Cons of the Options

### 工具内直接调用模型 HTTP API

* Good, because 延迟最低，重试与超时完全自控
* Bad, because 需要在工具里管理 key、模型名、供应商差异与重试策略，与用户既有通道重复

### 本地常驻 ollama 服务

* Good, because 数据不出本机，成本固定
* Bad, because 需要一个常驻服务与显存前提，摘要质量与用户日常使用的模型不一致

## More Information

prompt 模板在 `prompts/digest.md`、`prompts/chunk.md`、`prompts/reduce.md`；调用逻辑见 `src/dc_digest/summarize.py`。
