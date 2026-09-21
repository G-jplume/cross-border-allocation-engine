"""
分仓占比计算引擎 - Streamlit Web App

运行方式:
    streamlit run app.py

页面结构:
  侧边栏  参数面板（分组 + 生效状态标注 + 自检）
  主区域  1.上传数据 → 2.开始计算 → 3.结果 → 4.占比微调 → 5.可视化 → 6.导出
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
             ("reduction_summary", None), ("batch_edits", {})]:
    if k not in st.session_state:
        st.session_state[k] = v

WH_COLORS = {"美西": "#4e79a7", "美东": "#f28e2b", "美南GA": "#e15759", "美南TX": "#76b7b2"}
FONT = dict(family="Microsoft YaHei, sans-serif")

# 从独立文件读取使用说明（单一数据源，guide.md 同步生成 docx）
_guide_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guide.md")
with open(_guide_path, encoding="utf-8") as _gf:
    GUIDE_MD = _gf.read()


def get_seasonal_defaults(df):
    """从数据中提取品类列表和默认强季节品类。"""
    if df is None or "一级分类" not in df.columns:
        return [], []
    cats_all = sorted(df["一级分类"].dropna().astype(str).unique().tolist())
    cats_all = [c for c in cats_all if c.strip()]
    default_cats = [c for c in cats_all if c.strip() in ("庭院、草坪与花园", "庭院")]
    return cats_all, default_cats


def read_csv_auto(path):
    """自动检测编码读取CSV。"""
    try:
        return pd.read_csv(path, encoding="utf-8-sig", dtype={"源SKU": str})
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="gbk", dtype={"源SKU": str})


def clear_stale_results():
    """参数变更时清空旧结果。"""
    st.session_state.df_results = None
    st.session_state.engine = None
    st.session_state.agg_results = {}
    st.session_state.checks = []
    st.session_state.edited_ratios = None
    st.session_state.reduction_summary = None
    st.session_state.batch_edits = {}

st.title("📦 分仓占比计算引擎")
st.markdown(
    "上传历史出单数据 → 调节参数 → 计算各仓库发货占比 → 导出结果\n\n"
    "✅ 四仓占比合计恒为 100% | ✅ 最大余额法保证落货量整数精确 | ✅ 自动体检防静默失效"
)

_col_title, _col_dl = st.columns([4, 1])
with _col_dl:
    st.download_button(
        "📄 下载说明",
        data=GUIDE_MD.encode("utf-8-sig"),
        file_name="分仓占比计算引擎_使用说明.md",
        mime="text/markdown",
        use_container_width=True,
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
    """按 一级分类 / 室内外 / SPU / 全公司 聚合分仓占比（按单量加权平均）。"""
    wh_cols = {}
    for wh in WAREHOUSES:
        for cand in (f"调整后_{wh}", f"最终_{wh}"):
            if cand in df_sku.columns:
                wh_cols[wh] = cand
                break
    valid_whs = [wh for wh in WAREHOUSES if wh in wh_cols]
    if not valid_whs:
        st.warning("⚠️ 引擎输出中未找到仓库占比列，可能无SKU通过计算。请检查数据或参数设置。")
        return {}

    weight_col = "目标期单量" if "目标期单量" in df_sku.columns else None
    df_w = df_sku.copy()
    if weight_col:
        df_w["_w"] = df_w[weight_col].clip(lower=0)
    else:
        df_w["_w"] = 1.0

    agg = {}
    for level in ["一级分类", "室内外", "SPU"]:
        if level not in df_w.columns:
            continue
        rows = []
        for label, group in df_w.groupby(level):
            w = group["_w"].values
            w_sum = w.sum()
            if w_sum > 0:
                vals = {wh: float(np.average(group[wh_cols[wh]].values, weights=w)) for wh in valid_whs}
            else:
                vals = {wh: float(group[wh_cols[wh]].mean()) for wh in valid_whs}
            row = {level: label, "SKU数": len(group)}
            row.update({f"占比_{wh}": vals[wh] for wh in valid_whs})
            rows.append(row)
        g = pd.DataFrame(rows)
        tot = sum(g[f"占比_{wh}"] for wh in valid_whs)
        for wh in valid_whs:
            g[f"占比_{wh}"] = g[f"占比_{wh}"] / tot
        agg[level] = g

    w = df_w["_w"].values
    w_sum = w.sum()
    if w_sum > 0:
        overall = {wh: float(np.average(df_w[wh_cols[wh]].values, weights=w)) for wh in valid_whs}
    else:
        overall = {wh: float(df_w[wh_cols[wh]].mean()) for wh in valid_whs}
    tot = sum(overall.values())
    agg["全公司"] = pd.DataFrame([{
        "层级": "全公司", "SKU数": len(df_w),
        **{f"占比_{wh}": overall[wh] / tot for wh in valid_whs}
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
st.sidebar.subheader("目标发货月份")
st.sidebar.caption("支持跨年（如2026年12月→2027年3月）。")

_y_min, _y_max = 2024, 2030
if st.session_state.df_raw is not None and "年" in st.session_state.df_raw.columns:
    _years = pd.to_numeric(st.session_state.df_raw["年"], errors="coerce").dropna()
    if len(_years) > 0:
        _y_min = max(2020, int(_years.min()))
        _y_max = min(2035, int(_years.max()) + 2)
_ym_options = {f"{y}年{m}月": (y, m) for y in range(_y_min, _y_max + 1) for m in range(1, 13)}
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

target_start_seq = int(target_start_year) * 12 + int(target_start_month)
target_end_seq = int(target_end_year) * 12 + int(target_end_month)

if target_start_seq > target_end_seq:
    st.sidebar.warning("起始年月晚于结束年月，已自动交换。")
    target_start_seq, target_end_seq = target_end_seq, target_start_seq
    target_start_year, target_end_year = target_end_year, target_start_year
    target_start_month, target_end_month = target_end_month, target_start_month

anchor = (target_start_seq + target_end_seq) // 2
anchor_year = anchor // 12
anchor_month = (anchor - 1) % 12 + 1

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
st.sidebar.subheader("时间衰减加权")
st.sidebar.caption("同月用λ_same，非同月用λ×月份相似度（同月1.0/相邻0.6/隔2月0.3/隔3月+0.1）。")

lambda_val = st.sidebar.slider(
    "非同月衰减速度 λ", 0.50, 1.00, 0.85, 0.01,
    help="非同月数据时间衰减率。0.85=每月衰减15%。"
)
lambda_same = st.sidebar.slider(
    "同月跨年衰减速度 λ_same", 0.80, 1.00, 0.95, 0.01,
    help="同月跨年衰减率。0.95=去年保留95%。"
)
st.sidebar.caption(
    f"同月：去年{lambda_same:.0%}，前年{lambda_same**2:.0%} | 非同月：1月前{lambda_val:.0%}×相似度，6月前{lambda_val**6:.0%}×相似度"
)

# ---------- 季节适用品类 ----------
_cats_all, _default_cats = get_seasonal_defaults(st.session_state.df_raw)
seasonal_cats = _default_cats

# ---------- 趋势因子 ----------
with st.sidebar.expander("趋势因子 α", expanded=False):
    st.caption("去年vs前年同期占比差，叠加到最终占比。")

    alpha_trend = st.slider(
        "趋势因子 α", 0.0, 1.0, 0.3, 0.05,
        help="趋势差×α叠加到最终占比。0=关闭，0.3=默认，1.0=激进。"
    )
    trend_cap = st.slider(
        "单仓调整上限", 0.0, 0.20, 0.05, 0.01,
        help="单仓趋势调整幅度上限，防异常月份带偏。"
    )
    trend_min_orders = st.slider(
        "趋势单量门槛", 10, 100, 50, 5,
        help="去年+前年同期总单量低于此值则跳过趋势调整，避免噪声。"
    )
    if alpha_trend == 0:
        st.caption("⚪ α=0，趋势调整已关闭")
    else:
        st.caption(f"🟢 >100单用α={alpha_trend:.1f}，{trend_min_orders}-{100}单用α/2，<{trend_min_orders}单跳过")

    trend_recent_lo = target_start_seq - 12
    trend_recent_hi = target_end_seq - 12
    trend_far_lo = target_start_seq - 24
    trend_far_hi = target_end_seq - 24
    _tr_y = trend_recent_lo // 12
    _tr_m = (trend_recent_lo - 1) % 12 + 1
    _tr_y2 = trend_recent_hi // 12
    _tr_m2 = (trend_recent_hi - 1) % 12 + 1
    _tf_y = trend_far_lo // 12
    _tf_m = (trend_far_lo - 1) % 12 + 1
    _tf_y2 = trend_far_hi // 12
    _tf_m2 = (trend_far_hi - 1) % 12 + 1
    st.caption(
        f"📅 趋势对比窗口：\n"
        f"近期（去年同期）= {_tr_y}年{_tr_m}月~{_tr_y2}年{_tr_m2}月\n"
        f"远期（前年同期）= {_tf_y}年{_tf_m}月~{_tf_y2}年{_tf_m2}月"
    )

# ---------- 新品与基准 ----------
with st.sidebar.expander("新品与基准", expanded=False):
    st.caption("新品判定与自身/基准混合比例。基准自动计算。")

    new_product_threshold = st.slider(
        "新品阈值（月）", 1, 12, 2,
        help="历史出单月数≤此值视为新品，完全用基准占比。"
    )
    new_product_min_orders = st.slider(
        "新品订单量门槛", 1, 100, 20,
        help="目标期总单量<此值也视为新品，避免低单量偶然性。"
    )
    st.caption(
        f"新品：历史月数≤{new_product_threshold} 或 目标期单量<{new_product_min_orders}"
    )

    k_val = st.slider(
        "收缩强度 k", 1, 20, 8,
        help="自身权重=n/(n+k)，n=目标期月数。k越大越信基准，越小越信自身。"
    )
    _est_n = target_end_seq - target_start_seq + 1
    st.caption(
        f"k={k_val}：目标期约{_est_n}个月 → "
        f"自身权重≈{_est_n}/({_est_n}+{k_val})={_est_n/(_est_n+k_val):.0%}，"
        f"基准≈{k_val/(_est_n+k_val):.0%}"
    )

    col_a1, col_a2 = st.columns(2)
    with col_a1:
        a_min = st.slider("权重下限", 0.0, 0.5, 0.0, 0.05,
                          help="自身数据最低权重。0=新品完全用基准。")
    with col_a2:
        a_max = st.slider("权重上限", 0.5, 1.0, 0.9, 0.05,
                          help="自身数据最高权重。0.9=最多90%用自身。")

    k_cat = st.slider(
        "品类层最小等效观测数", 3, 50, 12,
        help="基准贝叶斯收缩参数。子层权重=n/(n+k_cat)。k_cat越大越保守，数据少时更快拉向父层。"
    )
    st.caption(
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
            clear_stale_results()
            st.success(f"上传成功！共 {len(_df)} 行 × {len(_df.columns)} 列")
    except Exception as e:
        st.error(f"读取失败: {e}")

elif use_sample:
    _sample = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "engine_data", "sheet2_raw.csv")
    if os.path.exists(_sample):
        try:
            _df = pd.read_csv(_sample, encoding="utf-8-sig",
                              dtype={"源SKU": str, "SPU": str, "室内外": str, "一级分类": str})
        except UnicodeDecodeError:
            _df = pd.read_csv(_sample, encoding="gbk",
                              dtype={"源SKU": str, "SPU": str, "室内外": str, "一级分类": str})
        st.session_state.df_raw = _df
        st.session_state.pop("seasonal_cats_ms", None)
        clear_stale_results()
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

        _bad_months = _d.loc[~pd.to_numeric(_d["月"], errors="coerce").between(1, 12)]
        if len(_bad_months) > 0:
            st.warning(f"⚠️ 检测到 {len(_bad_months)} 行月份不在 1-12 范围内，请检查数据")
        else:
            st.caption("✓ 月份均在 1-12 范围内")

        _neg = _d.loc[(_d[["美西", "美东", "美南GA", "美南TX"]] < 0).any(axis=1)]
        if len(_neg) > 0:
            st.warning(f"⚠️ 检测到 {len(_neg)} 行发货量为负数，请检查数据")
        else:
            st.caption("✓ 发货量无负数")

        _empty_sku = _d.loc[_d["源SKU"].isna() | (_d["源SKU"].astype(str).str.strip() == "")]
        if len(_empty_sku) > 0:
            st.warning(f"⚠️ 检测到 {len(_empty_sku)} 行源SKU为空，请检查数据")
        else:
            st.caption("✓ 源SKU无空值")

        _bad_years = _d.loc[~pd.to_numeric(_d["年"], errors="coerce").between(2020, 2035)]
        if len(_bad_years) > 0:
            st.warning(f"⚠️ 检测到 {len(_bad_years)} 行年份不在 2020-2035 范围内，请检查数据")
        else:
            st.caption("✓ 年份均在合理范围内")

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
        st.markdown("#### ⚙️ 计算选项")
        st.caption("选配后点击下方按钮")

        if "一级分类" in st.session_state.df_raw.columns:
            _cats_all, _default_cats = get_seasonal_defaults(st.session_state.df_raw)
            st.markdown("**🌿 季节适用品类**")
            st.caption("勾选强季节品类（如庭院类），只用目标期同月数据；未勾选的品类放开全部月份加权")
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
        st.markdown("**🔍 SKU级集中度自动检测**")
        st.caption("系统自动算每个SKU在目标期同月的出单量占全年的比例。集中度高→自动按强季节处理，低→自动按弱季节处理，中间→跟随品类设置")
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            conc_high = st.slider(
                "高集中度阈值（≥此值→强季节）%", 50, 100, 70, 5,
                format="%d%%",
                help="SKU在目标期同月的出单量占全年比例≥此值时，自动判定为强季节，即使品类未勾选"
            )
        with col_c2:
            conc_low = st.slider(
                "低集中度阈值（≤此值→弱季节）%", 0, 50, 20, 5,
                format="%d%%",
                help="SKU在目标期同月的出单量占全年比例≤此值时，自动判定为弱季节，即使品类已勾选"
            )
        conc_high_f = conc_high / 100.0
        conc_low_f = conc_low / 100.0
        st.caption(f"集中度≥{conc_high}%→自动强季节 | 集中度≤{conc_low}%→自动弱季节 | 中间→跟随品类勾选")

        st.markdown("---")
        st.markdown("**📦 减仓优化**")
        col_r1, col_r2 = st.columns(2)
        with col_r1:
            reduction_monthly_threshold = st.number_input(
                "月均单量上限", min_value=1, value=3, step=1,
                help="月均低于此值的仓点会被减仓"
            )
        with col_r2:
            reduction_ratio_threshold = st.slider(
                "占比上限%", 1, 20, 5, 1, format="%d%%",
                help="占比低于此值的仓点会被减仓"
            )
        reduction_ratio_f = reduction_ratio_threshold / 100.0
        st.caption(f"月均<{reduction_monthly_threshold}单且占比<{reduction_ratio_threshold}%的仓点按比例均分到其他仓")
        reduction_on = st.checkbox("启用减仓优化", value=False)

    if st.button("🚀 开始计算", type="primary", use_container_width=True):
        try:
            engine = AllocationEngine()
            engine.df_raw = st.session_state.df_raw.copy()

            if "mix_mapping" in st.session_state:
                engine.mix_mapping = st.session_state.mix_mapping

            engine.params["anchor"] = anchor
            engine.params["target_start"] = target_start_month
            engine.params["target_end"] = target_end_month
            engine.params["lambda"] = lambda_val
            engine.params["lambda_same_month"] = lambda_same
            engine.params["k"] = k_val
            engine.params["a_min"] = a_min
            engine.params["a_max"] = a_max
            engine.params["alpha_trend"] = alpha_trend
            engine.params["trend_cap"] = trend_cap
            engine.params["trend_min_orders"] = trend_min_orders
            engine.new_product_threshold = new_product_threshold
            engine.k_cat = float(k_cat)
            engine.seasonal_categories = seasonal_cats
            engine.concentration_threshold_high = float(conc_high_f)
            engine.concentration_threshold_low = float(conc_low_f)

            with st.status("计算中...", expanded=True) as status:
                engine.df_raw["在目标期"] = engine.df_raw.apply(
                    lambda row: 1 if target_start_seq <= int(row["年"]) * 12 + int(row["月"]) <= target_end_seq else 0,
                    axis=1
                )

                st.write("步骤1/5：SKU映射...")
                engine.step1_sku_mapping()
                st.write("步骤2/5：时间衰减加权...")
                engine.step2_decay_weight()
                st.write("步骤3/5：自身占比计算（含季节品类分支）...")
                engine.step3_self_ratio()
                st.write("步骤4/5：基准占比计算...")
                engine.step4_benchmark()
                st.write("步骤5/5：最终占比合并 & 减仓自检...")
                df_results = engine.step5_final_ratio(demand_qty=1)

                if df_results.empty:
                    st.warning("⚠️ 引擎未产出任何SKU结果。请检查数据是否包含目标期月份的出单记录。")
                    status.update(label="计算完成（0个SKU）", state="complete")
                    st.stop()

                drop_cols = [c for c in df_results.columns if c.startswith("落货量")]
                df_results = df_results.drop(columns=drop_cols)

                st.session_state.agg_results = compute_aggregate_ratios(df_results)

                if reduction_on:
                    df_results, reduction_summary = apply_warehouse_reduction(
                        engine, df_results,
                        float(reduction_monthly_threshold),
                        float(reduction_ratio_f)
                    )
                    st.session_state.reduction_summary = reduction_summary
                else:
                    df_results["是否减仓"] = ""
                    st.session_state.reduction_summary = None

                st.session_state.df_results = df_results
                st.session_state.engine = engine
                st.session_state.checks = engine.self_check(df_results)
                st.session_state.edited_ratios = None
                st.session_state.batch_edits = {}
                st.session_state.calc_params = {
                    "anchor": anchor, "lambda": lambda_val,
                    "lambda_same": lambda_same, "k": k_val,
                    "a_min": a_min, "a_max": a_max,
                    "alpha_trend": alpha_trend, "trend_cap": trend_cap,
                    "trend_min_orders": trend_min_orders,
                    "new_product_threshold": new_product_threshold,
                    "new_product_min_orders": new_product_min_orders,
                    "k_cat": k_cat, "seasonal_cats": seasonal_cats,
                    "conc_high": conc_high_f, "conc_low": conc_low_f,
                    "reduction_on": reduction_on,
                    "reduction_monthly": reduction_monthly_threshold,
                    "reduction_ratio": reduction_ratio_f,
                    "target_start_seq": target_start_seq,
                    "target_end_seq": target_end_seq,
                }

                n_msg = f"完成！共 {len(df_results)} 个运算SKU"
                if reduction_on and reduction_summary["n_reduced_skus"] > 0:
                    n_msg += f"（减仓: {reduction_summary['n_reduced_skus']}个SKU, {reduction_summary['n_reduced_slots']}个仓位）"
                status.update(label=n_msg, state="complete")
        except Exception as e:
            st.error(f"计算失败: {e}")
            with st.expander("查看详细错误信息", expanded=False):
                st.exception(e)


# ==========================================================
# 主区域 3：结果展示
# ==========================================================
if st.session_state.df_results is not None:
    df_r = st.session_state.df_results
    ADJ = [f"调整后_{wh}" for wh in WAREHOUSES]
    FIN = [f"最终_{wh}" for wh in WAREHOUSES]

    _calc_p = st.session_state.get("calc_params")
    if _calc_p:
        _current = {
            "anchor": anchor, "lambda": lambda_val,
            "lambda_same": lambda_same, "k": k_val,
            "a_min": a_min, "a_max": a_max,
            "alpha_trend": alpha_trend, "trend_cap": trend_cap,
            "trend_min_orders": trend_min_orders,
            "new_product_threshold": new_product_threshold,
            "new_product_min_orders": new_product_min_orders,
            "k_cat": k_cat, "seasonal_cats": seasonal_cats,
            "conc_high": conc_high_f, "conc_low": conc_low_f,
            "reduction_on": reduction_on,
            "reduction_monthly": reduction_monthly_threshold,
            "reduction_ratio": reduction_ratio_f,
            "target_start_seq": target_start_seq,
            "target_end_seq": target_end_seq,
        }
        _changed = [k for k, v in _current.items() if v != _calc_p.get(k)]
        if _changed:
            st.warning(f"⚠️ 参数已变更（{', '.join(_changed)}），以下结果可能过期，请重新计算。")

    st.subheader("3. 计算结果")

    checks = st.session_state.get("checks", [])
    if checks:
        bad = [c for c in checks if not c[1]]
        with st.expander("🔍 自动体检", expanded=bool(bad)):
            for name, ok, msg in checks:
                (st.success if ok else st.warning)(f"{'✓' if ok else '⚠'} **{name}** — {msg}")

    reduction_summary = st.session_state.get("reduction_summary")
    if reduction_on and reduction_summary and reduction_summary["n_reduced_skus"] > 0:
        with st.expander(f"📦 减仓优化（{reduction_summary['n_reduced_skus']}个SKU, {reduction_summary['n_reduced_slots']}个仓位）", expanded=False):
            st.caption("仅影响SKU层，上层聚合用减仓前原始数据。")

    _weights = df_r["目标期单量"].values if "目标期单量" in df_r.columns else None
    _w_sum = float(_weights.sum()) if _weights is not None and _weights.sum() > 0 else 0

    col_k1 = st.columns(5)
    col_k1[0].metric("运算SKU 总数", f"{len(df_r):,}")
    for i, wh in enumerate(WAREHOUSES):
        if _w_sum > 0:
            adj_val = float(np.average(df_r[ADJ[i]].values, weights=_weights))
        else:
            adj_val = float(df_r[ADJ[i]].mean())
        base_val = float(df_r[f"基准_{wh}"].mean()) if f"基准_{wh}" in df_r.columns else 0.25
        delta_val = adj_val - base_val
        col_k1[i + 1].metric(
            f"{wh} 平均占比", f"{adj_val:.1%}",
            delta=f"{delta_val:+.1%}", delta_color="inverse"
        )

    st.caption("📊 平均占比按各SKU目标期出单量加权计算（大单量SKU话语权更大）" if _w_sum > 0 else "📊 平均占比为简单平均（无单量数据时回退）")

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

    base_cols = ["SKU", "SPU", "一级分类", "室内外", "历史月数", "目标期月数",
                 "收缩权重_a", "季节集中度", "季节性来源", "基准层级", "基准观测数"]
    base_cols = [c for c in base_cols if c in df_show.columns]

    metric_tabs = st.tabs(["调整后占比", "最终占比", "自身占比", "基准占比", "趋势差", "计算路径"])
    metric_groups = [
        ("调整后", [f"调整后_{wh}" for wh in WAREHOUSES] + ["是否减仓"], False),
        ("最终", [f"最终_{wh}" for wh in WAREHOUSES], False),
        ("自身", [f"自身_{wh}" for wh in WAREHOUSES], False),
        ("基准", [f"基准_{wh}" for wh in WAREHOUSES], False),
        ("趋势差", [f"趋势差_{wh}" for wh in WAREHOUSES], True),
    ]

    for tab, (prefix, cols, is_trend) in zip(metric_tabs[:5], metric_groups):
        tab_cols = base_cols + [c for c in cols if c in df_show.columns]
        fmt = {}
        if "收缩权重_a" in tab_cols:
            fmt["收缩权重_a"] = "{:.3f}"
        if "季节集中度" in tab_cols:
            fmt["季节集中度"] = "{:.0%}"
        for c in tab_cols:
            if c.startswith("趋势差"):
                fmt[c] = "{:+.2%}"
            elif c.split("_")[0] in ("自身", "基准", "最终", "调整后"):
                fmt[c] = "{:.2%}"
        tab.dataframe(
            df_show[tab_cols].style.format(fmt),
            use_container_width=True, height=400
        )

    path_tab = metric_tabs[5]
    if len(df_show) > 0:
        _path_sku = path_tab.selectbox("选择SKU查看计算路径", sorted(df_show["SKU"].astype(str)),
                                       key="path_sku_sel")
        if _path_sku:
            _row = df_show[df_show["SKU"].astype(str) == _path_sku].iloc[0]
            _pc1, _pc2 = path_tab.columns(2)
            with _pc1:
                path_tab.markdown("**基本信息**")
                path_tab.markdown(f"""
                - SKU：{_row['SKU']}
                - SPU：{_row.get('SPU', '—')}
                - 一级分类：{_row.get('一级分类', '—')}
                - 室内外：{_row.get('室内外', '—')}
                - 历史月数：{_row.get('历史月数', '—')}
                - 目标期月数：{_row.get('目标期月数', '—')}
                - 目标期单量：{_row.get('目标期单量', '—')}
                - 是否新品：{_row.get('是否新品', '—')}
                """)
            with _pc2:
                path_tab.markdown("**计算参数**")
                path_tab.markdown(f"""
                - 收缩权重 a：{_row.get('收缩权重_a', 0):.3f}
                - 基准层级：{_row.get('基准层级', '—')}
                - 基准观测数：{_row.get('基准观测数', '—')}
                - 基准父层：{_row.get('基准父层', '—')}
                - 趋势α：{_row.get('趋势alpha', 0):.2f}
                - 是否减仓：{_row.get('是否减仓', '否')}
                """)

            path_tab.markdown("**四仓占比明细**")
            _path_df = pd.DataFrame([
                {"仓库": wh,
                 "自身占比": _row.get(f"自身_{wh}", 0),
                 "基准占比": _row.get(f"基准_{wh}", 0),
                 "最终占比": _row.get(f"最终_{wh}", 0),
                 "趋势差": _row.get(f"趋势差_{wh}", 0),
                 "调整后占比": _row.get(f"调整后_{wh}", 0)}
                for wh in WAREHOUSES
            ])
            path_tab.dataframe(
                _path_df.style.format({
                    "自身占比": "{:.2%}", "基准占比": "{:.2%}",
                    "最终占比": "{:.2%}", "趋势差": "{:+.2%}",
                    "调整后占比": "{:.2%}"
                }),
                use_container_width=True, hide_index=True
            )

    # --- 聚合层级 ---
    agg_results = st.session_state.get("agg_results", {})
    if agg_results:
        st.markdown("**聚合层级分仓占比**（按单量加权平均）")
        tabs = st.tabs(["一级分类", "室内外", "SPU", "全公司"])
        for i, level in enumerate(["一级分类", "室内外", "SPU", "全公司"]):
            if level in agg_results:
                tabs[i].dataframe(
                    agg_results[level].style.format(
                        {f"占比_{wh}": "{:.2%}" for wh in WAREHOUSES}
                    ), use_container_width=True
                )
                if level in ("一级分类", "室内外") and len(agg_results[level]) > 0:
                    _agg_df = agg_results[level]
                    _label_col = _agg_df.columns[0] if _agg_df.columns[0] != "index" else _agg_df.columns[1]
                    fig_agg = go.Figure()
                    for wh in WAREHOUSES:
                        fig_agg.add_trace(go.Bar(
                            x=_agg_df[_label_col].astype(str),
                            y=_agg_df[f"占比_{wh}"],
                            name=wh, marker_color=WH_COLORS[wh]
                        ))
                    fig_agg.update_layout(
                        barmode="stack", title=f"{level}聚合分仓占比",
                        xaxis_title=level, yaxis_title="占比",
                        yaxis=dict(tickformat=".0%", range=[0, 1]),
                        font=FONT, height=350
                    )
                    tabs[i].plotly_chart(fig_agg, use_container_width=True)


    # ======================================================
    # 主区域 4：占比微调（锁定合计 100%）
    # ======================================================
    with st.expander("4. 占比微调（点击展开）", expanded=False):
        st.caption("改任意一仓，其余三仓按原比例自动补足，合计始终100%。")
        st.info("🔒 四仓合计=100%：计算归一化→趋势归一化→微调补足→自检偏差<1e-9")

        _tune_mode = st.radio("微调模式", ["单个SKU微调", "批量微调"], horizontal=True, key="tune_mode")

        if _tune_mode == "单个SKU微调":
            adj_sku = st.selectbox("选择要微调的SKU", sorted(df_r["SKU"].astype(str)),
                                   key="tune_sku")
            if adj_sku:
                _row = df_r[df_r["SKU"].astype(str) == adj_sku].iloc[0]
                base = {wh: float(_row[f"调整后_{wh}"]) for wh in WAREHOUSES}

                c_left, c_right = st.columns([1, 1])

                with c_left:
                    st.markdown("**拖动滑块调整四仓占比**")
                    new_vals = {}
                    for wh in WAREHOUSES:
                        new_vals[wh] = st.slider(
                            f"{wh}", min_value=0.0, max_value=1.0,
                            value=round(base[wh], 4), step=0.01,
                            format="%.0f%%", key=f"tune_{adj_sku}_{wh}",
                        )
                    tot_new = sum(new_vals.values())
                    if abs(tot_new - 1.0) < 1e-9:
                        st.success(f"合计 = {tot_new:.4%} ✓")
                    else:
                        st.error(f"合计 = {tot_new:.4%} ✗ 需要等于 100%")

                with c_right:
                    st.markdown("**一键锁定到 100%**")
                    st.caption("以改动最大的仓为准，其余三仓按原比例补足。")
                    col_fix, col_reset = st.columns(2)
                    with col_fix:
                        if st.button("自动补足到 100%", use_container_width=True):
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
                                for wh in WAREHOUSES:
                                    st.session_state[f"tune_{adj_sku}_{wh}"] = round(fixed[wh], 4)
                                st.rerun()
                    with col_reset:
                        if st.button("🔄 恢复引擎值", use_container_width=True):
                            st.session_state.edited_ratios = None
                            for wh in WAREHOUSES:
                                st.session_state[f"tune_{adj_sku}_{wh}"] = round(base[wh], 4)
                            st.rerun()

                final_use = st.session_state.get("edited_ratios") or base
                if st.session_state.get("edited_ratios"):
                    st.info("已应用自动补足结果，下方落货量按修正后的占比计算。")

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

        elif _tune_mode == "批量微调":
            st.markdown("**按品类或SPU批量调整**")
            batch_level = st.selectbox("选择批量维度", ["一级分类", "SPU", "室内外"], key="batch_level")

            if batch_level == "一级分类":
                batch_options = sorted(df_r["一级分类"].dropna().unique())
            elif batch_level == "SPU":
                batch_options = sorted(df_r["SPU"].dropna().unique())
            else:
                batch_options = sorted(df_r["室内外"].dropna().unique())

            batch_target = st.selectbox(f"选择{batch_level}", batch_options, key="batch_target")

            st.markdown("**设置调整方式**")
            batch_mode = st.radio("调整方式", ["整体偏移", "直接设定占比"], horizontal=True, key="batch_mode")

            batch_skus = df_r[df_r[batch_level] == batch_target]["SKU"].astype(str).tolist()
            st.caption(f"将影响 {len(batch_skus)} 个SKU")

            if batch_mode == "整体偏移":
                st.markdown("为每个仓库设置增减幅度（百分点），合计需为0")
                col_b1, col_b2, col_b3, col_b4 = st.columns(4)
                batch_deltas = {}
                for col_b, wh in zip([col_b1, col_b2, col_b3, col_b4], WAREHOUSES):
                    batch_deltas[wh] = col_b.slider(f"{wh} (%)", -20, 20, 0, 1, key=f"bdelta_{wh}")

                _sum_delta = sum(batch_deltas.values())
                if _sum_delta != 0:
                    st.warning(f"⚠️ 四仓增减合计 = {_sum_delta}%，需要等于 0 才能保持100%")
                else:
                    st.success(f"✓ 四仓增减合计 = 0%")

                if st.button("应用批量偏移", type="primary", use_container_width=True):
                    for sku in batch_skus:
                        _srow = df_r[df_r["SKU"].astype(str) == sku].iloc[0]
                        _sbase = {wh: float(_srow[f"调整后_{wh}"]) for wh in WAREHOUSES}
                        _sadj = {wh: max(0.0, _sbase[wh] + batch_deltas[wh] / 100.0) for wh in WAREHOUSES}
                        _stot = sum(_sadj.values())
                        if _stot > 0:
                            _sadj = {wh: v / _stot for wh, v in _sadj.items()}
                        st.session_state.batch_edits[sku] = _sadj
                    st.success(f"已对 {len(batch_skus)} 个SKU应用批量偏移")
                    st.rerun()

            elif batch_mode == "直接设定占比":
                st.markdown("直接设定四仓占比（合计需为100%）")
                col_b1, col_b2, col_b3, col_b4 = st.columns(4)
                batch_vals = {}
                for col_b, wh in zip([col_b1, col_b2, col_b3, col_b4], WAREHOUSES):
                    batch_vals[wh] = col_b.slider(f"{wh} (%)", 0, 100, 25, 1, key=f"bval_{wh}")

                _sum_b = sum(batch_vals.values())
                if _sum_b != 100:
                    st.warning(f"⚠️ 四仓合计 = {_sum_b}%，需要等于 100%")
                else:
                    st.success(f"✓ 四仓合计 = 100%")

                if st.button("应用批量占比", type="primary", use_container_width=True):
                    for sku in batch_skus:
                        st.session_state.batch_edits[sku] = {wh: batch_vals[wh] / 100.0 for wh in WAREHOUSES}
                    st.success(f"已对 {len(batch_skus)} 个SKU应用统一占比")
                    st.rerun()

            if st.session_state.batch_edits:
                _n_batch = len(st.session_state.batch_edits)
                if st.button(f"清除批量调整（{_n_batch}个SKU）"):
                    st.session_state.batch_edits = {}
                    st.rerun()

    # ======================================================
    # 可视化
    # ======================================================
    st.subheader("5. 可视化")

    vis_tab1, vis_tab2 = st.tabs(["单SKU占比", "全SKU堆叠图"])

    with vis_tab1:
        chart_sku = st.selectbox("选择SKU", sorted(df_r["SKU"].astype(str)),
                                 key="chart_sku")
        if chart_sku:
            row = df_r[df_r["SKU"].astype(str) == chart_sku].iloc[0]
            _chart_base = st.session_state.batch_edits.get(chart_sku, {wh: row[f"调整后_{wh}"] for wh in WAREHOUSES})
            pie_data = pd.DataFrame({
                "仓库": WAREHOUSES,
                "占比": [_chart_base[wh] for wh in WAREHOUSES],
            })
            fig = px.pie(pie_data, values="占比", names="仓库",
                         title=f"{chart_sku} 分仓占比", color="仓库",
                         color_discrete_map=WH_COLORS)
            fig.update_layout(font=FONT)
            fig.update_traces(textposition="inside", textinfo="label+percent")
            st.plotly_chart(fig, use_container_width=True)

    with vis_tab2:
        search_sku = st.text_input("搜索SKU（留空显示全部）", key="stack_search")
        df_stack = df_r.copy()
        if search_sku.strip():
            df_stack = df_stack[df_stack["SKU"].astype(str).str.contains(search_sku.strip(), case=False, na=False)]

        _page_size = 30
        _total_pages = max(1, (len(df_stack) + _page_size - 1) // _page_size)
        _cur_page = st.session_state.get("stack_page", 1)
        _new_page = st.selectbox("页码", list(range(1, _total_pages + 1)),
                                 index=_cur_page - 1, key="stack_page_sel")
        st.session_state.stack_page = _new_page
        st.caption(f"第 {_new_page} / {_total_pages} 页（共 {len(df_stack)} 个SKU）")

        _start = (_new_page - 1) * _page_size
        _end = _start + _page_size
        df_page = df_stack.iloc[_start:_end]

        if len(df_page) > 0:
            fig_stack = go.Figure()
            for wh in WAREHOUSES:
                fig_stack.add_trace(go.Bar(
                    x=df_page["SKU"].astype(str), y=df_page[ADJ[WAREHOUSES.index(wh)]],
                    name=wh, marker_color=WH_COLORS[wh]
                ))
            fig_stack.update_layout(barmode="stack", title=f"各SKU分仓占比（第{_new_page}页，本页{len(df_page)}个）",
                                    xaxis_title="SKU", yaxis_title="占比",
                                    yaxis=dict(tickformat=".0%", range=[0, 1]),
                                    font=FONT, height=450)
            st.plotly_chart(fig_stack, use_container_width=True)
        else:
            st.info("未找到匹配的SKU。")

    # ======================================================
    # 导出
    # ======================================================
    st.subheader("6. 导出结果")
    c_e1, c_e2, c_e3, c_e4 = st.columns(4)
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
        _all_agg_dfs = []
        for lv in ["一级分类", "室内外", "SPU", "全公司"]:
            if lv in agg_results:
                _t = agg_results[lv].copy()
                _t.insert(0, "层级", lv)
                _all_agg_dfs.append(_t)
        if _all_agg_dfs:
            _all_agg = pd.concat(_all_agg_dfs, ignore_index=True)
            st.download_button("下载 CSV（全层级聚合）",
                               data=_all_agg.to_csv(index=False).encode("utf-8-sig"),
                               file_name="分仓占比结果_全层级聚合.csv", mime="text/csv",
                               use_container_width=True)
    with c_e4:
        if "一级分类" in agg_results:
            st.download_button("下载 CSV（一级分类）",
                               data=agg_results["一级分类"].to_csv(index=False).encode("utf-8-sig"),
                               file_name="分仓占比结果_一级分类.csv", mime="text/csv",
                               use_container_width=True)
