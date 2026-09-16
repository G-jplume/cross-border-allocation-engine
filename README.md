# 跨境电商分仓占比计算引擎

基于历史出单数据，为跨境电商SKU计算各仓库（美西/美东/美南GA/美南TX）的发货占比和落货量。

## 核心算法 - 6步计算链路

```
原始数据 → ①运算SKU映射 → ②时间衰减加权 → ③季节因子
         → ④自身占比 → ⑤基准占比(层级回退) → ⑥贝叶斯收缩+落货量
```

### Step 1: 运算SKU映射
- 混用SKU映射表（VLOOKUP）将源SKU匹配到相似SKU
- 未映射的SKU取 `TEXTBEFORE(SKU, "-")` 作为运算SKU
- 多个源SKU合并为同一运算SKU参与计算

### Step 2: 时间衰减加权
- 权重公式: `w = λ^|anchor - month_seq|`（λ默认0.85）
- 锚点月序号 = 目标发货月的年×12+月
- 距离越近权重越高，近期数据影响力远大于远期

### Step 3: 季节匹配因子
- 对季节性品类（如"庭院、草坪与花园"）叠加季节增强权重
- 当数据月份与目标发货月属于同一季节窗口（±N月）时，权重×β（默认3.0）
- 月份距离用环形距离计算: `min(|m1-m2|, 12-|m1-m2|)`

### Step 4: 自身占比
- 对目标期内的数据按运算SKU汇总加权出单量
- 自身占比 = SUMIFS(加权_仓库, 运算SKU) / SUMIFS(加权合计, 运算SKU)

### Step 5: 基准占比（层级回退）
- 回退链: SPU → 一级分类 → 室内外 → 全公司
- 每层基准已包含贝叶斯收缩: `基准 = a_cat×观测 + (1-a_cat)×父层`
- 数据充足的层优先使用，数据不足自动回退

### Step 6: 最终占比 + 落货量
- 贝叶斯收缩: `最终 = a×自身 + (1-a)×基准`
- 收缩权重: `a = n/(n+k)`（k默认6），新品（历史月数≤2）强制a=0
- 占比归一化后乘以需求量得到各仓落货量

## 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| λ (lambda) | 0.85 | 时间衰减因子，越小衰减越快 |
| k | 6 | 贝叶斯收缩参数，越大越倾向基准 |
| a_min | 0.0 | 收缩权重下限 |
| a_max | 0.9 | 收缩权重上限 |
| 新品阈值 | 2 | 历史月数≤此值时a=0 |
| α (trend) | 0.3 | 趋势调整因子 |
| 调整上限 | 0.05 | 单仓趋势调整上限(±5%) |
| β (seasonal) | 3.0 | 季节增强因子 |

## 项目结构

```
.
├── allocation_engine.py      # 计算引擎核心（6步链路）
├── engine_data/              # 数据目录（需自行填充）
│   ├── params.json           # 计算参数
│   ├── sheet2_raw.csv        # 原始出单数据
│   ├── sheet4_benchmarks.json # 基准表
│   ├── sheet5_results.csv    # Excel计算结果（用于验证）
│   └── mix_sku_mapping.json  # 混用SKU映射表
├── requirements.txt
└── README.md
```

## 使用方法

```python
from allocation_engine import AllocationEngine

engine = AllocationEngine(data_dir="engine_data")
engine.load_data()

engine.step1_sku_mapping()   # 运算SKU映射
engine.step2_decay_weight()  # 时间衰减加权
engine.step3_seasonal_factor() # 季节因子
engine.step4_self_ratio()    # 自身占比
engine.step5_benchmark()     # 基准占比
df_results = engine.step6_final_ratio(demand_qty=3000)  # 最终占比+落货量

# 验证与Excel结果一致性
engine.verify(df_results)
```

## 验证结果

727个SKU全部通过验证，7个核心指标max_diff=0.000000：

| 指标 | max_diff | 状态 |
|------|----------|------|
| 自身_美西 | 0.000000 | PASS |
| 基准_美西 | 0.000000 | PASS |
| 最终_美西 | 0.000000 | PASS |
| 最终_美东 | 0.000000 | PASS |
| 最终_美南GA | 0.000000 | PASS |
| 最终_美南TX | 0.000000 | PASS |
| 收缩权重 | 0.000000 | PASS |

## 技术栈

- Python 3.x
- pandas / numpy（向量化计算）
- openpyxl / pywin32（Excel数据读取）

## 后续规划

- [x] P1: 计算引擎核心（6步链路纯Python实现）
- [ ] P2: Streamlit UI（上传+参数+结果看板）
- [ ] P3: 可视化（品类分析、季节诊断、趋势图）
- [ ] P4: 导出Excel + 部署
