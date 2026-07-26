# 模型运作全解(Walk-through)
**v1 · 2026-07-26 · 现状 = layer1–5 @ 亚洲9(净夏普 0.76)· 数据层已升级到 14 货币,模型层重接是下一步**

> 本文回答三个问题:①三支柱怎么运作 ②怎么选货币做 L/S ③funding 怎么选。
> 第 5 节把**你提出的四步运转逻辑**和现状逐条对照,标出哪些已有、哪些要建。

---

## 1. 流水线总览(现状)

```
数据层(data_pipeline_v2)                     周五收盘后
  └→ 53 张表: L1 清洗 + L2 特征, 完整性门 14/14
第1层 · 价值锚(layer1_fair_value)             慢
  └→ 递归 PCA(28货币) + 每货币协整 → 错误定价 ε(慢速价值 + 极值风控, 不做周度引擎)
第2层 · 信号审计(layer2_ic_audit)             因子准入
  └→ 每个候选因子: 周度横截面 IC(总回报口径) + NW t 值 + regime 分段 → 活/死
第3层 · 组合引擎(layer3_portfolio)            核心
  └→ 三支柱综合分 → 触发器 → sizing(详见 §2-3)
第5层 · conviction + dispersion 择时           全局杠杆
  └→ 信号横截面离散度 z → 0.5-1.5× 乘数 + 5% vol target
产出(run_weekly)
  └→ weekly_signal.csv: 每货币 多/空/不动 + 权重
```

## 2. 三支柱怎么运作

### 成员(现状, 亚洲9 口径)
| 支柱 | 成员信号 | 口径 |
|---|---|---|
| **Fundamental** | carry/realvol(★IC最高 0.056) · CA YoY · ESI Δ4w · CDS Δ4w(反号) | 每周横截面 z |
| **Momentum** | 12周 spot 动量(实测已死, 待换4周) · 实钱仓位 · 实钱流量 z(与仓位99%重复, 待删) | 同上 |
| **Technical** | Bollinger 通道(walk-forward 参数自选, 周度) | ±1 信号 |
| **Sentiment** | **不是支柱, 是状态变量**: RR z / VIX / DXY趋势 / (PMI待接) → 决定支柱间权重 | regime |

### 三级合成
1. **支柱内**: 成员信号各自横截面 z 化 → 按 trailing 104 周 IC 加权(负 IC 地板为 0)→ 支柱分。
2. **支柱间**: regime 状态(DXY趋势 × MSCI趋势 4 态)→ 取"历史上同状态周"的各支柱 IC → 加权(25% 向等权收缩)。近期典型: Fundamental ~57% / Momentum ~31% / Technical ~12%。
3. **综合分**: Σ(支柱权重 × 支柱分) 再横截面 z 化 → 每货币一个 z 分。

## 3. 怎么选货币做 L/S(现状)

```
每周五, 对综合分 z:
  |z| > 0.5 才有资格(conviction 门, 你设计的, 实测胜出)
  → 做多 top3 / 做空 bottom3(进场); 已持仓者跌出 top5/bottom5 才离场(滞回, 降换手)
  → 腿内 1/ATM-vol 加权(低波动货币拿更大名义)
  → 全局: dispersion 乘数(信号分散度高=机会多=加杠杆, 0.5-1.5×) × 5% vol target
```
- **多空腿名义对称** → 组合近似美元中性 → 本质是**亚洲 RV**(空腿为多腿 funding)。
- 名义集中度上限: 单一信号不能扛整本书(min(1, n/4)×200% gross 规则)。

## 4. funding currency 怎么选(现状 + 可选)

- **现状: 横截面自融资。** 没有单独"选 funding currency"这一步——做空的货币就是 funding。因为亚洲货币对美元 beta 都高且相近, 多空对冲后残余美元敞口小。
- **可选 overlay(MS Beyond Carry)**: 若某周净敞口偏多 EM, 残差美元腿可用 G3(USD/EUR/JPY)或 CAD 篮子对冲——把回撤从 −28.8% 砍到 −13% 的是这一步。**数据已备(G4 腿全套), 未接线。**
- **扩池后(Willer)**: 跨 cluster 配对——亚洲(中国轴) vs 拉美(商品轴) vs 中欧(欧元轴), 配对时两腿波动匹配(vol-match)以剥离美元方向。

---

## 5. 你的四步运转逻辑 vs 现状(逐条对照)

> 你的架构 = **顺序确认流**: Fundamental 定方向 → Technical 定入场点 → Sentiment 确认
> → Funding 定执行方式。现状 = **并行加权流**(三支柱同时投票加权求和)。
> 两者不冲突——你的版本是把并行投票升级为**分工明确的流水线**, 且更贴近
> MS Ex9 的经济含义(Fundamental=方向/防御, Technical=择时, Sentiment=风控)。

