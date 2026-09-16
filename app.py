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
st.sidebar.title("计算参数")

# --- 目标期 ---
st.sidebar.subheader("目标发货月份")
st.sidebar.caption("选择要发哪个月的货，系统会优先参考同月份的历史分仓数据")

col_m1, col_m2 = st.sidebar.columns(2)
with col_m1:
    target_start_month = st.number_input("起始月", 1, 12, 1)
with col_m2:
    target_end_month = st.number_input("结束月", 1, 12, 3)

target_year = st.sidebar.number_input("目标年份", 2024, 2030, 2027)

# --- 衰减参数 ---
st.sidebar.subheader("时间衰减")
st.sidebar.caption("控制历史数据的影响力：越近的数据权重越高，越远的数据越被淡化")

lambda_val = st.sidebar.slider("衰减速度 λ", 0.50, 1.00, 0.85, 0.01,
    help="0.85=每月衰减15%。越小=越看重近期数据，1.0=不衰减（所有月份同等对待）")
k_val = st.sidebar.slider("收缩强度 k", 1, 20, 6,
    help="控制自身历史数据和行业基准的混合比例。k越大=越信任行业基准，k越小=越信任该SKU自身数据")
a_min = st.sidebar.slider("收缩权重下限", 0.0, 0.5, 0.0, 0.05,
    help="自身数据的最低权重占比。0=新品完全用基准，0.3=至少30%用自身数据")
a_max = st.sidebar.slider("收缩权重上限", 0.5, 1.0, 0.9, 0.05,
    help="自身数据的最高权重占比。0.9=最多90%用自身数据，1.0=完全用自身数据（不推荐）")

# --- 季节因子 ---
st.sidebar.subheader("季节匹配因子")
st.sidebar.caption("对季节性品类，增强同季节历史数据的权重（如5月发货则4-6月数据加权）")

seasonal_on = st.sidebar.checkbox("启用季节因子", value=True)
if seasonal_on:
    beta = st.sidebar.slider("季节增强倍数 β", 1.0, 5.0, 3.0, 0.1,
        help="同季节数据权重乘以此倍数。3.0=同季节数据权重×3，1.0=不增强")
    seasonal_window = st.sidebar.slider("季节窗口范围", 1, 3, 1,
        help="目标月前后N个月算同季节。1=前后1个月，3=前后3个月")
else:
    beta = 1.0
    seasonal_window = 1


# ============================================================
# 主区域：文件上传 + 下载模板
# ============================================================
st.subheader("1. 上传数据")

col_upload, col_sample, col_tpl = st.columns([3, 1, 1])

with col_upload:
    uploaded_file = st.file_uploader(
        "上传原始出单数据 (CSV 或 Excel)",
        type=["csv", "xlsx", "xls"],
        help="需要包含列: 源SKU, SPU, 室内外, 一级分类, 年, 月, 美西, 美东, 美南GA, 美南TX"
    )

with col_sample:
    use_sample = st.button("使用示例数据", help="用提取的数据做演示")

with col_tpl:
    # 下载模板
    tpl_cols = ["源SKU", "SPU", "室内外", "一级分类", "年", "月", "美西", "美东", "美南GA", "美南TX"]
    tpl_df = pd.DataFrame(columns=tpl_cols)
    tpl_csv = tpl_df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        label="下载模板",
        data=tpl_csv,
        file_name="原始数据模板.csv",
        mime="text/csv",
        help="下载CSV模板查看需要哪些列"
    )

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
        st.success(f"上传成功！共 {len(df)} 行 x {len(df.columns)} 列")
    except Exception as e:
        st.error(f"读取失败: {e}")

elif use_sample:
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine_data")
    sample_path = os.path.join(data_dir, "sheet2_raw.csv")
    if os.path.exists(sample_path):
        df = pd.read_csv(sample_path, encoding="utf-8-sig",
            dtype={"源SKU": str, "SPU": str, "室内外": str, "一级分类": str, "运算SKU": str})
        st.session_state.df_raw = df
        st.success(f"加载示例数据！共 {len(df)} 行 x {len(df.columns)} 列")
    else:
        st.warning("示例数据文件不存在，请上传数据。")

