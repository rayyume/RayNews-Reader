# 未发布变更 / Unreleased

## 抓取范围与日报

- 旧行为：每轮只保留北京时间当天的消息，中断期间产生的跨日积压会被丢弃。
- 新行为：仅首次运行（没有增量游标）限制为当天；后续运行按消息 ID 增量抓取，保留跨日消息，以配合按入库时间生成日报。
- 抓取中断数天后，恢复时可能批量导入积压，增加当轮抓取量和后续 AI 处理量。每轮仍最多抓取 200 页；不保证超出该范围的积压全部补齐。
- 补抓文章按实际入库时间进入前一日 21:00 至当日 21:00（北京时间）的日报窗口，而非回填原发布日期的日报。
- “待分类”筛选包含显式标记为待分类的来源，以及尚未建立分类记录的来源；它们不再默认归入“其他信息”。

## Fetch scope and daily digests

- Previously, every fetch kept only messages published that day in Beijing time and discarded older backlog after interruptions.
- Now, only the first run without a cursor is restricted to today. Subsequent runs fetch newer message IDs across dates to support digests based on ingestion time.
- Recovery after a multi-day interruption may import a backlog and increase fetching and AI processing. The existing 200-page limit still applies per run; complete recovery beyond that limit is not guaranteed.
- Recovered articles enter the digest window from the previous 21:00 to the current 21:00 Beijing time according to their ingestion time, not their original publication date.
- The Uncategorized filter includes both explicitly uncategorized sources and sources without a category record; these no longer default to Info.
