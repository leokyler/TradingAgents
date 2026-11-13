# 单次 ETF 分析性能实测报告

- 日期：2026-11-12 18:18–18:28（Asia/Hong_Kong）
- 标的/参数：`515070.SS`（华夏中证人工智能主题 ETF），`2026-11-12`
- 分析师：`market,etf,news`（ETF 默认团队），辩论/风控各 1 轮
- 模型：`opencode-go / deepseek-v4.1-flash`（quick 与 deep 同款，deep effort=high）
- 输出语言：中文；保存报告（Markdown + HTML）
- 采样：独立监控进程，每 0.5 s 采样整棵进程树（RSS/PSS、线程、CPU ticks、块IO、系统网络），共 1179 个采样点
- 缓存：全新冷缓存（`.bench/cache`），等价于云上首次运行

## 1. 结论

**这是一次典型的 I/O-bound（等 API）任务，不是算力任务。**

- 全程 9 分 50 秒里，CPU 只用了 **6.2 秒**（平均 1.1% 单核，98.1% 的采样点 CPU < 5%）
- 峰值内存 **244.6 MB**（PSS 235.6 MB），启动完成后常驻约 244 MB
- 网络 **约 4.1 MB 下行 / 4.7 MB 上行**（经本地代理，含 TLS 开销）
- 磁盘写入 **1.8 MB**，单次产物 800 KB
- 真正的成本是大模型 token：**23 次调用，274.5k 输入 / 86.7k 输出 token**

> 结论：**1 vCPU / 1 GB / 10 GB 的入门云主机就够跑**；2 vCPU / 2 GB 更从容。
> 选云的第一变量是**网络可达性（Yahoo、东方财富、LLM 中转）**，不是 CPU/内存。

## 2. 关键指标

| 指标 | 实测值 | 说明 |
|---|---:|---|
| 总耗时 | 589.8 s（9m50s） | 启动到报告落盘 |
| CPU 总时间 | 6.2 s | 占单核 1.1%，含启动/报告生成 |
| CPU 峰值 | 375.9%（1 s） | 仅在 Python 导入/初始化瞬间；3 s 后归零 |
| 报告生成瞬间 | 58%（0.5 s） | HTML 渲染 + 落盘 |
| 峰值 RSS | 244.6 MB | 出现在第 436 s（风控/组合经理阶段） |
| 峰值 PSS | 235.6 MB | 更接近容器实际占用 |
| 启动后基线 | 176 MB / 2.5 s | 导入 pandas、langgraph 等 |
| 线程峰值 | 35 | HTTP 客户端 + LangGraph |
| 磁盘写（物理） | 1.8 MB | 报告/日志/缓存 |
| 磁盘读（物理） | ~0 MB | 全部命中页缓存 |
| 网络 下行/上行 | 4.1 / 4.7 MB | 经代理；直连时数值会不同 |
| LLM 调用 | 23 次 | 平均 11.9k 输入 / 3.8k 输出 token/次 |
| Tool 调用 | 44 次 | 行情/新闻/宏观/预测市场 |
| Token 合计 | 274,522 in + 86,741 out = 361,263 | 单次分析 |
| LLM 延迟 | p50 21.9 s / p90 72.6 s / max 109.8 s | 23 次串行累计 702 s（分析师并行抵扣后总耗时 590 s） |

## 3. 时间线（并行结构）

```
18:18:08  启动，3 个分析师同时开始
18:19:11  ETF 分析师完成      (62.6 s)
18:19:58  Market 分析师完成   (110.2 s)
18:20:06  News 分析师完成     (118.1 s)   ← 分析师阶段由最慢者决定
18:20:06 → 18:23:22  多空辩论 + Research Manager + Trader   (196 s，约 4 次 LLM 调用)
18:23:22 → 18:27:55  风控三方辩论 + Portfolio Manager + 报告落盘 (273 s)
18:27:55  完成，报告 106 KB MD / 153 KB HTML
```

分析师阶段耗时 ≈ max(110, 63, 118) 而非三者相加，说明并行生效；总耗时约 2/3 花在两轮串行辩论和最终决策上。

## 4. 跑一次需要什么

**硬件**：无 GPU；单核足够；1 GB 内存可运行，建议 1–2 GB 留余量。