# --- 可选：上传映射表和基准表 ---
with st.expander("可选：上传混用SKU映射表 / 基准表", expanded=False):
    col_m, col_b = st.columns(2)
    with col_m:
        mix_file = st.file_uploader("混用SKU映射表 (JSON)", type=["json"], key="mix_upload",
            help="没有映射表也可以计算，未映射的SKU自动取'-'前部分")
        mix_tpl = json.dumps([{"源SKU": "示例SKU-1", "相似SKU": "示例SKU"}], ensure_ascii=False, indent=2).encode("utf-8")
        st.download_button(
            label="下载映射表模板",
            data=mix_tpl,
            file_name="混用SKU映射表模板.json",
            mime="application/json",
            key="mix_tpl_dl"
        )
    with col_b:
        bench_file = st.file_uploader("基准表 (JSON)", type=["json"], key="bench_upload",
            help="没有基准表也可以计算，系统会自动从上传数据计算全公司基准")
        bench_tpl = json.dumps({
            "室内外": [{"label": "室内", "观测数": 0, "收缩权重": 0, "基准_美西": 0.25, "基准_美东": 0.25, "基准_美南GA": 0.25, "基准_美南TX": 0.25}],
            "一级分类": [{"label": "庭院、草坪与花园", "观测数": 0, "收缩权重": 0, "基准_美西": 0.25, "基准_美东": 0.25, "基准_美南GA": 0.25, "基准_美南TX": 0.25}],
            "SPU": [{"label": "示例SPU", "观测数": 0, "收缩权重": 0, "基准_美西": 0.25, "基准_美东": 0.25, "基准_美南GA": 0.25, "基准_美南TX": 0.25}]
        }, ensure_ascii=False, indent=2).encode("utf-8")
        st.download_button(
            label="下载基准表模板",
            data=bench_tpl,
            file_name="基准表模板.json",
            mime="application/json",
            key="bench_tpl_dl"
        )

    if mix_file is not None:
        try:
            st.session_state.mix_mapping = json.load(mix_file)
            st.caption(f"映射表已加载: {len(st.session_state.mix_mapping)} 条")
        except Exception as e:
            st.error(f"映射表读取失败: {e}")

    if bench_file is not None:
        try:
            st.session_state.benchmarks = json.load(bench_file)
            st.caption(f"基准表已加载")
        except Exception as e:
            st.error(f"基准表读取失败: {e}")

# 显示数据预览
if st.session_state.df_raw is not None:
    with st.expander("数据预览（前10行）", expanded=False):
        st.dataframe(st.session_state.df_raw.head(10), use_container_width=True)
        st.write(f"列名: {list(st.session_state.df_raw.columns)}")


# ============================================================
# 计算按钮
# ============================================================
st.subheader("2. 开始计算")

if st.session_state.df_raw is not None:
    if st.button("开始计算", type="primary", use_container_width=True):
        with st.spinner("计算中..."):
            try:
                engine = AllocationEngine()
                engine.df_raw = st.session_state.df_raw.copy()

                # 注入上传的映射表和基准表
                if "mix_mapping" in st.session_state:
                    mapping_list = st.session_state.mix_mapping
                    engine.mix_mapping = {item["源SKU"]: item["相似SKU"] for item in mapping_list}
                if "benchmarks" in st.session_state:
                    engine.benchmarks = st.session_state.benchmarks

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
                    row_month = row["月"]
                    return 1 if target_start_month <= row_month <= target_end_month else 0

                engine.df_raw["在目标期"] = engine.df_raw.apply(in_target_period, axis=1)

                # 执行6步（不需要需求量，只算占比）
                engine.step1_sku_mapping()
                engine.step2_decay_weight()
                engine.step3_seasonal_factor()
                engine.step4_self_ratio()
                engine.step5_benchmark()
                df_results = engine.step6_final_ratio(demand_qty=1)  # demand=1 → 落货量=占比比例

                # 删除落货量列，只保留占比
                drop_cols = [c for c in df_results.columns if c.startswith("落货量")]
                df_results = df_results.drop(columns=drop_cols)

                st.session_state.df_results = df_results
                st.session_state.engine = engine
                st.success(f"计算完成！共 {len(df_results)} 个SKU")
            except Exception as e:
                st.error(f"计算失败: {e}")
                st.exception(e)
else:
    st.info("请先上传数据或点击「使用示例数据」")


# ============================================================
# 结果展示
# ============================================================
if st.session_state.df_results is not None:
    st.subheader("3. 计算结果")
    df_r = st.session_state.df_results

    # --- KPI 卡片 ---
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("SKU 总数", f"{len(df_r)}")
    col2.metric("美西占比均值", f"{df_r['最终_美西'].mean():.1%}")
    col3.metric("美东占比均值", f"{df_r['最终_美东'].mean():.1%}")
    col4.metric("美南GA占比均值", f"{df_r['最终_美南GA'].mean():.1%}")

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
        "最终_美西", "最终_美东", "最终_美南GA", "最终_美南TX"
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
    st.subheader("4. 可视化")

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
                pie_data, x="仓库", y="占比",
                title=f"{chart_sku} 各仓占比",
                color="仓库",
                color_discrete_map={"美西": "#4e79a7", "美东": "#f28e2b", "美南GA": "#e15759", "美南TX": "#76b7b2"},
                text="占比"
            )
            fig_bar.update_layout(
                font=dict(family="Microsoft YaHei, sans-serif"),
                yaxis=dict(tickformat=".1%")
            )
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
    st.subheader("5. 导出结果")

    col_exp1, col_exp2 = st.columns([1, 1])

    with col_exp1:
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df_r.to_excel(writer, index=False, sheet_name="分仓占比")

        st.download_button(
            label="下载 Excel 结果",
            data=output.getvalue(),
            file_name="分仓占比结果.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

    with col_exp2:
        csv_output = df_r.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            label="下载 CSV 结果",
            data=csv_output,
            file_name="分仓占比结果.csv",
            mime="text/csv",
            use_container_width=True
        )


# ============================================================
# 页脚
# ============================================================
st.markdown("---")
st.markdown(
    "**分仓占比计算引擎** | "
    "6步链路: 运算SKU映射 → 时间衰减 → 季节因子 → 自身占比 → 基准回退 → 贝叶斯收缩"
)
