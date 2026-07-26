# Asia FX Weekly Model — 改进档案 / Improvement Registry
**版本 v1 · 2026-07-25 · 分支 claude/picture-to-code-20nq5a**

> 用途:这是一份可以**逐条去改**的登记表。每条有编号(便于你回复"改 M3"/"D5 先不做"),
> 类别、优先级、依据(哪份报告/页 或 我们自己的审计)、需要的数据、落地方式、状态。
> 四份外部材料:MS《Beyond Carry》、MS《Realized Skewness 专题》、MS《Quant 2026 展望》、
> JPM《From Vols to Correlation》;一本 Citi 教科书:Willer《Trading FI & FX in EM》。
>
> **编号规则**:Q=数据质量, D=要加的数据, E=每货币因子资格, M=模型/信号改动,
> X=需你拍板的冲突, G=因子墓地(勿重复测)。优先级 ★★★ 最高。

---

## 当前模型基线(改动前的状态)
- **宇宙**:9 亚洲货币 CNH IDR INR KRW MYR PHP SGD THB TWD;周五调仓;1W NDF/forward roll @ mid。
- **三支柱**:Fundamental {carry/realvol, CA YoY, ESI Δ, CDS Δ} · Momentum {12周动量, 实钱仓位, 实钱流量z} · Technical {Bollinger}。
- **Sentiment = regime 状态**:state = DXY趋势 × MSCI World趋势(4态),支柱间条件调权(shrink 25% 向等权)。
- **价值锚**:递归 PCA(28货币)+ 每货币协整 → 错误定价 ε(慢信号 + 极值风控)。
- **触发/sizing**:|综合分 z|>0.5 才交易 → top3进/跌出top5离 · 1/ATM-vol 加权 · dispersion 择时(0.5–1.5×)· 5% 目标波动。
- **净 Sharpe ≈ 0.76**;瓶颈 = 9 只货币高相关(PC1≈DXY 0.98),有效 breadth ~4.5,夏普天花板 ~0.9。

---

# Part 1 · 现有数据质量问题(实测审计,按严重度)

> 下面每一条都是我刚跑 `data/clean/clean_csv` 全表 profiling 得到的,不是猜的。
> "gap%" = 首个有效值到最后有效值之间的缺失比例。

| ID | 表 / 货币 | 问题(实测) | 严重度 | 建议动作 |
|---|---|---|---|---|
| **Q1** | `L1_carry_1m_ann` **MYR** | carry 数据**只从 2020-11 开始**(之前全空) | 🔴高 | MYR carry 因子在 2020 前不可用;要么补拉 Bloomberg MYR NDF-implied carry 历史,要么在 2020 前对 MYR **关闭 carry 因子**(见 E-MYR) |
| **Q2** | `L1_vol_realized_1m` **MYR** | 实现波动**只从 2019-04**开始 | 🔴高 | 影响 carry/realvol 因子;MYR 该因子历史极短,down-weight |
| **Q3** | `L1_carry_1m_ann` **CNH** | 从 2012-07 起且 **35% 缺口**;managed 货币 carry 意义弱 | 🟠中 | CNH carry 因子噪声大,建议对 CNH **降权/关闭**(见 E-CNH) |
| **Q4** | `L1_carry_1m_ann` **INR/TWD** | 各 **25% 缺口** | 🟠中 | ffill 上限内可用,但要意识到插值成分高 |
| **Q5** | `L1_rr25_2w` | **THB 19% / IDR 26% / MYR 33%** 缺口 | 🟠中 | 2周 RR 太破;RR 类信号统一用 **1M RR**(1M 缺口最小),2周仅作参考 |
| **Q6** | `L1_vol_implied_atm_1w` **MYR** | **31% 缺口**(THB/TWD 各 8%) | 🟠中 | 1W ATM vol 用于期权/VRP 时对 MYR 不可靠;VRP 因子对 MYR 关闭 |
| **Q7** | `L2_rr25_1m_z_52w` **MYR** | **21% 缺口** | 🟠中 | MYR 的 RR 情绪信号不可靠,down-weight |
| **Q8** | `L1_fwd_pts_1m/1w/tn` 全部 | **只从 2019-06 开始**;`fwd_pts_tn` **PHP 全空** | 🟡低-中 | 已知:所以我们用 1M implied carry/52 近似 1W roll。**继续用近似**,不要依赖原始 pip 远期点数(pip scale 不可靠) |
| **Q9** | `L1_govt_yield_*` **CNH 8% / PHP 从2013** | 国债曲线 CNH 缺口大、PHP 晚 | 🟡低 | 反正曲线斜率因子已进墓地(G),影响小 |
| **Q10** | `L1_pi_realmoney` / `flow_z` | **只从 2014 开始**(655周) | 🟡低 | 仓位/流量因子回测样本较短,评估时注意 |
| **Q11** | `L1_commodities` / `kr_semi_export` | 从 **2014** 开始 | 🟡低 | 商品/半导体因子回测限 2014 后 |
| **总结** | **MYR** | Q1+Q2+Q5+Q6+Q7 **全部指向 MYR** | 🔴 | **MYR 是系统性数据缺陷货币**——carry/vol/RR 多项残缺。ex-ante 理由:**整体 down-weight MYR 或在其数据不全的因子上关闭它**(见 E-MYR)。这不是过拟合,是数据事实。 |

