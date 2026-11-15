# 架构决策记录索引

本目录存放 dc-digest 的架构决策记录（ADR）。编号、标题、状态与日期只在本表维护，其它文档链接到具体文件。

| 编号 | 标题 | 状态 | 日期 |
| --- | --- | --- | --- |
| [0001](./0001-read-history-through-rest-paging-with-user-token.md) | 用 REST 分页加用户 token 读取 Discord 历史 | accepted | 2026-11-13 |
| [0002](./0002-use-spi-subprocess-as-summarizer.md) | 摘要后端用 spi 子进程 | accepted | 2026-11-13 |
| [0003](./0003-store-messages-and-cursors-in-sqlite.md) | 消息与抓取游标存于 SQLite | accepted | 2026-11-13 |
| [0004](./0004-feed-images-to-spi-as-local-attachments.md) | 图片以本地附件交给 spi 子进程判读 | accepted | 2026-11-13 |
| [0005](./0005-cache-images-in-temp-dir-with-url-as-source-of-truth.md) | 图片只缓存到临时目录，Discord 链接才是真相源 | accepted | 2026-11-13 |
| [0006](./0006-never-fetch-external-links.md) | 外部链接只作文本，下载仅限 Discord CDN 白名单 | accepted | 2026-11-13 |
| [0007](./0007-read-only-no-posting.md) | 只读不发言，通信维持 REST 轮询而非 Gateway 常驻 | accepted | 2026-11-13 |
| [0008](./0008-notify-failures-via-feishu.md) | 抓取失败必须通过飞书提醒，不静默失败 | accepted | 2026-11-13 |
| [0009](./0009-track-brief-progress-separately-from-fetch.md) | 简报进度与抓取进度分开记账 | accepted | 2026-11-13 |
| [0010](./0010-use-discord-embeds-instead-of-fetching-pages.md) | 复用 Discord 已生成的链接预览，不抓外部页面 | accepted | 2026-11-16 |
