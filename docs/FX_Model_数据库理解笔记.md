# FX_Model.xlsx 数据库理解笔记

> 供批改用。我的理解按 sheet 逐个写,每个 sheet 末尾有【疑问/待确认】。最后是跨 sheet 的整体问题清单。

## 0. 总体理解

- **数据源**:主体是 **Citi Velocity**(`CVTSHIST` 公式,ticker 带 `.CITI` 后缀),参数统一为 `WEEKLY / MAX / CLOSE`;另有 5 个 sheet 是 Bloomberg 数据(Regime、FX Implied Yield 的本地债券部分、Equity Performance、Current Account、Commodities)以静态块形式粘贴。
- **频率**:全库以**周度**为主(日期戳为周五),个别块是月度(PMI、Current Account、KOEISEU)。也就是说这个模型将是一个**周频模型**——比 Compass30 的月度细、比 MS 的日度粗。
- **标的**:9 个亚洲货币(CNH/CNY、IDR、INR、KRW、MYR、PHP、SGD、THB、TWD),与我们讨论的清单一致,未含 HKD(与我建议一致)。
- **两个空 sheet**(`Sentiment`、`Fundamental`)我理解为**分组标签页**:Sentiment 组 = Vol & RR、FX Flows & Position、Carry to Vol;Fundamental 组 = Economics Surprise、FX Implied Yield、CDS、Equity Performance、Current Account、ToT、Commodities;Technical 组 = Top 30 FX Spot(趋势/偏度由它派生)。
- **与原清单的对照**:数据库已覆盖清单的大部分,并且有两处**比我原清单更好**:(1) CitiPI 仓位数据(我说过这是对标 MS 自家 positioning tracker 的内部数据);(2) Citi ESI 经济意外指数(我清单里没有,是合理补充)。缺口见第 16 节。

---

## 1. Top 30 FX Spot(1986 行,1988-07-01 → 2026-07-17)

- 30 个货币兑美元即期,周度收盘,`FX.SPOT.*.CITI`。
- 用途 = **PCA 因子的原材料** + 9 个标的的价格序列(趋势、realized skew 也从这里派生)。
- 报价方向混合:EUR/GBP/AUD/NZD 是 XXX/USD,其余是 USD/XXX——**做 PCA 前必须统一方向**(全部转成同一方向再取 log)。
- 人民币用的是 **CNH**(不是 CNY)。没有 RUB(好事)。
- **重要发现**:各列历史长短不一,最短的列只有 **382 个周度点(≈2019 年起)**。PCA 需要平衡面板,如果取所有 30 列的公共样本,历史会被最短的列拖到只剩 ~7 年——这会严重伤害协整估计。
- 【疑问/待确认】① 哪几个货币是短历史?是否接受"PCA 用长历史子集(比如 20 个货币),9 个标的仍全覆盖"的方案?② 1988 年起的超长历史中,亚洲货币在 1997 危机前后的报价制度不同(THB/MYR 曾盯住美元),PCA 样本起点你想设在哪(我倾向 2005 或 2010 之后)?

## 2. Asia FX Forward(369 行,2019-06-28 → 2026-07-17)

- **远期点(pips)**,`FWD_POINT_PIP`,期限 **TN / 1W / 1M**,9 个货币(CNH 口径)。PHP 缺 1W(只有 TN 和 1M)。
- 我的理解:这个 sheet 是**执行/换算层**——远期点 + 即期 = outright,TN 点用于滚动成本;**不是** carry 信号的主数据源(历史太短,只有 7 年)。carry 信号应来自 Carry to Vol sheet(2011 年起)。
- **缺口**:没有 **12M** 远期 → slope differential(12M 隐含利差 − 1M 隐含利差)造不出来。
- 【疑问/待确认】① 远期点的 scale(每个货币 pips 的十进制位)需要一张换算表,Velocity 的 PIP 定义是否与 BBG 一致?② 12M 你打算从 Velocity 补拉,还是用 FX Implied Yield sheet 的国债 1Y 利差替代(有在岸/离岸 basis 的瑕疵)?

