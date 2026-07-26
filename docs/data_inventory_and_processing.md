# FX 模型 · 数据清单与处理方式
**版本 v1 · 2026-07-26 · 数据源:FX_Model_Data.xlsx(17 sheets)· 管线:data_pipeline_v2.py**

> 这份文档回答两件事:**(A) 我现在手上有哪些数据**、**(B) 每一样怎么清洗处理**。
> 配套文档:`model_improvement_registry.md`(改进项)、`FX_Model_数据库理解笔记.md`(早期理解)。

---

## 0. 全局约定(所有表通用)

| 项 | 规则 |
|---|---|
| **时间起点** | **2013-01-01**,之前的数据全部丢弃(2013 前亚洲期权薄市场、stale 严重) |
| **网格** | 统一对齐到**周五网格(W-FRI)**;日度源(spot/商品)采样到周五 |
| **as-of 对齐** | 每个观测放到"**≥ 其自身日期戳的第一个周五**"——**无前视** |
| **ffill 上限** | 周度源填 2 周,月度源填 8 周;超过就留 NaN |
| **spot 方向** | 统一 **USD/XXX**(数值上升 = 美元走强);EUR/GBP/AUD/NZD 从市场报价取倒数 |
| **缺失** | 保留 NaN,**从不插值** |
| **分母地板** | carry/vol 的分母(已实现波动)**floor 在 1%**,防管理货币近零波动导致比值爆表 |

## 交易宇宙

- **G4 funding 腿**:USD(基准)、EUR、JPY、CAD
- **亚洲 9(交易)**:CNH IDR INR KRW MYR PHP SGD THB TWD
- **EM 扩池(交易)**:BRL MXN CLP PLN HUF
- **合计 14 只交易货币 + 3 条 funding 腿**

---

## A. 数据清单(按 sheet + 用途)

### 市场价格类

| Sheet | 内容 | 频率 | 覆盖 | 喂给 |
|---|---|---|---|---|
| **Top 30 FX Spot** | 30 货币现货(含扩池 BRL/MXN/CLP/PLN/HUF/ZAR/TRY/CZK),MYR 从 Bloomberg 侧块 | **日度** 2001–2026 | 全 | spot 收益、动量、PCA、skewness |
| **FX Forward** | **双源**远期点数 1W+1M:主用 Bloomberg NDF(AL–CO)、备份 Citi(B–AJ) | 周度 2013+ | 14+G4 | carry-roll、funding 信号 |
| **Vol & RR** | 25d RR(CLP 是 10d)+ ATM vol,1M;MYR/THB/IDR/TWD 有 Bloomberg 补充(AM–AT) | 周度 | 全 | RR 情绪、VRP、1/vol 加权 |
| **Carry to Vol** | FX.CARRY 1M + 已实现波动 1M;**隐含收益率 AM–AP**(CNH/MYR/TWD/INR) | 周度 | 14+G4 | carry/vol 因子 |

### 收益率 / carry 类

| Sheet | 内容 | 用途 |
|---|---|---|
| **Govt & FX Implied Yield** | 各国国债 1Y/2Y/5Y/10Y + **各货币 1M/12M 隐含收益率 + SOFR 1M/12M** | **carry 重建(隐含收益率 − SOFR)**、曲线因子(墓地) |

### 基本面类

| Sheet | 内容 | 用途 |
|---|---|---|
| **Current Account** | 17 国经常账(月度)+ 巴西周度贸易差额 | CA YoY 基本面 |
| **Economics Surprise** | ESI 经济惊喜指数 + ISI 通胀惊喜指数 | ESI 变化/零轴穿越 |
| **ToT** | Citi 商品贸易条件(DM 4 + EM 14) | ToT 因子 |
| **REER** | 实际有效汇率(broad + 各货币) | 价值锚 |
| **CDS** | 主权 CDS 5Y(KRW/MYR/IDR/PHP/INR/BRL/PLN) | 风险因子 |
| **Commodities** | 棕榈油/小麦/Brent/黄金/大米/铜/半导体(SOX)/煤等 | 商品信号 |

### 情绪 / 流量 / regime 类

| Sheet | 内容 | 用途 |
|---|---|---|
| **FX Flows & Position** | Citi PI:**PI_RM(实钱)+ PI_LV(leveraged,新)+ PI_ZSCORE** | 仓位/流量、leveraged-vs-RM 分歧 |
| **Regime** | DXY、MSCI World、VIX、**MOVE、OVX、G7/EM FX vol、US HY OAS、G10 carry、ISM PMI** | 状态变量、跨资产风控 |
| **Countries Factor**(新) | **China fixing(FCCNYFIX)、CNY 篮子、泰国旅游(SETTOUR/到港)、韩国外资流(KPCPNT/KSFINET)、增长惊喜** | 国家专属因子 |
| **Equity Performance** | 12 股指(北亚 KOSPI/TWSE 为主) | equity-flow 动量(北亚) |

---

## B. 每类数据怎么处理

