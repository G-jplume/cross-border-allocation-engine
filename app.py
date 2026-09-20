"""
分仓占比计算引擎 - Streamlit Web App

运行方式:
    streamlit run app.py

页面结构:
  侧边栏  参数面板（分组 + 生效状态标注 + 自检）
  主区域  1.上传数据 → 2.计算参数确认 → 3.结果 → 4.占比微调 → 5.导出
"""
import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import io
import os

from allocation_engine import AllocationEngine, WAREHOUSES

st.set_page_config(
    page_title="分仓占比计算引擎",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ==========================================================
# session_state 初始化
# ==========================================================
for k, v in [("df_raw", None), ("df_results", None), ("engine", None),
             ("agg_results", {}), ("checks", []), ("edited_ratios", None),
             ("reduction_summary", None)]:
    if k not in st.session_state:
        st.session_state[k] = v

WH_COLORS = {"美西": "#4e79a7", "美东": "#f28e2b", "美南GA": "#e15759", "美南TX": "#76b7b2"}
FONT = dict(family="Microsoft YaHei, sans-serif")

st.title("📦 分仓占比计算引擎")
st.markdown(
    "上传历史出单数据 → 调节参数 → 计算各仓库发货占比 → 导出结果\n\n"
    "✅ 四仓占比合计恒为 100% | ✅ 最大余额法保证落货量整数精确 | ✅ 自动体检防静默失效"
)


def apply_warehouse_reduction(engine, df_results, monthly_threshold, ratio_threshold):
    """减仓优化：对运算SKU×仓点判断是否减仓，被减的占比等比分配给其他仓，归一化到100%。
    仅修改SKU层的「调整后_」列，上层聚合不受影响。
    返回 (df_results_modified, reduction_summary_dict)
    """
    df_raw = engine.df_raw
    latest_month_seq = int(df_raw["月份序号_py"].max())

    sku_stats = {}
    for sku, group in df_raw.groupby("运算SKU_py"):
        wh_totals = {wh: int(group[wh].sum()) for wh in WAREHOUSES}
        grand_total = sum(wh_totals.values())
        if grand_total == 0:
            continue
        has_orders = group[WAREHOUSES].sum(axis=1) > 0
        if not has_orders.any():
            continue
        first_month_seq = int(group.loc[has_orders, "月份序号_py"].min())
        age_months = latest_month_seq - first_month_seq + 1
        for wh in WAREHOUSES:
            wh_total = wh_totals[wh]
            if wh_total == 0:
                continue
            monthly_avg = wh_total / age_months
            ratio = wh_total / grand_total
            if monthly_avg < monthly_threshold and ratio < ratio_threshold:
                if sku not in sku_stats:
                    sku_stats[sku] = {"reduced": set(), "age": age_months}
                sku_stats[sku]["reduced"].add(wh)

    df = df_results.copy()
    reduction_flags = {}

    for sku, stats in sku_stats.items():
        reduced_whs = stats["reduced"]
        if len(reduced_whs) >= len(WAREHOUSES):
            continue
        mask = df["SKU"].astype(str) == str(sku)
        if not mask.any():
            continue
        adj_cols = {wh: f"调整后_{wh}" for wh in WAREHOUSES}
        current = {wh: float(df.loc[mask, adj_cols[wh]].iloc[0]) for wh in WAREHOUSES}
        reduced_share = sum(current[wh] for wh in reduced_whs)
        for wh in reduced_whs:
            df.loc[mask, adj_cols[wh]] = 0.0
        remaining_whs = [wh for wh in WAREHOUSES if wh not in reduced_whs]
        remaining_total = sum(current[wh] for wh in remaining_whs)
        if remaining_total > 0:
            for wh in remaining_whs:
                new_val = current[wh] + reduced_share * (current[wh] / remaining_total)
                df.loc[mask, adj_cols[wh]] = new_val
        else:
            for wh in remaining_whs:
                df.loc[mask, adj_cols[wh]] = reduced_share / len(remaining_whs)
        adj_total = sum(float(df.loc[mask, adj_cols[wh]].iloc[0]) for wh in WAREHOUSES)
        if adj_total > 0:
            for wh in WAREHOUSES:
                df.loc[mask, adj_cols[wh]] = float(df.loc[mask, adj_cols[wh]].iloc[0]) / adj_total
        if "调整后合计" in df.columns:
            df.loc[mask, "调整后合计"] = 1.0
        reduction_flags[sku] = list(reduced_whs)

    df["是否减仓"] = ""
    for sku, whs in reduction_flags.items():
        mask = df["SKU"].astype(str) == str(sku)
        df.loc[mask, "是否减仓"] = f"是-{','.join(whs)}"

    n_skus = len(reduction_flags)
    n_slots = sum(len(whs) for whs in reduction_flags.values())
    summary = {"n_reduced_skus": n_skus, "n_reduced_slots": n_slots,
               "reduction_flags": reduction_flags}
    return df, summary


def compute_aggregate_ratios(df_sku):
    """按 一级分类 / 室内外 / SPU / 全公司 聚合分仓占比。"""
    wh_cols = {}
    for wh in WAREHOUSES:
        for cand in (f"调整后_{wh}", f"最终_{wh}"):
            if cand in df_sku.columns:
                wh_cols[wh] = cand
                break
    agg = {}
    for level in ["一级分类", "室内外", "SPU"]:
        if level not in df_sku.columns:
            continue
        g = df_sku.groupby(level).agg(
            **{wh: (wh_cols[wh], "mean") for wh in WAREHOUSES}
        ).reset_index()
        g.columns = [level] + [f"占比_{wh}" for wh in WAREHOUSES]
        tot = sum(g[f"占比_{wh}"] for wh in WAREHOUSES)
        for wh in WAREHOUSES:
            g[f"占比_{wh}"] = g[f"占比_{wh}"] / tot
        g.insert(1, "SKU数", df_sku.groupby(level).size().values)
        agg[level] = g

    overall = {wh: df_sku[wh_cols[wh]].mean() for wh in WAREHOUSES}
    tot = sum(overall.values())
    agg["全公司"] = pd.DataFrame([{
        "层级": "全公司", "SKU数": len(df_sku),
        **{f"占比_{wh}": overall[wh] / tot for wh in WAREHOUSES}
    }])
    return agg


# ==========================================================
# 侧边栏：参数面板
# ==========================================================
st.sidebar.title("计算参数")


def eff(label, desc, required=True):
    """参数生效状态标注。"""
    tag = "🟢 生效" if required else "⚪ 视条件"
    st.sidebar.caption(f"{tag} · {label}：{desc}")


# ---------- 目标发货月份 ----------
st.sidebar.subheader("① 目标发货月份")
st.sidebar.caption("支持跨年（如2026年12月→2027年3月）。")

_ym_options = {f"{y}年{m}月": (y, m) for y in range(2024, 2031) for m in range(1, 13)}
_ym_labels = list(_ym_options.keys())
col_ys, col_ye = st.sidebar.columns(2)
with col_ys:
    _start_label = st.selectbox("起始年月", _ym_labels,
                                index=_ym_labels.index("2027年1月"), key="tsym")
with col_ye:
    _end_label = st.selectbox("结束年月", _ym_labels,
                              index=_ym_labels.index("2027年3月"), key="teym")
target_start_year, target_start_month = _ym_options[_start_label]
target_end_year, target_end_month = _ym_options[_end_label]

# 计算月份序号区间
target_start_seq = int(target_start_year) * 12 + int(target_start_month)
target_end_seq = int(target_end_year) * 12 + int(target_end_month)

if target_start_seq > target_end_seq:
    st.sidebar.warning("起始年月晚于结束年月，已自动交换。")
    target_start_seq, target_end_seq = target_end_seq, target_start_seq
    target_start_year, target_end_year = target_end_year, target_start_year
    target_start_month, target_end_month = target_end_month, target_start_month

# 锚点 = 目标期中间月份序号（支持跨年）
anchor = (target_start_seq + target_end_seq) // 2
anchor_year = anchor // 12
anchor_month = (anchor - 1) % 12 + 1

# 目标期月份列表（1-12，去重）
target_months_set = set()
s = target_start_seq
while s <= target_end_seq:
    target_months_set.add((s - 1) % 12 + 1)
    s += 1

eff("目标发货月份",
    f"锚点={anchor_year}年{anchor_month}月，"
    f"目标期：{target_start_year}年{target_start_month}月→{target_end_year}年{target_end_month}月"
    f"（{len(target_months_set)}个月份）。强季节只看目标期同月，弱季节放开全部。")

# ---------- 时间衰减 ----------
st.sidebar.subheader("② 时间衰减加权")
st.sidebar.caption("强季节只看同月，弱季节看全部。同月跨年用λ_same，非同月用λ。")

lambda_val = st.sidebar.slider(
    "非同月衰减速度 λ", 0.50, 1.00, 0.85, 0.01,
    help="弱季节非同月数据衰减率。0.85=每月衰减15%。"
)
lambda_same = st.sidebar.slider(
    "同月跨年衰减速度 λ_same", 0.80, 1.00, 0.95, 0.01,
    help="同月跨年衰减率。0.95=去年保留95%。"
)
st.sidebar.caption(
    f"同月：去年{lambda_same:.0%}，前年{lambda_same**2:.0%} | 非同月：1月前{lambda_val:.0%}，6月前{lambda_val**6:.0%}"
)

# ---------- 季节匹配 ----------
st.sidebar.subheader("③ 季节匹配因子")
st.sidebar.caption("目标月前N个月历史数据权重放大β倍，与趋势因子不冲突。")

seasonal_on = st.sidebar.checkbox("启用季节因子", value=True)

if seasonal_on:
    beta = st.sidebar.slider(
        "强季节增强倍数 β", 1.0, 5.0, 3.0, 0.1,
        help="强季节品类同月数据权重放大倍数。"
    )
    beta_weak = st.sidebar.slider(
        "弱季节增强倍数 β_weak", 1.0, 3.0, 1.5, 0.1,
        help="弱季节品类同月数据权重放大倍数。"
    )
    st.sidebar.caption(f"强β={beta:.1f}，弱β={beta_weak:.1f}，非同月×1.0")
    seasonal_window = st.sidebar.slider(
        "季节窗口范围 N", 1, 3, 1,
        help="目标月前N个月权重放大（环形距离）。N=1→命中前1月，N=3→命中前3月。"
    )
else:
    beta, beta_weak, seasonal_window = 1.0, 1.0, 1

# 季节适用品类 —— 从上传数据动态识别，默认勾选庭院类
if seasonal_on and st.session_state.df_raw is not None and "一级分类" in st.session_state.df_raw.columns:
    _cats_all = sorted(
        st.session_state.df_raw["一级分类"].dropna().astype(str).unique().tolist()
    )
    _cats_all = [c for c in _cats_all if c.strip()]
    _default_cats = [c for c in _cats_all if c.strip() in ("庭院、草坪与花园", "庭院")]
    seasonal_cats = _default_cats
else:
    seasonal_cats = []

eff("季节匹配因子", "与衰减因子相乘，放大同季数据权重。")

# ---------- 趋势因子 ----------
st.sidebar.subheader("④ 趋势因子 α")
st.sidebar.caption("去年vs前年同期占比差，叠加到最终占比。")

alpha_trend = st.sidebar.slider(
    "趋势因子 α", 0.0, 1.0, 0.3, 0.05,
    help="趋势差×α叠加到最终占比。0=关闭，0.3=默认，1.0=激进。"
)
trend_cap = st.sidebar.slider(
    "单仓调整上限", 0.0, 0.20, 0.05, 0.01,
    help="单仓趋势调整幅度上限，防异常月份带偏。"
)
if alpha_trend == 0:
    st.sidebar.caption("⚪ α=0，趋势调整已关闭")
else:
    st.sidebar.caption(f"🟢 去年vs前年同期，单仓最多±{trend_cap:.0%}")

norm_method = "proportional"

# ---------- 新品与基准 ----------
st.sidebar.subheader("⑤ 新品与基准")
st.sidebar.caption("新品判定与自身/基准混合比例。基准自动计算。")

new_product_threshold = st.sidebar.slider(
    "新品阈值（月）", 1, 12, 2,
    help="历史出单月数≤此值视为新品，完全用基准占比。"
)
new_product_min_orders = st.sidebar.slider(
    "新品订单量门槛", 1, 100, 10,
    help="目标期总单量<此值也视为新品，避免低单量偶然性。"
)
st.sidebar.caption(
    f"新品：历史月数≤{new_product_threshold} 或 目标期单量<{new_product_min_orders}"
)

k_val = st.sidebar.slider(
    "收缩强度 k", 1, 20, 6,
    help="自身权重=n/(n+k)，n=目标期月数。k越大越信基准，越小越信自身。"
)
_est_n = target_end_month - target_start_month + 1
st.sidebar.caption(
    f"k={k_val}：目标期约{_est_n}个月 → "
    f"自身权重≈{_est_n}/({_est_n}+{k_val})={_est_n/(_est_n+k_val):.0%}，"
    f"基准≈{k_val/(_est_n+k_val):.0%}"
)

col_a1, col_a2 = st.sidebar.columns(2)
with col_a1:
    a_min = st.slider("权重下限", 0.0, 0.5, 0.0, 0.05,
                      help="自身数据最低权重。0=新品完全用基准。")
with col_a2:
    a_max = st.slider("权重上限", 0.5, 1.0, 0.9, 0.05,
                      help="自身数据最高权重。0.9=最多90%用自身。")

k_cat = st.sidebar.slider(
    "品类层最小等效观测数", 3, 50, 12,
    help="基准贝叶斯收缩参数。子层权重=n/(n+k_cat)。k_cat越大越保守，数据少时更快拉向父层。"
)
st.sidebar.caption(
    f"k_cat={int(k_cat)}：n={int(k_cat)}时各占50%，n大偏向子层，n小偏向父层。"
)

# ==========================================================
# 主区域 1：上传数据
# ==========================================================
st.subheader("1. 上传数据")

col_upload, col_sample, col_tpl = st.columns([3, 1, 1])
REQUIRED_COLS = ["源SKU", "SPU", "室内外", "一级分类", "年", "月",
                 "美西", "美东", "美南GA", "美南TX"]

with col_upload:
    uploaded_file = st.file_uploader(
        "上传原始出单数据 (CSV 或 Excel)", type=["csv", "xlsx", "xls"],
        help="必须包含列：" + "、".join(REQUIRED_COLS)
    )
with col_sample:
    use_sample = st.button("使用示例数据", help="用内置数据演示完整流程")
with col_tpl:
    tpl_csv = pd.DataFrame(columns=REQUIRED_COLS).to_csv(index=False).encode("utf-8-sig")
    st.download_button("下载模板", data=tpl_csv, file_name="原始数据模板.csv", mime="text/csv")

if uploaded_file is not None:
    try:
        if uploaded_file.name.endswith(".csv"):
            try:
                _df = pd.read_csv(uploaded_file, encoding="utf-8-sig",
                                  dtype={"源SKU": str, "SPU": str, "室内外": str, "一级分类": str})
            except UnicodeDecodeError:
                _df = pd.read_csv(uploaded_file, encoding="gbk",
                                  dtype={"源SKU": str, "SPU": str, "室内外": str, "一级分类": str})
        else:
            _df = pd.read_excel(uploaded_file,
                                dtype={"源SKU": str, "SPU": str, "室内外": str, "一级分类": str})
        _df.columns = [str(c).strip() for c in _df.columns]

        _missing = [c for c in REQUIRED_COLS if c not in _df.columns]
        if _missing:
            st.error(f"缺少必需列：{'、'.join(_missing)}")
            st.caption(f"当前列名：{list(_df.columns)}")
        else:
            st.session_state.df_raw = _df
            st.session_state.pop("seasonal_cats_ms", None)
            st.success(f"上传成功！共 {len(_df)} 行 × {len(_df.columns)} 列")
    except Exception as e:
        st.error(f"读取失败: {e}")

elif use_sample:
    _sample = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "engine_data", "sheet2_raw.csv")
    if os.path.exists(_sample):
        _df = pd.read_csv(_sample, encoding="utf-8-sig",
                          dtype={"源SKU": str, "SPU": str, "室内外": str, "一级分类": str})
        st.session_state.df_raw = _df
        st.session_state.pop("seasonal_cats_ms", None)
        st.success(f"已加载示例数据！共 {len(_df)} 行 × {len(_df.columns)} 列")
    else:
        st.warning("示例数据文件不存在，请上传数据。")