**Part 1 结论**:数据质量问题高度集中在 **MYR(最严重)**和 **CNH carry(managed)**。这两条直接支持"每货币微调"——不是所有因子对所有货币都该开(见 Part 3)。

---

# Part 2 · 要增加的数据(完整清单)

> 三档:**A** 现有亚洲书就能用 / **B** 扩 EM / **C** 每货币专属先验。
> 状态:✅已有 ⬜要拉 🔶部分有。依据列注明报告出处。

## A 档 — 增强现有亚洲书(优先,门槛低)

| ID | 数据 | 解锁什么 | 来源 | 依据 | 状态 |
|---|---|---|---|---|---|
| **D1** | **US HY OAS / CDX HY 利差** | regime state(EM 头号驱动,排名 1.74 > VIX 1.09) | BBG `LF98OAS` / CDX HY | Willer 表2.3, Ch2.8 | ⬜ ★★★ |
| **D2** | **G3+CAD 融资腿**:EUR/JPY/CAD 1M carry | 篮子 funding overlay(回撤 −28.8%→−13%) | Velocity FWD | MS Beyond Carry Ex26 | ⬜ ★★★ |
| **D3** | **月度贸易差额**(比年度 CA 频率高、滞后小) | 6m 变化 fundamental 信号(替代 CA level) | Citi/BBG | Willer Ch4.4 | ⬜ ★★ |
| **D4** | **各国股指日度** | 横截面 equity-driven momentum(MS 最强FX因子) | BBG | MS 2026 / Beyond Carry | 🔶有周度`L1_equity`,补日度 ⬜ ★★ |
| **D5** | **跨资产隐含波动**:G10 FX vol、US 利率 vol(MOVE)、油 vol | max-implied-vol 风控 overlay | BBG | Willer Ch4.3 | 🔶VIX✅ 其余⬜ ★★ |
| **D6** | **亚洲交叉对 ATM vol**(CNH/KRW、KRW/TWD 等 cross) | JPM 隐含相关性 → breadth 前瞻择时 | Velocity/BBG | JPM 全文 | ⬜ ★ |

## B 档 — 扩到高质量 EM(每只货币一整套,和亚洲同款)

> 优先 **MXN**(对中国 beta≈0,正交性最高)→ BRL/CLP(商品轴)→ PLN/HUF(欧洲轴)。

| ID | 数据(每只货币) | 备注 | 依据 |
|---|---|---|---|
| **D7** | Spot USD/XXX | BRL/CLP=NDF;MXN/PLN/HUF 可交割 | Willer 表2.7/3.7 |
| **D8** | 1M forward-implied carry | 头号因子 | — |
| **D9** | ATM 1M vol + 25d RR | 情绪 + 1/vol 加权 | — |
| **D10** | 已实现波动 | carry 分母(或用隐含,见 X2) | — |
| **D11** | 月度贸易差额 / 经常账 | fundamental | — |
| **D12** | 主权 CDS 5Y | 风险 | — |
| **D13** | CTOT / 商品 ToT | **拉美尤其关键**(商品生产国轴) | Willer Ch2.6 |
| **D14** | Economic Surprise Index | ESI 因子 | — |
| **D15** | **EUR/USD** | CEEMEA(PLN/HUF)定价基准,非广义 DXY | Willer 表2.7 |

