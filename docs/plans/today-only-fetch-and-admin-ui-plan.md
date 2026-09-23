# 刷新范围收紧(仅限今天)+ 管理端清理日期选择器 + 资源占用移动端适配 修改计划

> 适用分支:dev · 状态:历史计划 · 2026-07
>
> 2026-09-23 更新：第 1 节“所有抓取仅限当天”的方案已被按入库时间生成日报的设计取代。
> 当前仅首次运行（无增量游标）限制为北京时间当天；后续增量抓取保留跨日消息，
> 中断恢复后可能批量补抓积压，每轮仍受 200 页上限约束。补抓文章按实际入库时间进入日报窗口。
> 下文保留原始方案作为历史记录，不代表当前抓取行为；第 2、3 节不受此语义变更影响。
> 详见[未发布变更说明](../../release_note_unreleased.md)。

三项相互独立,建议按 3 个独立提交交付,可分别回滚。

---

## 1. 刷新范围固定为"增量 + 仅限今天(北京时间)"

### 现状与问题

`fetcher.py:931` `fetch_all_new_messages()` 的日期限制只在 `is_first_run`
(`last_seen_id == 0`,即从未成功抓取过)分支生效;常规增量分支只按
`id > last_seen_id` 过滤,没有任何日期边界,分页上限 `MAX_HISTORY_PAGES = 200` 页。
一旦上游积压跨天(周期任务失败过一段时间/实例闲置数日),一次刷新会把几天的积压
全部拉回,即"点一次刷出 400+ 篇"的根因。

### 目标语义

无论首次运行与否,统一为:
1. 增量:只处理 `id > last_seen_id` 的消息(不变);
2. 仅限今天:只保留 `is_from_today()`(北京时间 `CST`,`fetcher.py:912`)为真的消息;
   今天之外的旧积压**直接丢弃**,且在确认"本页最新消息已早于今天"时**停止翻页**。

### 具体修改(`fetcher.py`,仅改 `fetch_all_new_messages()` 一个函数)

把现有 `if is_first_run: ... else: ...` 两个分支合并为统一逻辑(现首次分支的行为
就是目标行为,相当于把它推广到所有运行):

```python
while pages_fetched < MAX_HISTORY_PAGES:
    html = fetch_telegram_page(before)
    if not html: break
    msgs = parse_messages(html)          # 页内顺序:msgs[0] 最旧,msgs[-1] 最新
    if not msgs: break
    pages_fetched += 1

    seen_ids = {m["id"] for m in all_msgs}
    today_new = [m for m in msgs
                 if m["id"] > last_id
                 and m["id"] not in seen_ids
                 and is_from_today(m["datetime"])]
    if today_new:
        all_msgs.extend(today_new)
    # 停止条件一:本页所有消息都 <= last_id → 已追平增量游标
    if all(m["id"] <= last_id for m in msgs):
        log.info("  Caught up — stopping")
        break
    # 停止条件二:本页最新一条已早于今天 → 再往前翻只会更旧,全部丢弃并停止
    if not is_from_today(msgs[-1]["datetime"]):
        log.info("  Newest message on page is from before today — stopping")
        break
    # 分页锚点、<3 条提前退出逻辑保持不变
```

保留/沿用:
- `is_first_run` 仅用于日志文案(或直接删除该变量与冗余的 `pages_limit` if/else,
  两分支值本来就相同);
- `run()` 里的空库 bootstrap 检查(`fetcher.py:1017`,`article_count == 0` 时强制
  `last_seen_id = 0`)不动;
- `last_seen_id` 推进逻辑(`fetcher.py:1125-1136`)不动:`messages` 现在只含今天的
  消息,`max_id` 取其最大值即可。**被日期过滤丢弃的旧消息 id 小于今天消息的 id,
  游标推进后它们被永久跳过——这正是"直接丢弃"的预期语义**,无需额外处理。

### 边界情况