### 第 1 步 · Fundamental 是否 supportive + regime + 每货币微调
**你的要求**: 看基本面方向; 不同 regime 下不同货币的基本面权重不同; 针对每个货币调 fundamental 的预测方式。
**现状**: ✅ regime 调权已有(但只调"支柱间", 且 4 态较粗); ✅ 因子资格矩阵已定义(每货币开/关因子)但**未接线**; ❌ "regime × 货币"二维微调没有——现在同一 regime 下所有货币的 fundamental 构成相同。
**要建**: ① 把资格矩阵接进 layer3(每货币只用自己开着的 fundamental 成员); ② regime 状态升级(加 PMI、US HY); ③ 每货币的 regime 敏感度先验(如 risk-off 时 IDR 的 CDS 权重↑、CNH 看 fixing、KRW 看半导体/外资流)。

### 第 2 步 · Technical(日度)找 entry point
**你的要求**: 技术面不是第三票, 是**入场时机**——基本面选出的货币, 用日度技术找 good entry。
**现状**: ❌ 技术面目前是周度 Bollinger、和基本面并行投票——角色不对。
**要建**: 把 Technical 从"并行因子"改为 **entry gate**: 基本面+情绪选出候选名单后, 用**日度 spot**(数据已升级到日度)的技术条件(回调至均线/通道位、短期超卖反转等)决定"这周进还是等"。walk-forward 选参框架可复用, 只是采样从周度换日度、角色从投票换闸门。

### 第 3 步 · Sentiment 是否 supportive
**你的要求**: 情绪作为确认项。
**现状**: ◐ 一半已有——RR z 同时做过状态变量和 overlay; layer5 测过"≥2 支柱同向"确认(整体夏普没提高, 但那是在并行架构下测的)。
**要建**: 每货币的情绪确认门: RR z(该货币期权市场情绪)+ 仓位不极端(避免拥挤入场)+ leveraged-RM 流量分歧。作为**一票否决/降权**, 不是加分项。

### 第 4 步 · Funding market 离异 → 1W vs 1M → 1M swap + 1W roll ⭐ 全新
**你的要求**: 看远期点数市场有无 dislocation; 有 mean-reversion 机会时选择 roll 的期限——买 1W 还是 1M, 甚至进 1M swap 然后按 1W roll, bet 1W1M 点数收敛。
**现状**: ❌ 完全没有——现在一律假设 1W roll(用 1M carry/52 近似), 从不选期限。
**这是真正的新 alpha 维度, 且数据已备**(fwd_pts_1w + fwd_pts_1m 双源, 17 货币):

**机制**:
- 把 1W 和 1M 点数各自年化 → **近端利差 vs 1M 利差的斜率** `slope = ann(1W) − ann(1M)`。
- **NDF 货币的点数 = funding + 预期**(你的原话): 分红季/季末美元荒/仓位挤压会把 1W 点数打到偏离 1M 隐含的水平——这类离异**均值回归**。
- **决策**:
  - `slope` 正常 → 照常 1W roll(灵活, 每周可调仓)。
  - 1W 点数**贵**(做多某货币时 1W roll 收 carry 少/付 carry 多)→ **锁 1M**: 一次锁四周 funding, 避开近端挤压。
  - 主动 RV: **进 1M swap + 每周用 1W 反向 roll 近腿** = 多 1M 点数、空 4×1W 点数 ≈ 纯赌 1W-1M 斜率收敛, 与 spot 方向无关——一个独立的 **funding RV 子账本**。
**要建**: ① `L2_fwdpts_slope_1w_1m`(年化斜率 + 52 周 z); ② 回测"斜率极端后是否收敛"(每货币); ③ 若成立: (a)被动版——主书按 z 选 roll 期限, (b)主动版——funding RV 子账本。

---

## 6. 落地顺序(模型层重接, 下一个大工程)

| 步 | 内容 | 对应你的逻辑 |
|---|---|---|
| 1 | layer1/2/3 接新数据 + 14 货币; 资格矩阵接线; Momentum 重构(4周动量, 删冗余flow); CNH fixing 信号并入 | 第 1 步 |
| 2 | regime 升级(PMI + US HY 进状态); real carry / 实际利率 进 IC 审计 | 第 1 步 |
| 3 | funding 斜率特征 + 离异收敛回测 → roll 期限选择 | 第 4 步 |
| 4 | Technical 改造: 日度数据 entry gate(替代并行投票) | 第 2 步 |
| 5 | Sentiment 确认门(RR z + 仓位 + 流量分歧, 每货币) | 第 3 步 |
| 6 | 新回测 vs 旧 0.76 基准; 顺序流 vs 并行流对照实验 | 全部 |

> 原则: 每步单独验证(IC/回测)再进下一步, 不一次全改——否则出问题无法归因。