## 3. Regime(周度 2010-01-01 → 2026-07-17;PMI 月度)

- DXY、MSCI World(MXWO)、VIX、**DBDRCDMU**(标注 DB G10 Carry)、**JPMVEM1M**(标注 JPM EMFX Vol,即 JPM EM-VXY 1M)+ 美国 ISM PMI(NAPMPMI,单独月度块)。
- 与我建议的 regime 三件套(美元方向/风险偏好/美国增长)完全对应;且你用 **EM-VXY 替代了我建议的全球 VXY**——对亚洲 EM 更贴,我认同。
- 这些序列同时兼任 **PCA 因子校验**:第一主成分 vs DXY、第二 vs DB carry、第三 vs EM-VXY。
- 【疑问/待确认】DBDRCDMU 具体是 DB 哪条 carry 指数(G10 carry USD 版?),校验第二主成分时用它没问题,但注明它是 G10 口径、不是 EM carry。

## 4. Vol & RR(1544 行,1996-12-20 → 2026-07-17)

- **隐含波动率**:25-delta RR 和 ATM,期限 **1W / 2W / 1M**,9 个货币,`IMPLIED.CITI`。
- 用途:RR = sentiment 因子(替代亚洲没有的 CFTC 仓位);ATM = 权重的波动率缩放 + vol 因子。
- 历史起点:多数 1996-97 年起,INR 2000,CNY 1998。
- **两个数据坑**:① 人民币期权用的是 **CNY**(在岸),而即期/carry/仓位用 CNH——同一货币两个标的混用;② **MYR 的 RR 缺 628/1544 个点**(1998 资本管制 + 离岸市场断档),MYR 的期权序列实际连续可用段很短。
- 【疑问/待确认】① CNY vol 和 CNH 即期混用你接受吗?(Velocity 上如有 USD/CNH vol 建议换)② MYR 情绪因子是否降级为"只用仓位、不用 RR"?

## 5. FX Flows & Position(655 行,2014-01-03 → 2026-07-17,无缺失)

- **CitiFX 仓位指标(PI)**:每货币两列——`PI_RM`(我理解为 raw/rolling 仓位度量)和 `PI_ZSCORE.PI_FLOWSRM`(客户流量的 z-score),9 个货币(CNH 口径)。
- 这是全库**最有独占价值**的 sheet(外部买不到),对标 MS 自家 positioning tracker,归 sentiment 因子。
- 【疑问/待确认】PI_RM 的确切定义(单位?窗口?是流量累计还是仓位存量?)——这决定信号用法:极端仓位做反向,还是流量动量做同向。请你给我 Velocity 上 PI 的说明或口径定义。

## 6. Carry to Vol(1609 行,1995-09-22 → 2026-07-17)

- 两块:**1M 年化 carry**(`FX.CARRY.USD.xxx.1M.MID.ANNUAL.CITI`,即远期隐含 carry——与 MS 的定义一致,与我们"用远期不用 OIS"的结论一致)+ **1M REALISED vol**(注意:是**已实现波动率**,不是隐含!隐含在 Vol & RR sheet)。
- 明显意图:构造 **carry-to-vol ratio** 信号(carry 除以 vol 的风险调整 carry)。
- 历史起点:carry 多数 **2011-01** 起,CNH 2012-07,**MYR 只有 2020-11 起**(很短!);realized vol 1995-97 起(MYR 2019 起)。
- 【疑问/待确认】① carry/vol 比率你打算用 realized 还是 implied vol 做分母?(用 implied 更前瞻,数据也更长)② MYR 的 carry 只有 ~5.5 年,横截面排名时 MYR 早期会缺席,接受吗?

## 7. Economics Surpirse(1229 行,2003-01-03 → 2026-07-17)

- **Citi 经济意外指数(ESI/CESI)**,9 个经济体(CNY、TWD、KRW、THB、IDR、INR、MYR、SGD、PHP)。
- 我原清单没有此项,你加的——我认为是好补充:归 fundamental 桶(数据面 vs 预期的动量),也可作误差修正方程的 Z 变量。sheet 名有笔误(Surpirse)。
- 【疑问/待确认】用法你的想法是?(常见:ESI 上行动量 → 货币走强的同向信号;或 ESI 极值反转)