| 场景 | 行为 |
|---|---|
| 跨午夜运行(抓取中日期翻转) | `is_from_today` 每条独立判断,最坏丢掉刚过午夜前几分钟的消息,下轮周期(15min)不会找回(id 已被游标跳过)。可接受,与需求"直接丢弃"一致 |
| `datetime` 解析失败 | `is_from_today` 返回 `True`(现有行为,宽容保留),不变 |
| 页内新旧混排(今天+昨天同页) | 逐条过滤保留今天的;只有当"本页最新一条"早于今天才停止翻页,不会漏掉同页今天的消息 |
| 积压极大 | 停止条件二使翻页最多到"今天的边界"即止,自然消除 200 页深翻 |

### 测试(`tests/` 新增 `test_fetch_today_only.py` 或并入现有文件)

用 monkeypatch 替换 `fetch_telegram_page`/`parse_messages` 构造多页消息序列:
1. 常规增量运行(`last_seen_id > 0`)遇到昨天的积压消息 → 被丢弃,返回值只含今天的;
2. 翻页停止:第 2 页最新消息早于今天 → 不再请求第 3 页(对 `fetch_telegram_page`
   计数断言);
3. 同页混排:今天 3 条 + 昨天 2 条 → 只保留 3 条且继续按规则判断停页;
4. 首次运行(`last_seen_id == 0`)行为与原先一致(回归);
5. `run()` 集成:丢弃旧消息后 `last_seen_id` 仍推进到今天消息的最大 id。

已有测试影响:`test_streaming_refresh.py` 均 mock 掉 `fetch_all_new_messages`,不受影响;
`test_review_bug_hardening.py` 对 fetcher 源码的字符串断言需核对
(`grep "is_first_run\|First run"` 确认无引用后再删变量)。

---

## 2. 清理历史文章:录入框右侧增加日历日期选择组件

### 现状

`frontend/index.html:752`:纯文本输入框 `#purgeBeforeDate`(格式 `YYYY/MM/DD`,
`pattern` 校验),`normalizedPurgeDate()`(`index.html:3004`)转成 `YYYY-MM-DD` 提交;
服务端 `_parse_purge_before_date()`(`web_server.py:3489`)只接受不晚于今天的日期。
没有任何日历选择组件,手输体验差且易格式出错。

### 方案:原生 `<input type="date">` 作为隐藏取值源 + 📅 按钮触发

零依赖、移动端自动弹出系统日历、桌面端用 `showPicker()`。保留现有文本框
(可继续手输),在其右侧加日历按钮:

HTML(`index.html:751-755` 的 `source-step-actions` 内,文本框之后插入):

```html
<button class="ai-save-btn" type="button" id="purgeDatePickerBtn"
        onclick="openPurgeDatePicker()" title="选择日期"
        style="margin:0;width:auto;padding:8px 10px;background:var(--bg-card);color:var(--text);border:1px solid var(--border)">📅</button>
<input type="date" id="purgeDatePicker" aria-hidden="true" tabindex="-1"
       style="position:absolute;width:1px;height:1px;opacity:0;pointer-events:none">
```

JS(`normalizedPurgeDate()` 附近新增):

```js
function openPurgeDatePicker() {
  const picker = document.getElementById('purgeDatePicker');
  picker.max = new Date().toISOString().slice(0, 10);   // 与服务端"不晚于今天"一致
  const current = normalizedPurgeDate();
  if (current) picker.value = current;
  if (typeof picker.showPicker === 'function') {
    try { picker.showPicker(); return; } catch (e) {}   // 需用户手势,onclick 内满足
  }
  picker.style.pointerEvents = 'auto'; picker.focus(); picker.click(); // 兜底
}
// picker 的 change 监听:回填文本框并复位确认按钮
picker.addEventListener('change', () => {
  if (!picker.value) return;
  document.getElementById('purgeBeforeDate').value = picker.value.replaceAll('-', '/');
  document.getElementById('purgeConfirmBtn').disabled = true;  // 换日期后必须重新预览
  document.getElementById('purgeStatus').textContent = '';
});
```