if st.session_state.df_raw is not None:
    _d = st.session_state.df_raw
    with st.expander("数据体检 + 预览", expanded=False):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("总行数", f"{len(_d):,}")
        c2.metric("SKU 数", f"{_d['源SKU'].nunique():,}")
        c3.metric("品类数", f"{_d['一级分类'].nunique()}")
        _ym = f"{int(_d['年'].min())}-{int(_d['月'].min()):02d} ~ {int(_d['年'].max())}-{int(_d['月'].max()):02d}"
        c4.metric("时间跨度", _ym)

        _dup = _d.duplicated(subset=["源SKU", "年", "月"], keep=False).sum()
        if _dup:
            st.warning(f"检测到 {_dup} 行「同SKU+同年+同月」重复记录，会影响计算结果，建议先去重")
        else:
            st.caption("✓ 无「同SKU+同年+同月」重复记录")

        _zero = int((_d[["美西", "美东", "美南GA", "美南TX"]].sum(axis=1) == 0).sum())
        if _zero:
            st.caption(f"注意：{_zero} 行四仓发货量全为 0，这些行不参与加权")
        st.dataframe(_d.head(10), use_container_width=True)

with st.expander("混用SKU映射（可选）", expanded=False):
    st.caption("不上传也能算：未映射的 SKU 自动取「-」前部分合并")
    mix_file = st.file_uploader("上传映射表（源SKU / 相似SKU）",
                                type=["csv", "xlsx", "xls"], key="mix_upload")
    if mix_file is not None:
        try:
            if mix_file.name.endswith(".csv"):
                try:
                    mix_df = pd.read_csv(mix_file, encoding="utf-8-sig", dtype=str)
                except UnicodeDecodeError:
                    mix_df = pd.read_csv(mix_file, encoding="gbk", dtype=str)
            else:
                mix_df = pd.read_excel(mix_file, dtype=str)
            mix_df.columns = [str(c).strip() for c in mix_df.columns]
            if "源SKU" in mix_df.columns and "相似SKU" in mix_df.columns:
                st.session_state.mix_mapping = dict(zip(mix_df["源SKU"], mix_df["相似SKU"]))
                st.success(f"已加载 {len(st.session_state.mix_mapping)} 条映射")
            else:
                st.error("列名需为 源SKU / 相似SKU")
        except Exception as e:
            st.error(f"映射表读取失败: {e}")