## 8. FX Implied Yield(主块 1907 行,1990 → 2026;各国子块 2007/2011 起)

- **名实不符**:内容不是"FX 隐含收益率",而是**国债收益率**——美债 OTR 1/2/3/5/10Y(Citi RATES.TSY,周度、**降序**)+ 各国国债 1/2/5/10Y(Bloomberg `GT__` 和 `BV____ BVLI` 系列,各自独立日期列、**升序**、起点 2007-01 或 2011-04)。
- 覆盖:韩国、泰国、印尼(GTIDR 和 BVLI 两套!)、菲律宾、中国、马来西亚(表头拼写 Maylaysia)、新加坡、台湾。
- 我理解的用途:(a) 利差和期限斜率变量(在没有 12M 远期的情况下,用 1Y 国债利差近似 slope);(b) 债券市场基本面(外资债券流入的驱动)。
- **结构地狱**:同一 sheet 里主块降序+子块升序、9 套独立日期列、印尼重复两套——解析代码要非常小心,我会写专门的 parser 并对读数做交叉验证。
- 【疑问/待确认】① 你的本意是用国债利差替代远期隐含利差吗?(注意在岸国债利率 ≠ NDF 隐含利率,KRW/TWD/INR 的 basis 可以很大)② 印尼两套(GTIDR vs BVLI)保留哪套?

## 9. CDS(1321 行,2001-03-30 → 2026-07-17)

- 4 个名字:**SBIIN(印度用 SBI 代理 ✓)**、KOREA、MALAYS、INDON;口径 `SNRFOR.USD.CR14.5Y.PAR.BLENDED`——**与我们讨论的规格完全一致**(高级无担保/美元/CR14/5 年/Par rate),`BLENDED` 我理解为供应商拼接好的连续序列(解决了 2014 定义切换的接缝问题)。
- **缺口:没有菲律宾(PHILIP)**。PHP 的协整方程将缺 CDS 变量。
- 【疑问/待确认】补拉 PHILIP 5Y 吗?(Velocity 上应该有,流动性不差)

## 10. Equity Performance(847 行,2010-01-01 → 2026-07-17)

- 9 个本地股指(沪深300、NIFTY、KOSPI、台湾加权、STI、SET、KLCI、JCI、PSEi),Bloomberg,周度,**每个指数一对独立的日期+数值列**,且日期戳的星期不齐(有周日/周六戳)。
- 用途:equity-driven momentum 因子(全 9 个 ✓)+ 协整方程的股指变量(KRW/MYR/CNY)。SOX 在 Commodities sheet 里。
- 【疑问/待确认】对齐规则我打算统一为"该周最后可得值,截止周五"——OK?

## 11. Current Account(198 行,月度,2010-01 → 2026-06)

- 9 国经常账户,Bloomberg `ECOYB__N Index`。数值看量级(韩国 43.4、中国 173.1、印度 -96.8)像 **12 个月滚动、十亿美元**。
- 用途:fundamental 桶的外部平衡变量(我原清单的"可能项")。
- 【疑问/待确认】① 确认口径(滚动 12M 总额?单位 USD bn?)② 建议除以名义 GDP 转成 CA/GDP 再进模型——GDP 数据需要另补,还是你打算直接用绝对额 z-score?

## 12. ToT(1594 行,1996-01-05 → 2026-07-17)

- **Citi Terms-of-Trade(CTOT)**,**全部 9 个货币都有**(含 KRW/TWD/SGD)——比我原方案(制造业货币不用 ToT)覆盖更全,直接采用你这版。
- 数值为负居多(如 CNY -31.96、INR -47.65),说明它不是原始价格指数,更像**相对基期的变化/标准化值**。
- 【疑问/待确认】CTOT 的定义(相对什么基期?%变化还是指数点?更新频率内核是日度平滑还是周度?)——决定协整方程里它进 log 还是进水平。

## 13. Commodities(603 行周度,2014-08-01 → 2026-07-17;KOEISEU 月度 143 行)