**软件**：Linux + Python 3.11–3.13（本次 3.13.15）；依赖约 350 MB（venv）；仓库自带 Dockerfile（python:3.13-slim，镜像预计 450–550 MB）。

**凭据**：LLM key（当前 `OPENCODE_GO_API_KEY`）；`FRED_API_KEY` 可选（缺失时宏观数据降级，不影响完成）；无需数据库。

**网络（出站 443）**：
- `query1.finance.yahoo.com` 等 Yahoo 接口（价格、新闻、技术指标）
- `push2.eastmoney.com` / `fundf10.eastmoney.com`（A 股 ETF 基金资料、持仓、估值）
- LLM 中转（本次 `opencode.ai`）
- `polymarket`（预测市场，可选）、`sec.gov`（美股基本面，本标的未用）

**目录**：默认写 `~/.tradingagents/{logs,cache,memory}`；容器里挂 volume 即可。单次产物 800 KB，缓存 136 KB，可长期保留。

**无人值守运行**：必须给全 flags + 环境变量（无 TTY 时缺一即拒绝启动）：

```bash
TRADINGAGENTS_OUTPUT_LANGUAGE=Chinese \
TRADINGAGENTS_MAX_DEBATE_ROUNDS=1 TRADINGAGENTS_MAX_RISK_ROUNDS=1 \
TRADINGAGENTS_RESULTS_DIR=... TRADINGAGENTS_CACHE_DIR=... \
python -m cli.main --ticker 515070.SS --date 2026-11-12 \
  --analysts market,etf,news --save --no-show
```

## 5. 云服务器建议

| 场景 | vCPU | 内存 | 磁盘 | 说明 |
|---|---:|---:|---:|---|
| 最小可用 | 1 | 1 GB | 10 GB | 峰值 245 MB，留 3–4 倍余量；单次约 10 分钟 |
| 推荐 | 2 | 2 GB | 20 GB | 启动/报告有 1 s 级多核突发，2 核更顺 |
| 批量（4–6 并发） | 4 | 4 GB | 40 GB | 内存约 250 MB/并发，线性增长；真正瓶颈是 LLM 限流 |

**带宽**：每跑约 10 MB，1 万次分析也才 100 GB，忽略不计。

**地区选择（比配置重要）**：
- 境外/香港/新加坡：Yahoo 顺畅，但东方财富 `push2` 可能超时（本次就失败）
- 中国大陆：东方财富顺畅，但 Yahoo 基本不可用
- 该应用同时依赖两者（Yahoo 取价、东财取 A 股 ETF 基金数据），**建议选香港/新加坡等国际出口好的区域，并实测两端可达性**；或在一台机器上配置分流代理
- 本次实测端点延迟：Yahoo 约 1.4 s、东财 `push2` 超时、LLM 中转 TLS 4.6 s（网络质量差时 LLM 延迟是总耗时主因）

**并发建议**：同一台 2 GB 机器可跑 4–6 个并发分析（内存够），但 23 次 LLM 调用/单次，并发高会先撞 API 限流；建议队列化 + 限并发 2–4。

**成本结构**：服务器成本 ≈ $5/月（1C1G）即可覆盖；主要成本是 token（361k/次）与 API 套餐，与放不放在云上无关。

## 6. 实测中发现的坑（部署前必看）

1. **ETF 分析会在 CLI 显示层崩溃**（本仓库当前代码）：
   `KeyError: 'etf_report'` —— `cli/display.py` 的 `section_titles` 缺少 ETF 段标题。发生在 ETF 报告生成、更新状态栏时，进程退出码 1。
   已在工作区修复（2 行：补 `"etf_report": "ETF Analysis"`，并用 `.get` 兜底），`pytest -k "display or report_section or etf"` 52 项通过。**不修则任何带 ETF 分析师的运行都会在分析师阶段结束后崩溃。**
