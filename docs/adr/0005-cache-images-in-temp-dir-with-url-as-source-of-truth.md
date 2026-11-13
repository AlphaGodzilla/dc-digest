---
status: "accepted"
date: 2026-11-13
decision-makers: 用户
consulted: 无
informed: 无
---

# 图片只缓存到临时目录，Discord 链接才是真相源

## Context and Problem Statement

ADR-0004 决定把聊天图片落盘后作为 `@附件` 交给 spi，但当时把文件放进项目内的 `./data/media` 并当作长期资产。实际使用中图片属于一次性素材：一次简报用完即失效，长期留存既占空间也拉长敏感截图的留存窗口；而库里已有 Discord CDN 链接，随时可以重下。因此需要重新确定图片的存放位置与持久化边界（本条只修订 ADR-0004 的存储部分，其「本地文件喂给 spi」的结论不变）。

## Decision Drivers

* 图片是消耗品，不值得进备份与版本管理
* 敏感截图留存窗口越短越好
* 库要能独立迁移或重建，不依赖本地文件树
* 临时文件被系统清理后必须能自动恢复，而不是报错或静默缺图

## Considered Options

* 项目内 `./data/media` 持久化（ADR-0004 原方案）
* 临时目录缓存 + 库中只存 Discord 链接（选定）
* 不落盘，每次摘要前现下现删

## Decision Outcome

选择「临时目录缓存 + 库中只存链接」，因为它在不牺牲多模态摘要质量的前提下，把图片降级为可随时重建的缓存。

具体形态：缓存根目录默认 `$TMPDIR/dc-digest-media`（`[media] media_dir` 可覆盖）；`message_media` 表只以 `url` 与元数据为准，`local_path`/`sha256` 仅作缓存线索；摘要前若发现文件已不存在，把对应行打回 `pending` 并按节奏重下（`ensure_on_digest = true` 时在 `digest` 内自动完成，`--no-media` 可跳过）；`media --clean` 主动清空窗口内缓存并把记录打回待下载。

### Consequences

* Good, because 项目目录只留库与简报，图片不进备份与版本管理
* Good, because 敏感截图在系统清理或 `--clean` 后不留存
* Good, because 库与文件解耦，换机器只要带上 `.db` 就能重下图片
* Bad, because 缓存被清理后重算同一窗口会再次消耗下载预算与 CDN 请求
* Bad, because 带图简报依赖网络可达，离线时只能出纯文本版本
* Bad, because `local_path` 与真实文件之间会出现短时漂移，只能靠运行时校验修正

### Confirmation

`uv run dc-digest media --dry-run` 列出待下载项；手工删除缓存文件后重跑 `digest` 应看到「N 张图片的本地缓存已不在，标记为待重新下载」并自动补齐；`tests/test_media.py::test_reset_missing_cache_marks_pending` 与 `tests/test_store.py::test_reset_media_marks_pending_and_returns_paths` 覆盖该状态流转。

## Pros and Cons of the Options

### 项目内持久化 `./data/media`

* Good, because 重算零下载、可离线出带图简报
* Bad, because 磁盘长期增长，敏感截图长期留存，且容易误入备份与版本管理

### 不落盘，现下现删

* Good, because 留存窗口最短，项目与临时目录都不留痕
* Bad, because 分块摘要会重复下载同一张图，且失败后无法重试续传

## More Information

实现见 `src/dc_digest/media.py`（`reset_missing_cache`）与 `src/dc_digest/store.py`（`reset_media`、`reset_media_window`）。本条修订 ADR-0004 中「落地到 `./data/media`」的描述。