## C 档 — 每货币专属(实现 Part 3 的因子资格 + 事件 overlay)

| ID | 货币 | 专属数据 | 用途 | 依据 | 状态 |
|---|---|---|---|---|---|
| **D16** | CNH | PBOC 每日中间价 + 篮子权重;CNH-CNY basis;12m CNH fwd | fixing 偏离 state、延展度均值回归 | Willer Ch3.6 | ⬜ |
| **D17** | KRW/TWD | 外资股票净买入(交易所日度);半导体出口(已有`kr_semi`✅) | equity-flow 因子(北亚最强短期驱动) | Willer / MS | 🔶 |
| **D18** | INR | Brent 油价✅(在`commodities`);印度油进口额 | 油/CA 因子 | Willer | 🔶 |
| **D19** | SGD | **MAS S$NEER 篮子成分与权重** | SGD 公允价值锚(NEER band,非 vs USD) | Willer/MAS | ⬜ ★★ |
| **D20** | THB | 黄金价✅;旅游/入境数据 | 季节性 + 黄金相关 | Willer Ch4.11 | 🔶 |
| **D21** | 全体 | 分客群流量:leveraged / real-money✅ / corporate 分开 | leveraged-vs-realmoney 分歧信号 | Willer Ch4.9 | 🔶只有realmoney |
| **D22** | 全体(拉美优先) | 选举日历 + "市场不友好候选人≥25%支持"标记 | 选举前后 tilt | Willer Ch5.6 | ⬜ 低优先(手搓CSV) |

**如果只能给几样,按性价比:D1(US HY) > D2(G3腿) > B档 MXN全套 > D19(SGD NEER) > D3(月度贸易差额)。**

---

# Part 3 · 因子资格矩阵(每货币 × 每因子)——"受约束的异质性"

> 这是"每货币微调"的**正确落地方式**:信号定义共享(池化统计力量不丢),
> 但用**经济先验**决定每个因子对每只货币 **开(✓)/降权(◐)/关(✗)**。
> 这张表是先验、不吃自由度、不过拟合。**请逐格审阅并改**。

| 货币 | carry/vol | CA/贸易 | ESI | CDS | value(coint) | spot动量 | equity-flow动量 | 仓位/流量 | Bollinger | 季节性 | 商品/CTOT | 专属 overlay |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|---|
| **CNH** | ✗ Q3 managed | ✓ | ✓ | ◐ | ◐ 盯住扭曲 | ◐ 爬行 | ✓ | ✓ | ◐ 低波动假信号 | ✗ | ✗ 消费国 | ✓ fixing/basis (D16) |
| **KRW** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓★半导体 | ✓ | ✓ | ✓ 可交易 | ◐ | 外资股票流 (D17) |
| **TWD** | ✓ | ✓ | ✓ | ◐ 低 | ✓ | ◐ 干预+跳空 | ✓★半导体 | ✓ | ◐ 跳空分布 | ◐ | ◐ | 干预 overlay |
| **INR** | ✓ | ✓★油敏感 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ◐ | ✓ 油 (D18) | — |
| **IDR** | ✓★高carry | ✓★ | ✓ | ✓★risk-off beta | ✓ | ✓ | ◐ | ✓ | ✓ | ◐ | ✓ 煤 | US-HY 敏感 (D1) |
| **MYR** | ✗ Q1 数据2020起 | ✓ | ✓ | ✓ | ✓ | ✓ | ◐ | ✓ | ✓ | ◐ | ✓★棕榈油+油 | **整体 down-weight** |
| **PHP** | ✓ | ✓ 侨汇 | ✓ | ✓ | ✓ | ✓ | ◐ | ✓ | ◐ 低流动 | ◐ | ✗ | — |
| **SGD** | ✓ | ✓ | ✓ | ◐ 低 | **NEER★** (D19) | ✓ | ✓ | ✓ | ✓ | ✓★可交易 | ◐ | NEER band 锚 |
| **THB** | ✓ | ✓ 旅游 | ✓ | ✓ | ✓ | ✓ | ◐ | ✓ | ✓ | ✓★可交易 | ◐ | 黄金 beta (D20) |

