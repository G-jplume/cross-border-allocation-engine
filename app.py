"""
分仓占比计算引擎 - Streamlit Web App
基于 P1 计算引擎，提供文件上传、参数调节、结果展示、Excel导出功能。

运行方式:
    streamlit run app.py
"""
import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import io
import json
import os

from allocation_engine import AllocationEngine, WAREHOUSES

# ============================================================
# 页面配置
# ============================================================
st.set_page_config(
    page_title="分仓占比计算引擎",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ============================================================
# 初始化 session_state
# ============================================================
if "df_raw" not in st.session_state:
    st.session_state.df_raw = None
if "df_results" not in st.session_state:
    st.session_state.df_results = None
if "engine" not in st.session_state:
    st.session_state.engine = None


# ============================================================
# 标题区
# ============================================================
st.title("📦 分仓占比计算引擎")
st.markdown("上传历史出单数据 → 调节参数 → 计算各仓库发货占比 → 导出结果")


# ============================================================
# 侧边栏：参数面板
# ============================================================
st.sidebar.title("⚙️ 计算参数")

# --- 目标期 ---
st.sidebar.subheader("目标期")
target_type = st.sidebar.radio(
    "目标期类型",
    ["Q1 (1-3月)", "Q2 (4-6月)", "Q3 (7-9月)", "Q4 (10-12月)", "自定义"],
    index=0
)

quarter_map = {
    "Q1 (1-3月)": (1, 3),
    "Q2 (4-6月)": (4, 6),
    "Q3 (7-9月)": (7, 9),
    "Q4 (10-12月)": (10, 12),
}

if target_type in quarter_map:
    target_start_month, target_end_month = quarter_map[target_type]
else:
    col1, col2 = st.sidebar.columns(2)
    with col1:
        target_start_month = st.number_input("起始月", 1, 12, 1)
    with col2:
        target_end_month = st.number_input("结束月", 1, 12, 3)

target_year = st.sidebar.number_input("目标年份", 2024, 2030, 2027)

# --- 衰减参数 ---
st.sidebar.subheader("时间衰减")
lambda_val = st.sidebar.slider("衰减因子 λ", 0.50, 1.00, 0.85, 0.01,
    help="越小衰减越快，近期数据权重越高")
k_val = st.sidebar.slider("贝叶斯收缩 k", 1, 20, 6,
    help="越大越倾向基准占比，越小越信任自身数据")
a_min = st.sidebar.slider("收缩权重下限 a_min", 0.0, 0.5, 0.0, 0.05)
a_max = st.sidebar.slider("收缩权重上限 a_max", 0.5, 1.0, 0.9, 0.05)

# --- 季节因子 ---
st.sidebar.subheader("季节匹配因子")
seasonal_on = st.sidebar.checkbox("启用季节因子", value=True)
if seasonal_on:
    beta = st.sidebar.slider("季节增强因子 β", 1.0, 5.0, 3.0, 0.1,
        help="在季节窗口内的数据权重 ×β")
    seasonal_window = st.sidebar.slider("季节窗口 (±月)", 1, 3, 1,
        help="目标月前后N个月的数据获得增强")
else:
    beta = 1.0
    seasonal_window = 1

# --- 需求量 ---
st.sidebar.subheader("落货量")
demand = st.sidebar.number_input("需求总量 (件)", 100, 100000, 3000, step=100)


# ============================================================
# 主区域：文件上传
# ============================================================
st.subheader("1️⃣ 上传数据")

col_upload, col_sample = st.columns([3, 1])

with col_upload:
    uploaded_file = st.file_uploader(
        "上传 Sheet2 原始数据 (CSV 或 Excel)",
        type=["csv", "xlsx", "xls"],
        help="需要包含列: 源SKU, SPU, 室内外, 一级分类, 年, 月, 美西, 美东, 美南GA, 美南TX"
    )

with col_sample:
    use_sample = st.button("使用示例数据", help="用 v6 提取的数据做演示")

if uploaded_file is not None:
    try:
        if uploaded_file.name.endswith(".csv"):
            try:
                df = pd.read_csv(uploaded_file, encoding="utf-8-sig",
                    dtype={"源SKU": str, "SPU": str, "室内外": str, "一级分类": str, "运算SKU": str})
            except UnicodeDecodeError:
                df = pd.read_csv(uploaded_file, encoding="gbk",
                    dtype={"源SKU": str, "SPU": str, "室内外": str, "一级分类": str, "运算SKU": str})
        else:
            df = pd.read_excel(uploaded_file, dtype={"源SKU": str, "SPU": str, "室内外": str, "一级分类": str})

        st.session_state.df_raw = df
        st.success(f"✅ 上传成功！共 {len(df)} 行 × {len(df.columns)} 列")
    except Exception as e:
        st.error(f"❌ 读取失败: {e}")

elif use_sample:
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine_data")
    sample_path = os.path.join(data_dir, "sheet2_raw.csv")
    if os.path.exists(sample_path):
        df = pd.read_csv(sample_path, encoding="utf-8-sig",
            dtype={"源SKU": str, "SPU": str, "室内外": str, "一级分类": str, "运算SKU": str})
        st.session_state.df_raw = df
        st.success(f"✅ 加载示例数据！共 {len(df)} 行 × {len(df.columns)} 列")
    else:
        st.warning("示例数据文件不存在，请上传数据。")

# 显示数据预览
if st.session_state.df_raw is not None:
    with st.expander("数据预览（前10行）", expanded=False):
        st.dataframe(st.session_state.df_raw.head(10), use_container_width=True)
        st.write(f"列名: {list(st.session_state.df_raw.columns)}")


# ============================================================
# 计算按钮
# ============================================================
st.subheader("2️⃣ 开始计算")

if st.session_state.df_raw is not None:
    if st.button("🚀 开始计算", type="primary", use_container_width=True):
        with st.spinner("计算中..."):
            try:
                engine = AllocationEngine()
                engine.df_raw = st.session_state.df_raw.copy()

                # 注入参数
                anchor = int(target_year) * 12 + (target_start_month + target_end_month) // 2
                engine.params["anchor"] = anchor
                engine.params["lambda"] = lambda_val
                engine.params["k"] = k_val
                engine.params["a_min"] = a_min
                engine.params["a_max"] = a_max
                engine.params["seasonal_switch"] = 1 if seasonal_on else 0
                engine.params["seasonal_beta"] = beta
                engine.params["seasonal_window"] = seasonal_window

                # 判断目标期月份
                def in_target_period(row):
                    month_seq = row["年"] * 12 + row["月"]
                    start_seq = int(target_year) * 12 + target_start_month
                    end_seq = int(target_year) * 12 + target_end_month
                    # 目标期是年+月范围，但也匹配历史同期
                    row_month = row["月"]
                    return 1 if target_start_month <= row_month <= target_end_month else 0

                engine.df_raw["在目标期"] = engine.df_raw.apply(in_target_period, axis=1)

                # 执行6步
                engine.step1_sku_mapping()
                engine.step2_decay_weight()
                engine.step3_seasonal_factor()
                engine.step4_self_ratio()
                engine.step5_benchmark()
                df_results = engine.step6_final_ratio(demand_qty=demand)

                st.session_state.df_results = df_results
                st.session_state.engine = engine
                st.success(f"✅ 计算完成！共 {len(df_results)} 个SKU，需求总量 {demand} 件")
            except Exception as e:
                st.error(f"❌ 计算失败: {e}")
                st.exception(e)
else:
    st.info("请先上传数据或点击「使用示例数据」")


# ============================================================
# 结果展示
# ============================================================
if st.session_state.df_results is not None:
    st.subheader("3️⃣ 计算结果")
    df_r = st.session_state.df_results

    # --- KPI 卡片 ---
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("SKU 总数", f"{len(df_r)}")
    col2.metric("美西占比均值", f"{df_r['最终_美西'].mean():.1%}")
    col3.metric("美东占比均值", f"{df_r['最终_美东'].mean():.1%}")
    col4.metric("需求总量", f"{demand:,} 件")

    # --- 筛选 ---
    col_f1, col_f2 = st.columns([1, 3])

    with col_f1:
        sku_filter = st.selectbox("查看单个SKU", ["全部"] + sorted(df_r["SKU"].tolist()))

    with col_f2:
        cat_filter = st.multiselect("按品类筛选", sorted(df_r["一级分类"].unique()))

    df_show = df_r.copy()
    if sku_filter != "全部":
        df_show = df_show[df_show["SKU"] == sku_filter]
    if cat_filter:
        df_show = df_show[df_show["一级分类"].isin(cat_filter)]

    # --- 表格 ---
    display_cols = [
        "SKU", "SPU", "一级分类", "历史月数", "目标期月数", "收缩权重_a", "基准层级",
        "最终_美西", "最终_美东", "最终_美南GA", "最终_美南TX",
        "落货量_美西", "落货量_美东", "落货量_美南GA", "落货量_美南TX"
    ]
    display_cols = [c for c in display_cols if c in df_show.columns]

    st.dataframe(
        df_show[display_cols].style.format({
            "收缩权重_a": "{:.3f}",
            "最终_美西": "{:.2%}",
            "最终_美东": "{:.2%}",
            "最终_美南GA": "{:.2%}",
            "最终_美南TX": "{:.2%}",
        }),
        use_container_width=True,
        height=400
    )

    # --- 饼图 ---
    st.subheader("4️⃣ 可视化")

    chart_sku = st.selectbox(
        "选择SKU查看分仓占比",
        sorted(df_r["SKU"].tolist()),
        key="chart_sku"
    )

    if chart_sku:
        row = df_r[df_r["SKU"] == chart_sku].iloc[0]
        pie_data = pd.DataFrame({
            "仓库": WAREHOUSES,
            "占比": [row["最终_美西"], row["最终_美东"], row["最终_美南GA"], row["最终_美南TX"]],
            "落货量": [row["落货量_美西"], row["落货量_美东"], row["落货量_美南GA"], row["落货量_美南TX"]]
        })

        col_pie1, col_bar1 = st.columns(2)

        with col_pie1:
            fig_pie = px.pie(
                pie_data, values="占比", names="仓库",
                title=f"{chart_sku} 分仓占比",
                color="仓库",
                color_discrete_map={"美西": "#4e79a7", "美东": "#f28e2b", "美南GA": "#e15759", "美南TX": "#76b7b2"}
            )
            fig_pie.update_layout(font=dict(family="Microsoft YaHei, sans-serif"))
            st.plotly_chart(fig_pie, use_container_width=True)

        with col_bar1:
            fig_bar = px.bar(
                pie_data, x="仓库", y="落货量",
                title=f"{chart_sku} 各仓落货量",
                color="仓库",
                color_discrete_map={"美西": "#4e79a7", "美东": "#f28e2b", "美南GA": "#e15759", "美南TX": "#76b7b2"},
                text="落货量"
            )
            fig_bar.update_layout(font=dict(family="Microsoft YaHei, sans-serif"))
            st.plotly_chart(fig_bar, use_container_width=True)

    # --- 全SKU堆叠柱状图 ---
    st.markdown("**全SKU分仓占比堆叠图**（前50个SKU）")
    df_top = df_r.head(50)
    fig_stack = go.Figure()
    colors = {"美西": "#4e79a7", "美东": "#f28e2b", "美南GA": "#e15759", "美南TX": "#76b7b2"}
    for wh in WAREHOUSES:
        fig_stack.add_trace(go.Bar(
            x=df_top["SKU"],
            y=df_top[f"最终_{wh}"],
            name=wh,
            marker_color=colors[wh]
        ))
    fig_stack.update_layout(
        barmode="stack",
        title="各SKU分仓占比（前50）",
        xaxis_title="SKU",
        yaxis_title="占比",
        yaxis=dict(tickformat=".0%"),
        font=dict(family="Microsoft YaHei, sans-serif"),
        height=400
    )
    st.plotly_chart(fig_stack, use_container_width=True)

    # --- 导出 ---
    st.subheader("5️⃣ 导出结果")

    col_exp1, col_exp2 = st.columns([1, 3])

    with col_exp1:
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df_r.to_excel(writer, index=False, sheet_name="分仓结果")

        st.download_button(
            label="📥 下载 Excel 结果",
            data=output.getvalue(),
            file_name=f"分仓占比结果_{demand}件.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

    with col_exp2:
        csv_output = df_r.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            label="📥 下载 CSV 结果",
            data=csv_output,
            file_name=f"分仓占比结果_{demand}件.csv",
            mime="text/csv",
            use_container_width=True
        )


# ============================================================
# 页脚
# ============================================================
st.markdown("---")
st.markdown(
    "📦 **分仓占比计算引擎** | "
    "6步链路: 运算SKU映射 → 时间衰减 → 季节因子 → 自身占比 → 基准回退 → 贝叶斯收缩+落货量"
)
