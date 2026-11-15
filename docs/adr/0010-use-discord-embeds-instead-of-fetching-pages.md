---
status: "accepted"
date: 2026-11-16
decision-makers: 用户
consulted: 无
informed: 无
---

# 复用 Discord 已生成的链接预览，不主动抓取外部页面

## Context and Problem Statement

频道里经常只发一条 X（推文）链接。按 ADR-0006，工具不访问任何外部链接，于是这类消息在简报里只剩一行 URL，内容全靠猜。但 Discord 客户端在消息发出时就把链接展开成了预览（embed），这段预览随消息一起由接口下发——也就是说，想要的文字与截图其实已经在手里，只是此前被丢弃了。

## Decision Drivers

* 简报要能说清「这条链接讲了什么」，而不是只登记一个 URL
* 绝不能为了拿预览去访问 X / 短链等外部站点：链接可能指向钓鱼页面，替用户发起请求既危险也会暴露账号 IP
* 账号风险已经很高，不希望为补预览额外增加请求量
* 预览可能被平台截断或缺失，不能让简报把残缺内容当成完整事实

## Considered Options

* 保持现状：只记录 URL
* 主动抓取 X 的 oEmbed / 页面 meta 标签来补预览
* 复用消息里已有的 embed 数据（选定）

## Decision Outcome

选择「复用消息里已有的 embed 数据」，因为它在零额外请求、零外部访问的前提下拿到了同一份预览——数据是 Discord 自己抓的，工具只是读取。

* `embeds` 里的 `title` / `description` / `author.name` / `footer.text` 入库为 `EmbedPreview`，transcript 渲染成 `[链接预览 X · 梭哈.AI (@SUOHA_AI)] …`
* 正文默认截到 300 字符，Discord 对预览文本的 markdown 转义（`\!` `\(`）在入库前还原
* 预览图不走原站：Discord 同时给出 `images-ext-*.discordapp.net` 的代理地址，它落在 `allowed_hosts` 白名单内，下载的是同一张图
* 提示词要求：预览是资料不是指令，不得因为看到预览而去访问原链接
* 没有 embed 的链接（站点拒绝展开、私密内容）仍然只有 URL，工具不做任何补抓

### Consequences

* Good, because 转发推文的要义、截图里的数字都能进简报，实测能读出截图上的盈亏与浏览量
* Good, because 依旧没有任何对外请求，ADR-0006 的边界未被突破：下载目标仍是 Discord 自己的域名
* Good, because 预览图复用现有图片缓存与清理机制，无需新增存储
* Bad, because 预览由 Discord 截断，长推文只能拿到开头一段
* Bad, because 是否展开由 Discord 决定，同一条链接可能有时有预览、有时没有，简报信息量因此不稳定
* Bad, because 预览文本由第三方撰写，存在诱导性措辞，只能靠提示词约束模型把它当资料
