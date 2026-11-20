# RITCx Algorithmic ETF Arbitrage Bot

完整版包含两种赚钱机会：

1. **Tender 套利**：扫描完整盘口，为 tender 的全部数量计算 VWAP。扣除手续费、
   汇率影响并检查仓位限制后仍有足够利润才接受，然后立即反向平仓。
2. **ETF–成分股套利**：比较 `RITC × USD/CAD` 与 `BULL + BEAR`。ETF 贵时卖
   ETF、买两只股票；ETF 便宜时反向操作；价差收敛后平掉三条腿。

程序还包含订单拆分、盘口深度检查、动态仓位限制、可选 USD 对冲、结束前停止开仓和
异常恢复。Converter 按规则只能人工操作，因此代码不会尝试调用 Converter。

持仓读取会在 API 返回列表中按 ticker 精确寻找 `RITC`，不会再把第一个 `CAD`
条目误认为 ETF。根据 90,000 RITC 后续 Tender 被 TRADING_LIMIT 拒绝的实测，
ETF 限制权重按原案例规则恢复为 2 倍。

根据多轮实测，Tender 的 API `action` 与实际持仓变化一致：BUY 产生多头、SELL
产生空头。程序用该字段选择正确盘口，同时在接受后读取 RITC 实际持仓变化进行
二次确认，再发送严格相反的退出订单。
大 Tender 使用自适应退出：每批重新读取盘口，从 5,000 向下寻找仍高于 Tender
成本、手续费和 0.005 USD 安全边际的最大切片；不满足时等待盘口恢复，最长等待
12 秒，随后为控制方向风险而强制退出剩余仓位。
Private Tender 提交后会进入最多 10 ticks 的 pending 状态；期间暂停其他 RITC
策略，持续检测真实持仓变化，避免因结算延迟漏掉 70,000–100,000 份仓位。

Tender 结算后默认平掉 90%，并最多保留 8,000 份同方向 Runner。Runner 每股盈利
0.10 USD 时止盈、亏损 0.05 USD 时止损，tick 270 强制关闭；新 Tender 出现时也会
先关闭旧 Runner。可通过 `RIT_RUNNER_FRACTION`、`RIT_RUNNER_MAX_QUANTITY`、
`RIT_RUNNER_TAKE_PROFIT`、`RIT_RUNNER_STOP_LOSS` 调整，设 fraction 为 0 可关闭。

## 运行

在 RIT Client 中复制 API key，然后执行：

```bash
cd ritc_etf_arb
python -m pip install -r requirements.txt
export RIT_API_KEY='你的 API key'
python etf_arb_bot.py
```

默认 API 地址是 `http://localhost:9999/v1`。如比赛机器不同：

```bash
export RIT_API_URL='http://localhost:9999/v1'
```

三次实测中 USD 对冲降低了最终成绩，因此现在默认关闭。如需重新开启：

```bash
export RIT_HEDGE_USD=1
```

运行测试：

```bash
python -m unittest -v
```

程序会在 RIT Reset 后保持运行并等待下一轮 `ACTIVE`；不需要每次重新启动 Python。
需要彻底停止时在终端按 `Ctrl+C`。

开局 USD/CAD 尚未形成有效 bid/ask 时程序不会交易；任何超过 2 CAD/份的 ETF
套利差都会被视为错误/过期行情并跳过，避免用接近 0 的汇率制造虚假机会。

## 主要参数

程序默认要求前 10,000 份的净优势至少 0.05 CAD/份，同时允许整笔
70,000–100,000 份的保守 VWAP 最多有 0.01 CAD/份的初始劣势。Tender 出现时
如果暂未达标，不会立即拒绝；程序会在 API 提供有效 `expires` 时持续重算盘口，
直到到期前一 tick 再决定。这样提高接受率，同时继续过滤整笔深度明显亏损的机会。

门槛可以用 `RIT_TENDER_SAMPLE_EDGE` 和 `RIT_TENDER_FULL_EDGE` 环境变量调整。
增加接受率，但剩余数量仍有市场冲击风险，必须通过下一轮报告继续校准。

在 `etf_arb_bot.py` 顶部调整 tender 最低利润、ETF 套利开仓/平仓阈值、每次数量
和停止开仓 tick。不同 heat 的流动性不同，必须用 practice heat 调参。VWAP 只能
反映下单前的显示盘口；三腿交易也不是原子成交，所以代码不能保证利润。

ETF 三腿套利包含两个方向：RITC 偏贵时卖 RITC、买 BULL 和 BEAR；RITC 偏便宜
时买 RITC、卖 BULL 和 BEAR。开仓门槛为 0.22 CAD/份，每次 1,000 份，当前默认
开启。如需比赛时临时关闭：

```bash
export RIT_ENABLE_BASKET_ARB=0
```

三腿订单不是原子成交，而且 Converter 只能手动操作，因此该策略的滑点风险高于
Tender。较低门槛配合较小仓位，用于增加机会但限制单次滑点；程序每 10 ticks
打印一次 `ARB scan`，即使没有下单也能看到实时 rich/cheap 价差。参数可通过
`RIT_ARB_OPEN_EDGE`、`RIT_ARB_ROUND_TRIP` 和 `RIT_ARB_QUANTITY` 调整。
