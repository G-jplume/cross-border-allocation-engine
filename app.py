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
st.sidebar.caption("发哪个月的货。系统只统计这段月份的历史数据，并以此推导季节窗口和趋势窗口。")

col_m1, col_m2 = st.sidebar.columns(2)
with col_m1:
    target_start_month = st.number_input("起始月", 1, 12, 1,
                                         help="单月发货就填相同数字，如 5、5")
with col_m2:
    target_end_month = st.number_input("结束月", 1, 12, 3,
                                       help="发 4-6 月三批货就填 4 和 6")
target_year = st.sidebar.number_input("目标年份", 2024, 2030, 2027)

if target_start_month > target_end_month:
    st.sidebar.warning("起始月大于结束月，已自动交换。")
    target_start_month, target_end_month = target_end_month, target_start_month

eff("目标发货月份",
    f"锚点 = {target_year}年{target_start_month}-{target_end_month}月。"
    "衰减权重只作用于目标期月份，其余月份权重归零。")

# ---------- 时间衰减 ----------
st.sidebar.subheader("② 时间衰减加权")
st.sidebar.caption("控制目标期内历史数据的影响力：越接近锚点的月份权重越高。")

lambda_val = st.sidebar.slider(
    "衰减速度 λ", 0.50, 1.00, 0.85, 0.01,
    help="每月权重乘以 λ。0.85=每月衰减15%；1.0=完全不衰减。"
)
st.sidebar.caption("λ=0.85：1个月前权重85%，6个月前38%，12个月前14%")

# ---------- 季节匹配 ----------
st.sidebar.subheader("③ 季节匹配因子")
st.sidebar.caption(
    "对季节性品类，把「目标月之前 N 个月」的历史数据权重放大 β 倍，"
    "让去年同季的表现主导预测。**作用于加权层（步骤③），与趋势因子作用层不同，两者不冲突。**"
)

seasonal_on = st.sidebar.checkbox("启用季节因子", value=True)

if seasonal_on:
    beta = st.sidebar.slider(
        "季节增强倍数 β", 1.0, 5.0, 3.0, 0.1,
        help=(
            "落在季节窗口内的历史数据，其权重乘以此倍数。\n"
            "β=3.0（默认）：权重放大3倍。例如原始衰减权重0.5 → 放大后1.5\n"
            "β=1.0：不增强（等同关闭季节因子）\n"
            "β=5.0：放大5倍（极端季节品类适用）"
        )
    )
    st.sidebar.caption(
        f"β={beta:.1f}：同季节数据权重 × {beta:.1f}，"
        f"非季节性数据权重不变（×1.0）"
    )
    seasonal_window = st.sidebar.slider(
        "季节窗口范围 N", 1, 3, 1,
        help=(
            "窗口 = 目标月之前 N 个月（目标月本身不计入），距离按环形计算。\n"
            "N=1（默认）：目标月前1个月。发1月货 → 命中12月\n"
            "N=2：目标月前2个月。发1月货 → 命中11、12月\n"
            "N=3：目标月前3个月。发1月货 → 命中10、11、12月"
        )
    )
else:
    beta, seasonal_window = 1.0, 1

# 季节适用品类 —— 从上传数据动态识别，默认勾选庭院类
st.sidebar.markdown("**季节适用品类**")
st.sidebar.caption(
    "选项从上传数据的「一级分类」列自动生成。\n"
    "默认勾选「庭院、草坪与花园」和「庭院」——经跨年同月检验季节性显著。\n"
    "可手动增删：不选任何品类 = 季节因子不生效。"
)

if st.session_state.df_raw is not None and "一级分类" in st.session_state.df_raw.columns:
    _cats_all = sorted(
        st.session_state.df_raw["一级分类"].dropna().astype(str).unique().tolist()
    )
    _cats_all = [c for c in _cats_all if c.strip()]
    _default_cats = [c for c in _cats_all if c.strip() in ("庭院、草坪与花园", "庭院")]
    seasonal_cats = st.sidebar.multiselect(
        "选择要启用季节因子的品类（可多选）",
        options=_cats_all,
        default=_default_cats,
        key="seasonal_cats_ms",
    )
    n_rows_cat = int(st.session_state.df_raw["一级分类"].isin(seasonal_cats).sum())
    if seasonal_cats:
        st.sidebar.caption(f"✅ 已选 {len(seasonal_cats)} 个品类，覆盖 {n_rows_cat} 行数据")
    else:
        st.sidebar.warning("⚠️ 未选择任何品类，季节因子不会生效（相当于关闭）")
else:
    st.sidebar.caption("📥 上传数据后，这里会列出你的「一级分类」供勾选")
    seasonal_cats = []

eff("季节匹配因子", "作用于加权层（步骤③），与衰减因子相乘，放大同季历史数据权重。")