图例:✓开 · ◐降权/条件开 · ✗关 · ★该货币的核心因子。
**季节性可交易的 SGD/KRW/THB 来自 Willer Ch4.11(点名这三只);商品轴 MYR/IDR/INR;equity-flow 北亚 KRW/TWD 最强。**

---

# Part 4 · 模型 / 信号改动清单(四份报告,已与我们现状对照)

| ID | 改动 | 现状 → 目标 | 依据 | 优先级 | 状态 |
|---|---|---|---|---|---|
| **M1** | **横截面 equity-driven momentum** | 我们枪毙的是"绝对股指动量"(G3);MS 用的是**按各国股市相对强弱排货币**的横截面版,是其最强 FX 因子(DM 夏普1.39) | MS 2026 Ex2/Ex12 | ★★★ | 用 `L1_equity` 零新数据先试 |
| **M2** | **G3 篮子 funding overlay** | 现在隐含纯美元 funding → 净 EM-long 敞口改 vs G3(USD+EUR+JPY)或 CAD,回撤砍半 | MS Beyond Carry Ex26 | ★★★ | 需 D2 |
| **M3** | **PMI 增长 regime 接线** | `US_ISM_PMI` 已在`L1_regime`但**没接进 state**;PMI 收缩期夏普 2.16 vs 扩张 1.59。把 state 扩成 DXY×PMI×risk | MS Beyond Carry Ex10 | ★★★ | 零新数据,纯代码 |
| **M4** | **US HY 利差 regime state** | 现有 state 无信用维度;HY 排名 > VIX。走阔时降 fundamental/carry 权重、砍 gross | Willer 表2.3/Ch2.8 | ★★ | 需 D1 |
| **M5** | **CA 用 6m 贸易差额变化 + carry 联署** | 现用 CA YoY level(书里 level 亏钱);换成 6m 变化并要求 carry 同号 | Willer Ch4.4 | ★★ | 需 D3(或先用现有CA做变化) |
| **M6** | **ESI 用零轴穿越 + 用 EM ESI 本身** | 现用 ESI change;改成 sign/零穿越特征;别减美国 | Willer Ch4.5 | ★★ | 有数据✅ |
| **M7** | **加 4 周快动量(双重波动调整)** | 现只有 12周动量(书里最弱窗口);加 1个月/4周,vol-调整动量本身而非只调仓位 | Willer 表4.5 | ★★ | 有数据✅ |
| **M8** | **cross-asset max-implied-vol 风控** | 取 {EMFX,G10,US rates,S&P,油}隐含vol z 的**最大值**>2σ 就减仓 | Willer Ch4.3 | ★★ | 需 D5 |
| **M9** | **HRP/等波动率跨 cluster sizing** | 现 1/ATM-vol 全局加权;扩池后按 HRP cluster(亚洲/商品/欧洲)分风险预算,才把新货币转成 breadth | Willer Ch10.3 | ★★(扩池后) | 需 B档 |
| **M10** | **仓位=动量(非反指)+ 熊市非对称短触发** | 已把仓位放 Momentum(✓做对);补:real-money 熊市跑输→3月前瞻负→减仓;牛市跑赢**不**反向 | Willer Ch4.10 | ★ | 有数据✅ |
| **M11** | **leveraged-vs-real-money 流量分歧信号** | 只有 real-money;分歧(leveraged 先动、real-money跟)有持续性 | Willer Ch4.9 | ★ | 需 D21 |
| **M12** | **价值锚:PPP/CTOT残差 + 更长回看;value 逆风将结束** | 我们协整 ε 已用 CTOT(✓被验证);可(a)偏好 PPP over REER (b)拉长错误定价回看;MS 说 value 2026 转正,可给 ε 多点权重 | Willer Ch4.6-7 + MS 2026 | ★ | 有数据✅ |
| **M13** | **CNH 延展度/basis 特征** | 12m CNH fwd 比 spot 弱>5% 历史上标记超卖/CNY 上涨耗尽 | Willer Ch3.6 | ★ | 部分有(fx_implied_yield_12m✅) |
| **M14** | **voting-classifier 组合器(浅层)** | 把三支柱子信号喂进浅树集成预测周度符号(BRL 上 IR 1.26 > 等权 1.04);保留基本面否决 | Willer Ch11.2 | ◐ 谨慎 | 有数据✅ |
| **M15** | **ATLAS 式"信号强度"维度进调权** | 现调权只用 trailing IC;加"当前信号强度/估值"自下而上维度 | MS 2026 ATLAS | ◐ | 有数据✅ |
| **M16** | **JPM 前瞻隐含相关性 breadth 择时** | 现 dispersion 乘数基于信号离散度;换/加**vol 隐含的前瞻相关性**(前瞻>已实现) | JPM 全文 | ◐ | 需 D6(粗版可用USD腿) |