### 1. Spot(日度 → 周五)
- 读 Citi FX.SPOT 块(30 货币),EUR/GBP/AUD/NZD 取倒数统一成 USD/XXX,MYR 用 Bloomberg 侧块补。
- 日度采样到周五网格。输出 `L1_spot_usd_all30`(全)、`L1_spot_usd_asia9`、`L1_spot_usd_traded`(14)。

### 2. Forward 点数(双源 + 回落)
- **主用 Bloomberg(AL–CO)**:每货币自带日期列,as-of 对齐。
- **主用缺失 → 回落 Citi(B–AJ)**。
- 输出 `L1_fwd_pts_1w` / `L1_fwd_pts_1m`(USD/XXX pip)。
- **新增信号**:`L2_fwdpts_chg_4w`(点数 4 周变化)——funding 动态,你说的"预测 funding 不只 spot"的第一步。

### 3. Carry(★ 新口径:隐含收益率 − SOFR)
- **不再直接用 FX.CARRY**(它 CNH/INR/TWD 缺 2016–2020、MYR 从 2020)。
- **carry_XXX = (XXX 1M 隐含收益率) − (SOFR 1M)** —— CIP 一致,**无缺口**。
- 隐含收益率来自 Govt sheet(全货币 1M)+ Carry to Vol 的 AM–AP(CNH/MYR/TWD/INR 补充);SOFR 1M 来自 Govt sheet EQ 列。
- 输出 `L1_carry_1m_ann`。**这一步补上了之前所有 carry 缺口。**

### 4. Vol & RR(只留 1M,优先补充源)
- 只生成 **1M** 的 `L1_rr25_1m` 和 `L1_vol_implied_atm_1m`(**1W/2W 弃用**——stale 重灾区)。
- **MYR/THB/IDR/TWD 优先用 Bloomberg 补充块(AM–AT)**,替掉主块的 stale 冻结段;其余货币用主块。
- `L2_rr25_1m_z_52w`(52 周 z-score)。

### 5. carry/vol 比值(加分母地板)
- `L2_carry_to_realvol = carry_1m_ann / max(realized_vol, 1%)` —— 地板消除管理货币近零波动的爆表。

### 6. 基本面(月度 ffill + 变化)
- CA:月度 → 周五 ffill(8 周);`L2_ca_yoy_usdbn`(12 月变化)。
- ESI:`L2_esi_chg_4w`;后续可加零轴穿越。
- ToT/CDS/REER:as-of 对齐;`L2_ctot_chg_13w`、`L2_cds_chg_4w`。

### 7. Regime(状态变量,含新增)
- `L1_regime` 含:DXY、MXWO、VIX、**MOVE、OVX、EM/G7 FX vol、US HY OAS**、G10 carry、ISM PMI(月度 ffill)。
- 这些驱动支柱间调权 + 跨资产 max-vol 风控。

### 8. 流量(实钱 + leveraged)
- `L1_pi_realmoney`(PI_RM)、`L1_pi_leveraged`(PI_LV,新)、`L1_pi_rm_flow_z`。
- **新增**:`L2_pi_lv_minus_rm`(leveraged − 实钱 z 分歧信号)。

### 9. 国家专属因子(新表 `L1_country_factors`)
- China fixing 偏离、CNY 篮子、泰国旅游(SETTOUR + 到港)、韩国外资股票净买入。
- 对应 CNH/THB/KRW 的专属 overlay。

---

## C. 完整性门(★ 你的核心要求)

`completeness_report(L1, L2, asof)`:**每次预测前**,逐"交易货币 × 关键因子输入"(spot / carry / vol / RR / CA / ESI / CDS / ToT / equity / forward)检查是否齐全,**缺哪个直接列出来**,而不是静默用旧值。

天然滞后需注意的:**经常账(月度,滞后~4周)、ISM PMI(月度,滞后~3周)**——这两个用 ffill,门会标出"用的是 X 周前的值"。

---

## D. 数据质量:已解决 vs 待办

| 项 | 状态 |
|---|---|
| carry 缺口(CNH/INR/TWD 2016–20、MYR) | ✅ **隐含收益率−SOFR 重建,已补** |
| MYR/THB/IDR/TWD 的 RR/vol stale 冻结 | ✅ **Bloomberg 补充块替换** |
| carry/vol 近零波动爆表 | ✅ **分母地板 1%** |
| 2012 薄市场 stale | ✅ **起点 2013** |
| 1W/2W RR/vol 太 stale | ✅ **弃用,只留 1M** |
| 国债曲线 CNH 2017 洞 | ⚪ 无影响(喂墓地因子,不用) |

---

## E. 输出

- `data/clean/clean_csv/*.csv`(L1 清洗层 + L2 特征层)
- `data/clean/FX_Model_Clean.xlsx`(分组彩色 tab:MARKET DATA / TECHNICAL / FUNDAMENTAL / SENTIMENT)
- 运行:`python3 data_pipeline_v2.py <xlsx> data/clean` → 打印 coverage + 最新日期完整性报告

---
*生成 2026-07-26 · 基于 FX_Model_Data.xlsx 全 17 sheet 结构核查。管线 data_pipeline_v2.py 正在最终构建/验证中。*