# ---------- 趋势因子 ----------
st.sidebar.subheader("④ 趋势因子 α")
st.sidebar.caption(
    "比较「去年同期」与「前年同期」的分仓占比差，把趋势方向叠加到最终占比上。"
    "**作用于最终占比层（步骤⑥），与季节因子（步骤③加权层）不冲突，可同时启用。**"
)
st.sidebar.info(
    "📊 **季节因子 vs 趋势因子**\n"
    "- 季节因子：管「数据权重」——放大同季历史数据的影响力\n"
    "- 趋势因子：管「结果方向」——把年度变化趋势叠加到最终占比\n"
    "- 两者互补：季节因子让预测贴近同期，趋势因子让预测跟随年度变化"
)

alpha_trend = st.sidebar.slider(
    "趋势因子 α", 0.0, 1.0, 0.3, 0.05,
    help=(
        "趋势差 × α 叠加到最终占比。\n"
        "α=0：关闭趋势调整\n"
        "α=0.3（默认）：叠加30%的趋势差\n"
        "α=1.0：完全采用趋势差（激进）"
    )
)
trend_cap = st.sidebar.slider(
    "单仓调整上限", 0.0, 0.20, 0.05, 0.01,
    help="每个仓库的趋势调整幅度不超过 ±此值，防止单个异常月份把占比带偏"
)
if alpha_trend == 0:
    st.sidebar.caption("⚪ α=0，趋势调整已关闭")
else:
    st.sidebar.caption(
        f"🟢 趋势窗口 = 去年同期 vs 前年同期，单仓最多调整 ±{trend_cap:.0%}"
    )

norm_mode = st.sidebar.radio(
    "四仓占比合计处理方式",
    ["归一化（四仓合计=100%）⭐", "保留原值（同Excel模板）"],
    index=0,
    help=(
        "归一化（推荐）：趋势调整后把四仓占比缩放到合计 100%\n"
        "保留原值：与 Excel 模板 AS 列一致，合计可能在 97%~103% 之间"
    ),
)
norm_method = "proportional" if norm_mode.startswith("归一化") else "raw"
if norm_method == "proportional":
    st.sidebar.caption("✅ 已启用归一化：四仓占比合计恒为 100%")

# ---------- 新品与基准 ----------
st.sidebar.subheader("⑤ 新品与基准")
st.sidebar.caption("控制新品判定、以及自身数据与基准的混合比例。基准自动从原始数据计算。")

new_product_threshold = st.sidebar.slider(
    "新品阈值（月）", 1, 12, 2,
    help="历史出单月数 ≤ 此值的 SKU 视为新品，完全使用基准占比"
)
st.sidebar.caption(
    f"历史出单月数≤{new_product_threshold} → 视为新品，自身权重=0，完全用基准"
)

k_val = st.sidebar.slider(
    "收缩强度 k", 1, 20, 6,
    help=(
        "自身数据与基准的混合比例。\n"
        "公式：自身权重 = n / (n + k)，n = 目标期月数\n"
        "k=6（默认）：目标期6个月 → 自身50%、基准50%\n"
        "k越大越信任基准，k越小越信任SKU自身历史"
    )
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
                      help="自身数据的最低权重。0=新品完全用基准")
with col_a2:
    a_max = st.slider("权重上限", 0.5, 1.0, 0.9, 0.05,
                      help="自身数据的最高权重。0.9=最多90%用自身数据")

k_cat = st.sidebar.slider(
    "品类层最小等效观测数", 3, 50, 12,
    help=(
        "基准计算中的贝叶斯收缩参数。\n"
        "公式：子层权重 = n / (n + k_cat)，n = 该层目标期实际观测行数\n"
        "k_cat=12（默认）：\n"
        "  · n=12时 → 子层50%、父层50%（各半）\n"
        "  · n=88时 → 子层88%、父层12%（数据充足，信任子层）\n"
        "  · n=4时  → 子层25%、父层75%（数据少，拉向父层均值）\n"
        "k_cat越大越保守——子层样本少时更快被拉向上层均值"
    )
)
st.sidebar.caption(
    f"k_cat={int(k_cat)}：n={int(k_cat)}时子层父层各占50%，"
    f"n>{int(k_cat)}时偏向子层自身，n<{int(k_cat)}时偏向父层均值"
)

# ---------- 减仓优化 ----------
st.sidebar.subheader("⑥ 减仓优化")
st.sidebar.caption(
    "启用后，对每个运算SKU×仓点判断：月均单量 < 阈值 且 分仓占比 < 阈值 → 该仓点减仓。\n"
    "被减的占比按其他仓现有比例等比分配，归一化到100%。\n"
    "仅影响SKU层分仓占比，上层（SPU/品类/室内外/公司）仍用减仓前原始数据汇总。"
)

reduction_on = st.sidebar.checkbox("启用减仓优化", value=False)