# ==========================================================
# 主区域 2：开始计算
# ==========================================================
st.subheader("2. 开始计算")

if st.session_state.df_raw is None:
    st.info("请先上传数据，或点击「使用示例数据」")
else:
    with st.container(border=True):
        st.markdown("""
        <div style="
            background: linear-gradient(135deg, #e6f4ff 0%, #f0f7ff 100%);
            border-left: 4px solid #4096ff;
            border-radius: 0 8px 8px 0;
            padding: 10px 14px;
            margin-bottom: 4px;
        ">
            <span style="font-size:16px; font-weight:700; color:#003eb3;">⚙️ 计算选项</span>
            <span style="font-size:12px; color:#8c8c8c; margin-left:8px;">（选配后点击下方按钮）</span>
        </div>
        """, unsafe_allow_html=True)

        if seasonal_on and "一级分类" in st.session_state.df_raw.columns:
            _cats_all = sorted(
                st.session_state.df_raw["一级分类"].dropna().astype(str).unique().tolist()
            )
            _cats_all = [c for c in _cats_all if c.strip()]
            _default_cats = [c for c in _cats_all if c.strip() in ("庭院、草坪与花园", "庭院")]
            st.markdown("**🌿 季节适用品类**")
            st.caption("勾选需要季节性增强的品类（如庭院类），不勾选则不应用季节因子")
            seasonal_cats = st.multiselect(
                "选择品类",
                options=_cats_all,
                default=_default_cats,
                key="seasonal_cats_main",
                label_visibility="collapsed",
            )
        else:
            seasonal_cats = []

        st.markdown("---")
        st.markdown("**📦 减仓优化**")
        st.caption("月均<3单且占比<5%的仓点按比例均分到其他仓")
        reduction_on = st.checkbox("启用减仓优化", value=False)

    if st.button("🚀 开始计算", type="primary", use_container_width=True):
        with st.spinner("计算中..."):
            try:
                engine = AllocationEngine()
                engine.df_raw = st.session_state.df_raw.copy()

                if "mix_mapping" in st.session_state:
                    engine.mix_mapping = st.session_state.mix_mapping

                # 锚点 = 目标期中间月份序号（已支持跨年）
                engine.params["anchor"] = anchor
                engine.params["lambda"] = lambda_val
                engine.params["lambda_same_month"] = lambda_same
                engine.params["k"] = k_val
                engine.params["a_min"] = a_min
                engine.params["a_max"] = a_max
                engine.params["alpha_trend"] = alpha_trend
                engine.params["trend_cap"] = trend_cap
                engine.params["norm_method"] = norm_method
                engine.params["seasonal_switch"] = 1 if (seasonal_on and seasonal_cats) else 0
                engine.params["seasonal_beta"] = beta
                engine.params["seasonal_beta_weak"] = beta_weak
                engine.params["seasonal_window"] = seasonal_window
                engine.params["new_product_min_orders"] = new_product_min_orders
                engine.new_product_threshold = new_product_threshold
                engine.k_cat = float(k_cat)
                engine.seasonal_categories = seasonal_cats

                # 跨年目标期：用月份序号区间判定"在目标期"
                engine.df_raw["在目标期"] = engine.df_raw.apply(
                    lambda row: 1 if target_start_seq <= int(row["年"]) * 12 + int(row["月"]) <= target_end_seq else 0,
                    axis=1
                )

                engine.step1_sku_mapping()
                engine.step2_decay_weight()
                engine.step3_seasonal_factor()
                engine.step4_self_ratio()
                engine.step5_benchmark()
                df_results = engine.step6_final_ratio(demand_qty=1)

                drop_cols = [c for c in df_results.columns if c.startswith("落货量")]
                df_results = df_results.drop(columns=drop_cols)

                # 上层聚合用减仓前原始数据
                st.session_state.agg_results = compute_aggregate_ratios(df_results)

                # 减仓优化：仅影响SKU层
                if reduction_on:
                    df_results, reduction_summary = apply_warehouse_reduction(
                        engine, df_results, 3.0, 0.05
                    )
                    st.session_state.reduction_summary = reduction_summary
                else:
                    df_results["是否减仓"] = ""
                    st.session_state.reduction_summary = None

                st.session_state.df_results = df_results
                st.session_state.engine = engine
                st.session_state.checks = engine.self_check(df_results)
                st.session_state.edited_ratios = None
                n_msg = f"计算完成！共 {len(df_results)} 个运算SKU"
                if reduction_on and reduction_summary["n_reduced_skus"] > 0:
                    n_msg += f"（减仓: {reduction_summary['n_reduced_skus']}个SKU, {reduction_summary['n_reduced_slots']}个仓位）"
                st.success(n_msg)
            except Exception as e:
                st.error(f"计算失败: {e}")
                st.exception(e)


