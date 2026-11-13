---
status: "accepted"
date: 2026-11-13
decision-makers: 用户
consulted: 无
informed: 无
---

# 图片以本地附件交给 spi 子进程判读

## Context and Problem Statement

群聊里大量信息以截图、报错图、图表出现，只把文件名写进 transcript 会让简报丢掉最关键的内容。摘要子进程被限制为 `--no-tools`（无工具、无网络），因此需要先决定图片如何到达模型手里，以及在哪一阶段下载。

## Decision Drivers

* 模型必须真正看到图片内容，而不是只看到「有人发了一张图」
* 摘要子进程不应获得 Discord 凭证，也不应依赖 CDN 鉴权与网络抖动
* 抓取阶段已有严格节奏与预算约束，下载不能绕过它
* 同一窗口的简报要能反复重算，重算不应重复下载

## Considered Options

* 抓取元数据 + 独立下载阶段 + 本地文件以 `@路径` 传给 spi（选定）
* 让模型自己访问 CDN URL
* 把图片 base64 内联进 prompt 文本
* 不做图片，只在 transcript 里保留文件名

## Decision Outcome

选择「抓取元数据 + 独立下载阶段 + 本地 `@路径` 多模态输入」，因为只有它在不交出凭证、不放开工具的前提下，让模型拿到真实像素内容，并且天然复用既有的预算与续传机制。

具体形态：`fetch` 把附件与 embed 内嵌图记为 `message_media` 行（`status=pending`）；`media` 阶段按 1–3 秒随机间隔下载到 `./data/media/<频道ID>/<消息ID>-<序号>-<文件名>`，单文件超 8 MB 跳过、单轮 40 张或 40 MB 封顶；`digest` 阶段按时间顺序给已下载图片编号，消息行尾标 `[图片 N]`，并以 `spi ... @/abs/path.png` 形式追加在命令行末尾。

### Consequences

* Good, because 摘要子进程不需要 Discord token，也不需要工具或网络
* Good, because 图片落盘后可反复参与重算，换模型、改 prompt 都不再下载
* Good, because 下载与抓取共用控制点（间隔抖动、单轮预算、401/403 立即中断、404 记失败继续）
* Bad, because 每张图都要占提示词预算，单次 8 张、单份 30 张的上限会丢弃部分图片
* Bad, because 多了一个需要备份与清理的磁盘目录，以及「图片已删除但库里仍指向它」的状态漂移
* Bad, because 图片内容会送进模型通道，敏感频道的截图外泄风险需要用户自己判断

### Confirmation

`uv run dc-digest media --dry-run` 列出待下载图片；`digest` 日志打印「附 N 张图」；`tests/test_media.py` 覆盖下载、超限、404、429、预算中断，`tests/test_summarize.py` 覆盖 `@路径` 拼接与单次上限裁剪。

## Pros and Cons of the Options

### 让模型自己访问 CDN URL

* Good, because 无需本地下载与磁盘管理
* Bad, because 需要给子进程开工具与网络，并把 Discord token 或签名 URL 交给模型，安全边界被打穿

### base64 内联进 prompt

* Good, because 一次调用即可携带图片，无中间文件
* Bad, because base64 让 prompt 体积膨胀数倍且模型未必解析，调试困难

### 不做图片

* Good, because 实现最简单
* Bad, because 截图类讨论会完全失真，恰恰是简报最需要的内容

## More Information

实现见 `src/dc_digest/media.py`、`src/dc_digest/summarize.py`（`build_argv`）与 `src/dc_digest/store.py`（`message_media` 表）。提示词要求读出图中报错与结论、看不清时写「图片无法判读」，并禁止把本地路径写进简报。

图片的存放位置与持久化边界已由 [ADR-0005](./0005-cache-images-in-temp-dir-with-url-as-source-of-truth.md) 修订：文件只缓存到临时目录，库中以 Discord 链接为准。