要点:
- 选完日期只回填文本框,**不自动触发预览/删除**,沿用"预览 → 确认删除"两步流程;
- 换日期后强制禁用"确认删除"(避免用旧预览结果删新日期);
- `max` 设为今天,与服务端校验一致,减少无效提交;
- `showPicker()` 在 iOS Safari 16+/Chrome 99+ 可用,兜底走 focus+click;
- 隐藏 date input 用 `aria-hidden` + `tabindex=-1`,不进 Tab 序。

### 测试

- 契约测试(`test_access_and_ui_contracts.py` 风格):断言 `id="purgeDatePicker"`、
  `openPurgeDatePicker` 存在,change 回填逻辑包含 `replaceAll('-', '/')`,
  以及回填后 `purgeConfirmBtn` 被禁用;
- 手工走查:桌面 Chrome(showPicker)、iOS Safari(系统滚轮日历)、
  手输仍可用、选择今天之后的日期被 max 阻止。

---

## 3. 资源占用 Tab 移动端宽度溢出优化

### 根因

`index.html:360-367`:
- `.admin-stat{display:flex;gap:16px}` 一行硬排 4 个 `flex:1` 的统计盒,**不换行**;
- `.admin-stat-detail{white-space:nowrap}` 使每盒最小宽度被
  `"1.5 GB / 2.0 GB"` 这类文本撑大(flex 项不能收缩到 min-content 以下);
- 4 盒 + 3 个 16px gap 在 ≤400px 屏上总宽超出 `.ai-body`(`overflow-x:hidden`,
  `index.html:424`),内容被裁切/撑破。

### 方案:≤640px 时改为 2×2 网格(比横向滚动更合理),辅以细节收敛

在现有 `@media(max-width:640px)`(`index.html:502`)块内追加:

```css
.admin-stat{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.admin-stat-box{padding:10px 8px;min-width:0}
.admin-stat-detail{white-space:normal;word-break:break-all;font-size:10px}
.admin-stat-ring{width:64px;height:64px}
.admin-stat-ring::before{width:52px;height:52px}
```

- 两行统计区(4 个环形 + 4 个数字盒)各自变 2×2,总宽恒等于容器宽,任何窄屏都不溢出;
- `min-width:0` + `white-space:normal` 解除 nowrap 撑宽;
- 桌面端(>640px)样式零改动;
- 顺带核查同 Tab 其余部分:存储明细行(flex space-between)、进度条、
  清理操作行(`source-step-actions` 已 `flex-wrap:wrap`)在 375px 宽下均无溢出,
  无需改;用户管理 Tab 的 `.admin-table` 已有 `overflow-x:auto` 包裹,不动。

选型说明:需求给出"横向滚动或其他更合理形式"两个方向。4 个等权重统计项
用 2×2 网格一屏全览,优于横向滚动(滚动会隐藏一半指标且 iOS 上滚动提示不明显);
表格类宽内容(用户管理)才适合横向滚动,且已具备。

### 测试

- 契约测试:断言 640px 媒体查询块内含 `.admin-stat{display:grid` 与
  `grid-template-columns:1fr 1fr`;
- 手工走查:DevTools 375px/320px 宽度下打开 管理员设置 → 资源占用,
  确认无横向溢出、环形图与文字完整;>640px 桌面布局与现状一致。

---

## 交付顺序与工作量

| 序 | 项 | 文件 | 预估 |
|---|---|---|---|
| 1 | 仅限今天抓取 | `fetcher.py`(1 个函数)+ 新测试 | ~60 行改动 + ~120 行测试 |
| 2 | 日期选择组件 | `frontend/index.html`(HTML 3 行 + JS ~25 行)+ 契约测试 | 0.5h |
| 3 | 移动端网格适配 | `frontend/index.html`(CSS ~6 行)+ 契约测试 | 0.5h |

风险最高的是第 1 项(行为收紧),发布说明需注明:**升级后旧积压(今天之前)的
未抓取消息将不再被拉取**;如有用户依赖"补抓历史",需另行讨论补抓入口(本次不做)。
