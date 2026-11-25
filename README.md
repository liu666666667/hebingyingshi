# hebingyingshi · 影视配置每日汇总仓库

把多个上游影视源每天自动汇总 → 强力去重 → 去广告去有害 → 失效隔离 → 分层合并，最后只产出**一个能直接导入影视仓的稳定订阅地址**。

## 订阅地址

> 页面会自动探测下列地址能否访问，以页面显示为准。

- 稳定版：`https://liu666666667.github.io/hebingyingshi/output/stable.json`
- 全量版：`https://liu666666667.github.io/hebingyingshi/output/final.json`
- 直播：`https://liu666666667.github.io/hebingyingshi/output/live.m3u`
- 备用（raw）：`https://raw.githubusercontent.com/liu666666667/hebingyingshi/main/output/stable.json`

复制地址到影视仓 App 的「配置地址」导入即可。

## 数据来源

主力（可信源，冲突时优先）：
- `tvbox-api-backup` · `tvbox-ysc-config` · `awesome-zhuiju-free` · `yingshicang`

补位（自动发现）：
- `yingshi` 的 daily discovery 结果

## 处理规则

1. **每日一更**：北京时间每天 20:00 自动跑。
2. **快照兜底**：上游拉取失败自动用最近一天快照，订阅不中断。
3. **强力去重**：线路按主 URL、站点按 api+名称归一化，跨全部源去重。
4. **去广告/有害**：博彩、广告、推广、登录/付费、网盘登录、GO 本地代理、图片伪装爬虫一律剔除。
5. **失效隔离**：连续失效 7 次的线路移入不可用区，7 天后尝试复活；成人/误判类进待定隔离区，人工判断。
6. **分层合并**：可信源全收，发现源只补可信源没有的站点。
7. **稳定版**：正常运行后只保留连续存活的线路；**首次运行全量可用**。

## 产出文件

| 文件 | 说明 |
| --- | --- |
| `output/stable.json` | 稳定版单仓 |
| `output/final.json` | 全量版单仓 |
| `output/multi.json` | 多仓订阅 |
| `output/live.m3u` | 直播源 |
| `output/changelog.md` | 每日变更（新增/移除/复活/失效） |
| `output/status.json` + `index.html` | 状态页面 |
| `data/unavailable/` | 失效隔离区（不进公开订阅） |
| `data/snapshots/日期/` | 每日快照 |

## 配置

- `config/sources.json`：上游入口（增删源改这里）
- `config/rules.json`：去广告/有害规则
- `config/discovery.json`：登录/GO/图片spider/成人词表
- `config/settings.json`：全局参数（门槛天数、超时、告警阈值等）

## 手动触发

在 GitHub 仓库页面进入 Actions → 「每日影视汇总更新」→ Run workflow。