if reduction_on:
    col_r1, col_r2 = st.sidebar.columns(2)
    with col_r1:
        monthly_threshold = st.number_input(
            "月均单量阈值", 0.1, 100.0, 3.0, 0.1,
            help=(
                "该运算SKU在该仓的月均出单量低于此值时触发减仓判断。\n"
                "月均 = 该仓总单量 ÷ 上架月数（首次出单月到数据最新月份）\n"
                "默认3：月均不到3单的仓点备货效率过低"
            )
        )
    with col_r2:
        ratio_threshold = st.slider(
            "分仓占比阈值", 0.01, 0.30, 0.05, 0.01,
            help=(
                "该仓占该运算SKU总单量的比例低于此值时触发减仓判断。\n"
                "默认5%：占比不到5%的仓不是该SKU的主要出货仓"
            )
        )
    st.sidebar.caption(
        f"双条件同时满足才减仓：月均<{monthly_threshold}单 且 占比<{ratio_threshold:.0%}\n"
        "上架月数 = 首次出单月到数据最新月份（自动检测，非固定8月）\n"
        "重分配方式：被减占比按其他仓现有比例等比分配 → 归一化到100%"
    )
else:
    monthly_threshold = 3.0
    ratio_threshold = 0.05

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
    if st.button("开始计算", type="primary", use_container_width=True):
        with st.spinner("计算中..."):
            try:
                engine = AllocationEngine()
                engine.df_raw = st.session_state.df_raw.copy()

                if "mix_mapping" in st.session_state:
                    engine.mix_mapping = st.session_state.mix_mapping

                # 锚点 = 目标期中间月份的月份序号
                anchor = int(target_year) * 12 + (target_start_month + target_end_month) // 2
                engine.params["anchor"] = anchor
                engine.params["lambda"] = lambda_val
                engine.params["k"] = k_val
                engine.params["a_min"] = a_min
                engine.params["a_max"] = a_max
                engine.params["alpha_trend"] = alpha_trend
                engine.params["trend_cap"] = trend_cap
                engine.params["norm_method"] = norm_method
                engine.params["seasonal_switch"] = 1 if (seasonal_on and seasonal_cats) else 0
                engine.params["seasonal_beta"] = beta
                engine.params["seasonal_window"] = seasonal_window
                engine.new_product_threshold = new_product_threshold
                engine.k_cat = float(k_cat)
                engine.seasonal_categories = seasonal_cats

                engine.df_raw["在目标期"] = engine.df_raw["月"].apply(
                    lambda m: 1 if target_start_month <= int(m) <= target_end_month else 0
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
                        engine, df_results, monthly_threshold, ratio_threshold
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
        with st.expander(f"📦 减仓优化摘要（{reduction_summary['n_reduced_skus']}个SKU, {reduction_summary['n_reduced_slots']}个仓位）", expanded=True):
            flags = reduction_summary["reduction_flags"]
            wh_count = {}
            for whs in flags.values():
                for wh in whs:
                    wh_count[wh] = wh_count.get(wh, 0) + 1
            rc1, rc2, rc3 = st.columns(3)
            rc1.metric("减仓SKU数", reduction_summary["n_reduced_skus"])
            rc2.metric("减仓仓位数", reduction_summary["n_reduced_slots"])
            rc3.metric("各仓减仓数", " / ".join(f"{wh}:{wh_count.get(wh,0)}" for wh in WAREHOUSES))
            st.caption("仅影响SKU层分仓占比。上层（SPU/一级分类/室内外/公司）使用减仓前原始数据汇总。")

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
    st.caption(
        "对某个SKU的分仓占比有业务判断时，可在此直接改。"
        "改任意一个仓，其余三个仓会按原比例自动补足，**合计始终锁定 100%**。"
    )
    st.info(
        "🔒 **四仓合计 = 100% 保障**：引擎计算后自动归一化 → 趋势调整后再次归一化 → "
        "微调时自动补足 → 自检验证偏差 < 1e-9"
    )

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
            st.caption("点击后：以你改动最多的那个仓为准，其余三仓按原比例自动补足")
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

**Q6：「减仓优化」是什么？怎么用？**

侧边栏「⑥ 减仓优化」开关，默认关闭。启用后对每个运算SKU×仓点判断：

- **月均单量 < 阈值**（默认3单）：该运算SKU在该仓的月均出单量 = 总单量 ÷ 上架月数（首次出单到数据最新月份，自动检测）
- **分仓占比 < 阈值**（默认5%）：该仓单量占该运算SKU全仓总单量的比例

**双条件同时满足**才减仓。被减仓的占比按其他仓现有比例等比分配，归一化到100%。

**影响范围**：
- ✅ 影响：SKU层分仓占比（调整后列）会被修改，导出时有「是否减仓」列备注
- ❌ 不影响：上层聚合（SPU/一级分类/室内外/全公司）仍用减仓前原始数据汇总

**为什么不看单量或占比单独判断？** 只看单量会误删大SKU的小仓（可能有几十单但占比不到5%），只看占比会误删小SKU的主仓（可能占比30%但月均不到1单）。双条件同时满足才触发，只砍真正的"僵尸仓位"。""")