2. **venv 里有一份旧的 site-packages 副本**（`tradingagents 0.7.1`，缺 ETF 探测等新代码）。直接跑 `tradingagents` 命令会用到旧代码，表现为把 `515070.SS` 识别成普通股票并拒绝 `--analysts etf`。云上部署务必用仓库源码：`pip install -e .` 或 `python -m cli.main`（cwd 在仓库根）。
3. 本次网络下 Yahoo 新闻（crumb/cookie）与东财 `push2` 行情失败，产生了 8 条 `DATA_UNAVAILABLE`；报告仍完成（Sell），但 **token 数可能比网络健康时低 10–20%**，CPU/内存结论不受影响。
4. 组合经理的结构化输出失败并回退纯文本重试了一次，会多计一次调用/token，属概率性抖动。

## 7. 复现

```bash
# 已放在 .bench/ 下（未提交）
.bench/run_etf.sh                     # 默认 515070.SS + 2026-11-12，自动监控
# 产物
.bench/logs/etf-clean-2026-11-12.{json,csv,log,tokens.json}
.bench/results/reports/515070.SS_20261112_182755/complete_report.{md,html}
```

监控脚本：`.bench/monitor.py`（进程树采样）、`.bench/run_agent.py`（token/延迟统计包装）。

## 8. 局限

- 单次样本；LLM 延迟波动大（p50 22 s、max 110 s），总耗时 6–12 分钟都属正常。
- 网络经本地代理（127.0.0.1:7897），部分数据源失败；换云后建议先跑一次连通性自检。
- 并发内存为线性估算，未实测多开。

## 9. Yahoo 限流的替代：cn_market 实测（2026-11-13）

新增无 key 的 A 股价格 vendor：`cn_market`，按 Eastmoney → Tencent → Sina 顺序取日线，指标复用 stockstats；非 A 股标的自动落到下一个 vendor。通过 `TRADINGAGENTS_DATA_VENDORS` 环境变量按类别配置。

- 配置（已写入 `.env`）：
  `{"core_stock_apis": "cn_market,yfinance", "technical_indicators": "cn_market,yfinance"}`
- 本次运行时 Eastmoney 的 push2his 被本机出口封锁，Tencent 接管，价格与指标正常返回（131 条日线、RSI/MACD 等）。
- 完整运行：**1166 s（19m26s）**，退出码 0，报告正常落盘；CPU 4.8 s，峰值内存 216 MB，19 次 LLM 调用（169k in / 72.7k out）。
- 仍走 Yahoo 的部分：新闻（`news_data: yfinance`）、`get_verified_market_snapshot`（本次运行时代码尚未包含 fallback，报告因此带“数据可用性声明”）。修复后单独实测该工具：Yahoo 429 → 自动用 cn_market 重建快照，返回 `close 0.90 / RSI 45.5 / 50SMA 0.997` 等真实数值，`SNAPSHOT_OK`。
- 注意：本次 `net_tx` 235 MB 明显异常，且随时间匀速增长（空闲阶段也在涨），判定为本机其他流量/代理所致，不是本进程；网络需求仍以 11-12 那次完整运行（4.1/4.7 MB）为准。

结论：Yahoo 被限流时，A 股 ETF 的价格与指标已能由 cn_market 提供，证据门不会再把有价格数据的运行误判成 REVIEW；`get_verified_market_snapshot` 也已改为回退到配置的价格 vendor。

## 10. Tushare 接入现状（2026-11-13）

- token 有效（56 字符），但账号是**免费档**：`daily`、`stock_basic`、`trade_cal`、`index_daily`、`daily_basic` 可用；所有 `fund_*` 与 `index_weight` 返回 `40203 没有接口访问权限`。
- 已把免费档接入**股票**链：`.env` 中 `core_stock_apis` / `technical_indicators` 改为 `cn_market,tushare,yfinance`。
- 交叉校验 `600519.SS`（2026-11-01～11-13）：Tushare 与腾讯（cn_market）收盘价 **10 天全部一致，最大偏差 0.00**；成交量口径也一致（手 ×100 → 股）。
- 路由实测：默认链由 cn_market（腾讯）先服务；把链临时切成 `tushare,cn_market` 时，Tushare 正常返回日线与 RSI，说明接入路径无误。
- ETF 分析不受影响，仍走 `cn_market`（价格/指标）+ `eastmoney`（基金资料/持仓/估值）。要切 Tushare 需要 **2000 积分**（fund_daily/nav/share/basic/div）；持仓与指数权重需要 **5000 积分**（fund_portfolio/index_weight），对应代码尚未实现。