---

# Part 5 · 需你拍板的冲突 + 因子墓地

## X 冲突(两种都合理,方向相反,需决定)

| ID | 冲突 | 现状 | 外部建议 | 我的倾向 |
|---|---|---|---|---|
| **X1** | carry 分母:实现波动 vs 隐含波动 | 你指示用**实现波动** | Willer Ch4.12:EMFX 应用**隐含波动**排序 | 跑对照,用 9 只上的 IC 决定;不擅自改你的指示 |
| **X2** | 季节性:已枪毙 vs 书里说可交易 | 墓地记录"−0.026 反号,已淘汰" | Willer Ch4.11 用**不同构造**(10年、偏离中位数>1%、vol调整)得 IR 1.0,点名 SGD/KRW/THB | **用他的精确构造重测一次**,不能因测过一个版本就永久拉黑 |

## G 因子墓地(现有数据已测无效,**勿重复**)

- 隐含 slope · 6 个国债 tenor-pair 斜率及其 4周变化 · CTOT 变化(13w) · 商品招牌篮子 · 曲度 · **绝对股指动量**(注意:横截面版 M1 未测,不在墓地) · 周度偏度(日度 skew 见 X/降级) · 周度 ε · 利率动量(隐含/国债 4周变化) · vol 期限结构 · 我们旧构造的季节性(见 X2 需重测)
- **利率差/rate momentum 在 EM 无效**(Willer 表4.2 验证:2年利率完美预见在 EM 亏钱)——**别再建亚洲利率动量信号**
- **realized skewness 在 FX 弱**(MS 专题:FX 是最弱资产类,夏普0.06-0.08;2026 预测 EM −2.4%)——**日度 skew 降为低优先级实验**,不是之前误传的"最强 EM 因子"
- **北亚外资股票流(KRX/TWSE 公开数据)周度全变体死亡**(2026-07 实测):KRW/TWD 的 raw flow z(IC −0.03/−0.05)、4周和、KRW−TWD 相对价差、|z|>1 触发式、以及 Willer 点名的"surge-not-in-price"变体(TWD 多头侧 t **−2.07** 反向显著)全部无效——**印证 Willer Fig4.20 "Asia FX leads equity flows"(流量追价格,不领先)**。数据保留在 `L1_country_factors`(TW_FOREIGN_NET 日度周求和 / KOSPI 系列),但不进模型;若未来做月度或事件口径可重访

---

# Part 6 · 优先级总表(建议执行顺序)

**第 1 波 · 零新数据,现有亚洲书上验证(1–2 周内可跑完)**
- M3(PMI regime)· M1(横截面 equity-flow 动量)· M6(ESI 零穿越)· M7(4周快动量)· X2(季节性重测)· Part 3 因子资格矩阵接线

**第 2 波 · 极少新数据,高回报**
- D1→M4(US HY state)· D2→M2(G3 funding)· D3→M5(月度贸易差额)· X1(vol 分母对照)

**第 3 波 · 扩池(需 B 档数据)**
- MXN→BRL/CLP→PLN/HUF 全套 · M9(HRP 跨 cluster)· 双 book 融合(亚洲 book + EM book)

**第 4 波 · 正交新账本(策略组合层,冲 1.2+)**
- D6→M16(隐含相关性 breadth 择时)· D19(SGD NEER 锚)· leveraged 流量分歧 · 选举 overlay(扩池后)

**数学预期**:第1-2波把亚洲书从 0.76 推向 ~0.9(结构天花板);要上 1.2+ 必须靠第3-4波的正交 breadth(全 EM 多因子 ~1.0 + 流量账 ~0.5 + 相关性账,两两低相关 → 组合 ≈ 1.3–1.5)。单一亚洲池到不了 1.2,这是 breadth 数学,不是信号问题。

---
*生成:2026-07-25 · 基于 data/clean 全表实测审计 + 5 份材料深读。每条可独立执行,回复编号即可。*