- 招牌商品清单和我们讨论的高度一致:Brent(CO1)、黄金(XAU)、大米(RR1)、铜(LMCADY)、SGX 铁矿(SCO1)、SOX、LNG(JKL1,数值与 JKM 相符)、**韩国半导体出口物价指数(KOEISEU,月度)**——最后这个正是我推荐的"用出口价格替代 SOX 进价格层"的序列 ✓。
- **两个标注错误/疑点**:① **KO1 Comdty 标注为 "Coal",但它是马来西亚棕榈油期货**(2014-08 值 2357 与 CPO 的 MYR 价一致,煤价当时只有 ~$70)——且它是 **MYR 计价**,直接用有内生性,需换算 USD;② XA1(值 76.3,像 API2 鹿特丹煤)是**欧洲煤**,对 IDR 更贴的是纽卡斯尔/API5 或印尼 HBA。
- **历史只有 2014-08 起**(约 12 年)——比其他 sheet 短,做协整时商品版设定的样本受限。
- 【疑问/待确认】① KO1 的"Coal"标签是笔误还是你有意为之?② 要不要把 XA1 换成纽卡斯尔煤(XW1/API5)?③ SOX ticker 前有个 tab 字符(`\tSOX Index`),读数时我会清洗。

---

## 16. 跨 sheet 整体问题清单(按优先级)

| # | 问题 | 影响 | 我的建议 |
|---|---|---|---|
| 1 | **没有 12M 远期/利率** → slope differential 缺失 | Compass30 的 Z 变量少一个 | 从 Velocity 补 12M 远期点;或暂用国债 1Y 利差替代并接受 basis 瑕疵 |
| 2 | **PCA 面板不平衡**(最短列 2019 起) | 公共样本仅 7 年,协整不够 | 确认短历史货币名单;PCA 用长历史子集或分段处理 |
| 3 | **CNY/CNH 混用**(vol 用 CNY,其余 CNH) | 人民币各信号的标的不一致 | 统一为 CNH;确认 Velocity 有无 CNH vol |
| 4 | **CDS 缺菲律宾** | PHP 协整方程缺变量 | 补拉 PHILIP.SNRFOR.USD.CR14.5Y |
| 5 | **MYR 数据全面偏弱**(carry 2020 起、RR 大段缺失) | MYR 多个因子缺席 | MYR 用降级因子集,或后补在岸远期数据 |
| 6 | **无 Value 层数据**(REER/CPI 都没有) | MS 框架的 value 因子缺失 | 补 BIS REER(免费)或确认用 CTOT 兼任价值代理 |
| 7 | 商品史短(2014 起)+ KO1 标签/计价问题 + XA1 选型 | 商品版协整样本受限 | 见第 13 节 |
| 8 | 无交易成本数据(NDF 点差) | 净成本回测做不了 | 手工建一张静态点差表即可起步 |
| 9 | 报价方向、日期戳星期、升降序混杂 | 纯工程问题 | 我写统一 loader 处理,输出标准化周五面板 |
| 10 | Current Account 口径、CTOT 定义、PI 定义待确认 | 影响变量变换方式 | 你提供 Velocity 口径说明,或我按合理假设先行 |

## 17. 数据库 → 模型因子映射(我理解的最终架构)

| 因子桶 | 来源 sheet | 信号 |
|---|---|---|
| Technical | Top 30 FX Spot | 趋势、realized skewness(派生计算) |
| Carry | Carry to Vol(+ Asia FX Forward 执行层) | 1M 隐含 carry、carry/vol |
| Value/公允价值 | ToT、CDS、Equity、Current Account、Commodities、FX Implied Yield + PCA 因子 | Compass30 协整方程 → 误差修正项 |
| Sentiment | Vol & RR(RR)、FX Flows & Position(PI)、Economics Surprise | 偏度定价、仓位极值、数据意外 |
| Regime/校验 | Regime | 美元/风险/增长三开关 + PCA 校验 |

**周频、9 货币、四桶因子 + 一个公允价值锚**——数据库已经能支撑这个形态的第一版,先决问题只有 16 节的 #1–#4。