# ==========================================================
# 主区域 3：结果展示
# ==========================================================
if st.session_state.df_results is not None:
    df_r = st.session_state.df_results
    ADJ = [f"调整后_{wh}" for wh in WAREHOUSES]
    FIN = [f"最终_{wh}" for wh in WAREHOUSES]

    st.subheader("3. 计算结果")

    # --- 自检 ---
    checks = st.session_state.get("checks", [])
    if checks:
        bad = [c for c in checks if not c[1]]
        with st.expander("🔍 自动体检", expanded=bool(bad)):
            for name, ok, msg in checks:
                (st.success if ok else st.warning)(f"{'✓' if ok else '⚠'} **{name}** — {msg}")

    # --- 减仓摘要 ---
    reduction_summary = st.session_state.get("reduction_summary")
    if reduction_on and reduction_summary and reduction_summary["n_reduced_skus"] > 0:
        with st.expander(f"📦 减仓优化（{reduction_summary['n_reduced_skus']}个SKU, {reduction_summary['n_reduced_slots']}个仓位）", expanded=False):
            st.caption("仅影响SKU层，上层聚合用减仓前原始数据。")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("运算SKU 总数", f"{len(df_r):,}")
    for col, wh in zip([col2, col3, col4], ["美西", "美东", "美南GA"]):
        col.metric(f"{wh} 平均占比", f"{df_r[ADJ[WAREHOUSES.index(wh)]].mean():.1%}")

    # --- SKU 级表格 ---
    st.markdown("**SKU 级分仓占比**")
    col_f1, col_f2 = st.columns([1, 3])
    with col_f1:
        sku_filter = st.selectbox("查看单个SKU", ["全部"] + sorted(df_r["SKU"].astype(str)))
    with col_f2:
        cat_filter = st.multiselect("按品类筛选", sorted(df_r["一级分类"].dropna().unique()))

    df_show = df_r.copy()
    if sku_filter != "全部":
        df_show = df_show[df_show["SKU"].astype(str) == sku_filter]
    if cat_filter:
        df_show = df_show[df_show["一级分类"].isin(cat_filter)]

    display_cols = ["SKU", "SPU", "一级分类", "室内外", "历史月数", "目标期月数",
                    "收缩权重_a", "基准层级", "基准观测数",
                    "自身_美西", "自身_美东", "自身_美南GA", "自身_美南TX",
                    "基准_美西", "基准_美东", "基准_美南GA", "基准_美南TX",
                    "最终_美西", "最终_美东", "最终_美南GA", "最终_美南TX",
                    "趋势差_美西", "趋势差_美东", "趋势差_美南GA", "趋势差_美南TX",
                    "调整后_美西", "调整后_美东", "调整后_美南GA", "调整后_美南TX",
                    "是否减仓"]
    display_cols = [c for c in display_cols if c in df_show.columns]
    pct_cols = [c for c in display_cols if c.split("_")[0] in
                ("自身", "基准", "最终", "调整后", "趋势差")]

    st.dataframe(
        df_show[display_cols].style.format({
            "收缩权重_a": "{:.3f}",
            **{c: "{:+.2%}" if c.startswith("趋势差") else "{:.2%}" for c in pct_cols},
        }),
        use_container_width=True, height=400
    )

    # --- 聚合层级 ---
    agg_results = st.session_state.get("agg_results", {})
    if agg_results:
        st.markdown("**聚合层级分仓占比**")
        tabs = st.tabs(["一级分类", "室内外", "SPU", "全公司"])
        for i, level in enumerate(["一级分类", "室内外", "SPU", "全公司"]):
            if level in agg_results:
                tabs[i].dataframe(
                    agg_results[level].style.format(
                        {f"占比_{wh}": "{:.2%}" for wh in WAREHOUSES}
                    ), use_container_width=True
                )


    # ======================================================
    # 主区域 4：占比微调（锁定合计 100%）
    # ======================================================
    st.subheader("4. 占比微调")
    st.caption("改任意一仓，其余三仓按原比例自动补足，合计始终100%。")
    st.info("🔒 四仓合计=100%：计算归一化→趋势归一化→微调补足→自检偏差<1e-9")

    adj_sku = st.selectbox("选择要微调的SKU", sorted(df_r["SKU"].astype(str)),
                           key="tune_sku")
    if adj_sku:
        _row = df_r[df_r["SKU"].astype(str) == adj_sku].iloc[0]
        base = {wh: float(_row[f"调整后_{wh}"]) for wh in WAREHOUSES}

        c_left, c_right = st.columns([1, 1])

        with c_left:
            st.markdown("**编辑四仓占比**")
            new_vals = {}
            for wh in WAREHOUSES:
                new_vals[wh] = st.number_input(
                    f"{wh}", min_value=0.0, max_value=1.0,
                    value=round(base[wh], 4), step=0.01, format="%.4f",
                    key=f"tune_{adj_sku}_{wh}",
                )
            tot_new = sum(new_vals.values())
            if abs(tot_new - 1.0) < 1e-9:
                st.success(f"合计 = {tot_new:.4%} ✓")
            else:
                st.error(f"合计 = {tot_new:.4%} ✗ 需要等于 100%")

        with c_right:
            st.markdown("**一键锁定到 100%**")
            st.caption("以改动最大的仓为准，其余三仓按原比例补足。")
            if st.button("按改动自动补足到 100%", use_container_width=True):
                deltas = {wh: abs(new_vals[wh] - base[wh]) for wh in WAREHOUSES}
                pivot = max(deltas, key=deltas.get)
                if deltas[pivot] < 1e-9:
                    st.info("未检测到改动，无需补足。")
                else:
                    target = min(max(new_vals[pivot], 0.0), 1.0)
                    others = [w for w in WAREHOUSES if w != pivot]
                    other_sum = sum(base[w] for w in others)
                    fixed = {pivot: target}
                    if other_sum > 0:
                        for w in others:
                            fixed[w] = (1 - target) * base[w] / other_sum
                    else:
                        for w in others:
                            fixed[w] = (1 - target) / len(others)
                    st.session_state.edited_ratios = fixed
                    st.rerun()

        # 应用锁定结果
        final_use = st.session_state.get("edited_ratios") or base
        if st.session_state.get("edited_ratios"):
            st.info("已应用自动补足结果，下方落货量按修正后的占比计算。")

        # 落货量
        st.markdown("**按占比生成落货量**")
        c_q1, c_q2 = st.columns([1, 2])
        with c_q1:
            qty = st.number_input("该SKU发运批量（件）", min_value=1, value=1000,
                                  step=100, key=f"qty_{adj_sku}")
        alloc = AllocationEngine._allocate_integer(final_use, int(qty))
        c_q2.dataframe(pd.DataFrame([{
            "仓库": wh,
            "占比": f"{final_use[wh]:.2%}",
            "落货量(件)": alloc[wh],
        } for wh in WAREHOUSES]), use_container_width=True, hide_index=True)

        _sum_alloc = sum(alloc.values())
        if _sum_alloc == qty:
            st.success(f"落货量合计 = {_sum_alloc:,} 件 = 批量 ✓（最大余额法保证整数精确）")
        else:
            st.error(f"落货量合计 = {_sum_alloc:,} 件 ≠ 批量 {qty:,}")

        exp_df = pd.DataFrame([{
            "SKU": adj_sku, "仓库": wh,
            "占比": final_use[wh], "落货量": alloc[wh],
        } for wh in WAREHOUSES])
        st.download_button(
            "下载该SKU调整结果 CSV",
            data=exp_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"分仓占比_{adj_sku}.csv", mime="text/csv"
        )

    # ======================================================
    # 可视化
    # ======================================================
    st.subheader("5. 可视化")

    chart_sku = st.selectbox("选择SKU查看分仓占比", sorted(df_r["SKU"].astype(str)),
                             key="chart_sku")
    if chart_sku:
        row = df_r[df_r["SKU"].astype(str) == chart_sku].iloc[0]
        pie_data = pd.DataFrame({
            "仓库": WAREHOUSES,
            "占比": [row[c] for c in ADJ],
        })
        c_p1, c_p2 = st.columns(2)
        with c_p1:
            fig = px.pie(pie_data, values="占比", names="仓库",
                         title=f"{chart_sku} 分仓占比", color="仓库",
                         color_discrete_map=WH_COLORS)
            fig.update_layout(font=FONT)
            st.plotly_chart(fig, use_container_width=True)
        with c_p2:
            fig = px.bar(pie_data, x="仓库", y="占比", title=f"{chart_sku} 各仓占比",
                         color="仓库", color_discrete_map=WH_COLORS, text="占比")
            fig.update_layout(font=FONT, yaxis=dict(tickformat=".1%"), showlegend=False)
            fig.update_traces(texttemplate="%{text:.1%}")
            st.plotly_chart(fig, use_container_width=True)

    st.markdown("**全SKU分仓占比堆叠图**（前50个SKU，四仓合计恒为100%）")
    df_top = df_r.head(50)
    fig_stack = go.Figure()
    for wh in WAREHOUSES:
        fig_stack.add_trace(go.Bar(
            x=df_top["SKU"].astype(str), y=df_top[ADJ[WAREHOUSES.index(wh)]],
            name=wh, marker_color=WH_COLORS[wh]
        ))
    fig_stack.update_layout(barmode="stack", title="各SKU分仓占比（前50）",
                            xaxis_title="SKU", yaxis_title="占比",
                            yaxis=dict(tickformat=".0%", range=[0, 1]),
                            font=FONT, height=400)
    st.plotly_chart(fig_stack, use_container_width=True)

    # ======================================================
    # 导出
    # ======================================================
    st.subheader("6. 导出结果")
    c_e1, c_e2, c_e3 = st.columns(3)
    with c_e1:
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as w:
            df_r.to_excel(w, index=False, sheet_name="SKU级占比")
            for lv in ["一级分类", "室内外", "SPU", "全公司"]:
                if lv in agg_results:
                    agg_results[lv].to_excel(w, index=False, sheet_name=lv)
        st.download_button("下载 Excel（含聚合）", data=out.getvalue(),
                           file_name="分仓占比结果.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)
    with c_e2:
        st.download_button("下载 CSV（SKU级）",
                           data=df_r.to_csv(index=False).encode("utf-8-sig"),
                           file_name="分仓占比结果_SKU级.csv", mime="text/csv",
                           use_container_width=True)
    with c_e3:
        if "一级分类" in agg_results:
            st.download_button("下载 CSV（一级分类）",
                               data=agg_results["一级分类"].to_csv(index=False).encode("utf-8-sig"),
                               file_name="分仓占比结果_一级分类.csv", mime="text/csv",
                               use_container_width=True)


# ==========================================================
# 页脚：计算链路说明 + 常见问题
# ==========================================================
st.markdown("---")
with st.expander("📖 计算链路说明（7步）", expanded=False):
    st.markdown("""
| 步骤 | 做什么 | 关键参数 |
|---|---|---|
| ① 运算SKU映射 | 混用SKU映射表把源SKU合并到相似SKU；未映射的取「-」前部分 | — |
| ② 时间衰减加权 | **只对目标期月份**赋权，权重 = λ^(锚点−月份) | λ |
| ③ 季节匹配因子 | 目标月之前 N 个月的历史数据权重 × β（环形距离，目标月本身不计入） | β、N、适用品类 |
| ④ 自身占比 | 加权出单量 ÷ 加权合计，逐仓计算 | — |
| ⑤ 基准占比 | 层级回退 SPU→一级分类→室内外→全公司，每层做贝叶斯收缩 | 品类层最小等效观测数 |
| ⑥ 最终占比 | 自身与基准加权混合，再叠加趋势调整，最后归一化 | k、权重上下限、α、调整上限 |
| ⑦ 落货量 | 占比 × 批量，最大余额法保证整数合计精确等于批量 | — |

**参数生效层级一览**

| 参数 | 作用层 | 影响范围 | 说明 |
|---|---|---|---|
| λ 衰减速度 | 加权层（步骤②） | 全局，所有品类 | 控制目标期内近期 vs 远期数据权重 |
| β 季节增强倍数 | 加权层（步骤③） | 仅适用品类 | 放大同季历史数据权重 |
| α 趋势因子 | 最终占比层（步骤⑥） | 全局，有趋势数据的SKU | 叠加年度趋势差到最终占比 |
| k 收缩强度 | SKU层混合（步骤⑥） | 全局 | 自身历史 vs 基准的混合比例 |
| k_cat 品类层观测数 | 基准层收缩（步骤⑤） | 全局 | 子层基准 vs 父层基准的混合比例 |
""")

with st.expander("❓ 常见问题（FAQ）", expanded=False):
    st.markdown("""
**Q1：季节匹配因子是默认按「庭院、草坪与花园/庭院」来，还是基于上传数据判断？**

混合模式。上传数据后，系统从你的「一级分类」列提取所有唯一品类作为可选项，自动勾选其中名为「庭院、草坪与花园」和「庭院」的品类作为默认值。你可以手动增删。如果上传数据里不包含这两个品类名，则默认不勾选任何品类，季节因子不生效。

---

**Q2：趋势因子和季节匹配因子作用一致吗？需要都保留吗？**

两者作用层级不同，不冲突，建议都保留：

| | 季节匹配因子 | 趋势因子 |
|---|---|---|
| 作用步骤 | ③ 加权层 | ⑥ 最终占比层 |
| 做什么 | 放大同季历史数据的**权重** | 叠加年度趋势**差**到最终占比 |
| 影响范围 | 仅适用品类 | 全局（有趋势数据的SKU） |
| 关键参数 | β、N | α、调整上限 |

简单说：季节因子让预测贴近去年同期的数据分布，趋势因子让预测跟随年度变化方向。两者互补。

---

**Q3：「品类层最小等效观测数」默认值12代表什么含义？**

这是基准计算中的**贝叶斯收缩参数**。公式为：

```
子层权重 = n / (n + k_cat)
```

其中 n = 该层目标期的实际观测行数，k_cat = 此参数（默认12）。

| 观测行数 n | 子层权重 | 父层权重 | 含义 |
|---|---|---|---|
| n = 4 | 25% | 75% | 数据太少，大部分用父层均值 |
| n = 12 | 50% | 50% | 临界点，子层和父层各半 |
| n = 88 | 88% | 12% | 数据充足，信任子层自身 |

**k_cat 越大越保守**——子层样本少时更快被拉向上层均值。

---

**Q4：「季节增强倍数」默认值3.0代表什么含义？**

β=3.0 表示：落在季节窗口内的历史数据，其权重被**放大3倍**。

例如：某行数据原始衰减权重为 0.5，在季节窗口内则变为 0.5 × 3.0 = 1.5。
非季节性品类的数据权重不变（× 1.0）。

这让去年同季的数据在加权计算中占据主导地位，尤其适用于「庭院、草坪与花园」等强季节性品类。

---

**Q5：4个仓库的占比加起来必须等于100%，能实现吗？**

**已实现，三层保障**：

1. **引擎层**：Step6 计算完最终占比后自动归一化，四仓合计 = 100%
2. **侧边栏开关**：「归一化（四仓合计=100%）」默认开启，趋势调整后再次归一化
3. **微调区**：改一个仓的占比，其余三仓按原比例自动补足，合计锁定 100%
4. **自检**：计算后自动验证「最终占比」和「调整后占比」的合计偏差 < 1e-9

落货量分配使用**最大余额法**，保证整数合计精确等于发运批量。

---

**Q6：「减仓优化」是什么？**

侧边栏「⑥ 减仓优化」开关，默认关闭。启用后，月均单量<3单且分仓占比<5%的仓点会被减仓，其占比按其他仓现有比例等比分配并归一化到100%。仅影响SKU层分仓占比，上层聚合使用减仓前原始数据。导出时SKU表增加「是否减仓」列备注。
""")
