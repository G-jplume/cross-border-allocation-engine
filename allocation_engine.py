"""
分仓占比计算引擎 - 分仓占比计算核心
纯Python实现计算链路，与v6 Excel结果对比验证。

计算链路（5步）:
1. 运算SKU映射       (VLOOKUP + TEXTBEFORE fallback)
2. 时间衰减加权       (w = λ^|anchor - month_seq|, 含月份相似度)
3. 自身占比           (含季节品类分支 + SKU级集中度自动检测, SUMIFS by 运算SKU / SUMIFS total)
4. 基准占比           (SPU → 一级分类 → 室内外 → 全公司 回退 + 品类层贝叶斯收缩)
5. 最终占比           (a×自身 + (1-a)×基准) + 趋势调整(α, 上限cap) + 归一化 + 落货量

参数生效层级说明:
┌────────────────────┬──────────────┬──────────────────────────────┐
│ 参数               │ 作用层级     │ 说明                          │
├────────────────────┼──────────────┼──────────────────────────────┤
│ lambda 衰减速度    │ 原始数据加权 │ 全局，所有品类                │
│ seasonal_* 季节因子│ 原始数据加权 │ 仅适用品类，与衰减相乘        │
│ conc 集中度阈值    │ 自身占比     │ SKU级自动检测强/弱季节        │
│ alpha_trend 趋势α  │ 最终占比     │ 归一化前叠加趋势差            │
│ trend_cap 调整上限 │ 最终占比     │ 单仓趋势调整幅度上限          │
│ k 收缩强度         │ SKU 层混合   │ 自身 vs 基准                  │
│ k_cat 观测数       │ 基准层混合   │ 子层 vs 父层                  │
└────────────────────┴──────────────┴──────────────────────────────┘
"""
import pandas as pd
import numpy as np
import json
import os

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ENGINE_DIR, "engine_data")

WAREHOUSES = ["美西", "美东", "美南GA", "美南TX"]


class AllocationEngine:
    """分仓占比计算引擎"""

    def __init__(self, data_dir=DATA_DIR):
        self.data_dir = data_dir
        self.params = self._load_params()
        self.new_product_threshold = 2
        self.mix_mapping = self._load_mix_mapping()
        self.df_raw = None
        self.sku_weighted = None
        self.sku_benchmarks = None
        self.benchmarks = self._load_benchmarks()
        # 品类层最小等效观测数（贝叶斯收缩虚拟样本量）
        self.k_cat = float(self.params.get("k_cat", 12.0))
        # 季节适用品类（None 表示使用 params 里的默认值）
        self.seasonal_categories = None
        # SKU级集中度自动检测阈值
        self.concentration_threshold_high = float(self.params.get("concentration_threshold_high", 0.80))
        self.concentration_threshold_low = float(self.params.get("concentration_threshold_low", 0.20))
        # 趋势窗口（None 表示按 anchor 自动推导）
        self.trend_recent = None
        self.trend_far = None

    # 默认参数（文件不存在时使用）
    DEFAULT_PARAMS = {
        "anchor": 24325, "lambda": 0.85, "k": 8.0,
        "a_min": 0.0, "a_max": 0.9,
        "target_start": 1, "target_end": 3,
        "alpha_trend": 0.3, "trend_cap": 0.05,
        "trend_min_orders": 50,
        "recent_start": None, "recent_end": None,
        "far_start": None, "far_end": None,
        "seasonal_categories": "庭院、草坪与花园,庭院",
        "lambda_same_month": 0.95,
        "new_product_min_orders": 20,
        "k_cat": 12.0,
        "concentration_threshold_high": 0.70,
        "concentration_threshold_low": 0.20,
    }

    def _load_params(self):
        path = os.path.join(self.data_dir, "params.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                p = json.load(f)
            merged = dict(self.DEFAULT_PARAMS)
            merged.update(p)
            return merged
        return dict(self.DEFAULT_PARAMS)

    def _load_mix_mapping(self):
        path = os.path.join(self.data_dir, "mix_sku_mapping.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                mapping_list = json.load(f)
            return {item["源SKU"]: item["相似SKU"] for item in mapping_list}
        return {}

    def _load_benchmarks(self):
        path = os.path.join(self.data_dir, "sheet4_benchmarks.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"室内外": [], "一级分类": [], "SPU": []}

    def load_data(self):
        self.df_raw = pd.read_csv(
            os.path.join(self.data_dir, "sheet2_raw.csv"),
            encoding="utf-8-sig",
            dtype={"源SKU": str, "SPU": str, "室内外": str, "一级分类": str, "运算SKU": str}
        )
        with open(os.path.join(self.data_dir, "sheet4_benchmarks.json"), "r", encoding="utf-8") as f:
            self.benchmarks = json.load(f)
        print(f"Loaded {len(self.df_raw)} rows from Sheet2")
        return self.df_raw

    # =========================================================
    # 辅助：目标期 / 季节窗口 / 趋势窗口
    # =========================================================
    def _get_seasonal_categories(self):
        """返回季节适用的品类列表。优先用外部注入，其次用 params 配置。"""
        if self.seasonal_categories is not None:
            return list(self.seasonal_categories)
        raw = self.params.get("seasonal_categories", "")
        if isinstance(raw, (list, tuple)):
            return [str(c) for c in raw]
        return [c.strip() for c in str(raw).split(",") if c.strip()]

    def _get_anchor_month(self):
        anchor = int(self.params["anchor"])
        return (anchor - 1) % 12 + 1

    def _get_target_months(self):
        """目标期包含的月份列表（1-12），支持跨年。

        优先从 params 的 target_start/target_end 计算，
        兜底：从"在目标期"列提取，再兜底：返回锚点月份。
        """
        ts = self.params.get("target_start")
        te = self.params.get("target_end")
        if ts and te:
            ts, te = int(ts), int(te)
            if ts <= te:
                return list(range(ts, te + 1))
            else:
                # 跨年：如 11→2 = [11,12,1,2]
                return list(range(ts, 13)) + list(range(1, te + 1))
        if self.df_raw is not None and "在目标期" in self.df_raw.columns:
            tm = sorted(self.df_raw.loc[self.df_raw["在目标期"] == 1, "月"].dropna().unique())
            if len(tm) > 0:
                return [int(m) for m in tm]
        return [self._get_anchor_month()]

    def _resolve_trend_windows(self):
        """推导趋势的近期/远期月份序号区间。

        默认策略：以目标期（月数宽度 w）为基准，取
          近期 = 目标期向前平移 1 年
          远期 = 目标期向前平移 2 年
        返回 (recent_lo, recent_hi, far_lo, far_hi) 或 None。
        """
        if self.trend_recent is not None and self.trend_far is not None:
            return (*self.trend_recent, *self.trend_far)

        rs, re_ = self.params.get("recent_start"), self.params.get("recent_end")
        fs, fe = self.params.get("far_start"), self.params.get("far_end")
        if rs and re_ and fs and fe:
            return int(rs), int(re_), int(fs), int(fe)

        # 自动推导：用"在目标期"列的月份序号区间（支持跨年）
        if self.df_raw is not None and "在目标期" in self.df_raw.columns:
            in_tp = self.df_raw[self.df_raw["在目标期"] == 1]
            if "月份序号_py" in in_tp.columns and len(in_tp) > 0:
                target_lo = int(in_tp["月份序号_py"].min())
                target_hi = int(in_tp["月份序号_py"].max())
            else:
                months = self._get_target_months()
                if not months:
                    return None
                anchor = int(self.params["anchor"])
                width = len(months)
                target_lo = anchor - width + 1
                target_hi = anchor
        else:
            months = self._get_target_months()
            if not months:
                return None
            anchor = int(self.params["anchor"])
            width = len(months)
            target_lo = anchor - width + 1
            target_hi = anchor

        recent_lo, recent_hi = target_lo - 12, target_hi - 12
        far_lo, far_hi = target_lo - 24, target_hi - 24
        return recent_lo, recent_hi, far_lo, far_hi

    # =========================================================
    # Step 1: 运算SKU映射
    # =========================================================
    def step1_sku_mapping(self):
        df = self.df_raw.copy()

        def map_sku(src_sku):
            src = str(src_sku).strip()
            if src in self.mix_mapping:
                return self.mix_mapping[src]
            if "-" in src:
                return src.split("-")[0]
            return src

        df["运算SKU_py"] = df["源SKU"].apply(map_sku)

        if "运算SKU" in df.columns:
            mismatches = df[df["运算SKU_py"] != df["运算SKU"]]
            print(f"  Step1: {len(df)} rows, mismatches vs Excel: {len(mismatches)}")
        self.df_raw = df
        return df

    # =========================================================
    # Step 2: 时间衰减加权
    # 优化：建议1 - 按品类分组，强季节品类限制目标期，弱季节品类放开全部月份
    # 优化：建议2 - 同月跨年用 λ_same_month (0.95)，非同月用 λ (0.85)
    # =========================================================
    def step2_decay_weight(self):
        df = self.df_raw.copy()
        anchor = self.params["anchor"]
        lambda_val = self.params["lambda"]
        lambda_same = float(self.params.get("lambda_same_month", 0.95))
        cats = self._get_seasonal_categories()

        df["月份序号_py"] = df["年"] * 12 + df["月"]
        dist = anchor - df["月份序号_py"]
        in_target = df["在目标期"] == 1 if "在目标期" in df.columns else pd.Series(True, index=df.index)

        # 判断是否为同月（月份编号在目标期月份列表中）
        target_months = self._get_target_months()
        is_same_month = df["月"].apply(lambda m: int(m) in target_months)

        # 月份相似度权重：同月=1.0，相邻月=0.6，隔2月=0.3，隔3月以上=0.1
        def month_similarity(m, target_mths):
            m = int(m)
            best = 0.1
            for tm in target_mths:
                d = abs(m - tm)
                d = min(d, 12 - d)  # 环形距离
                if d == 0:
                    return 1.0
                elif d == 1:
                    best = max(best, 0.6)
                elif d == 2:
                    best = max(best, 0.3)
            return best

        month_sim = df["月"].apply(lambda m: month_similarity(m, target_months))

        # 判断是否为强季节品类
        is_seasonal_cat = df["一级分类"].isin(cats) if "一级分类" in df.columns else pd.Series(False, index=df.index)

        # 时间衰减 × 月份相似度（所有品类统一公式，品类分支在 step4 实现）
        # 同月 → λ_same^dist × 1.0
        # 非同月 → λ^dist × month_sim
        time_decay = np.where(
            is_same_month,
            lambda_same ** dist.clip(lower=0),
            lambda_val ** dist.clip(lower=0)
        )
        decay_weight = time_decay * month_sim
        df["衰减权重_py"] = decay_weight

        if "在目标期" in df.columns:
            df["在目标期_py"] = df["在目标期"]

        n_seasonal_same = int((is_seasonal_cat & is_same_month & (decay_weight > 0)).sum())
        n_seasonal_fallback = int((is_seasonal_cat & ~is_same_month & (decay_weight > 0)).sum())
        n_weak_open = int((~is_seasonal_cat & ~is_same_month & (decay_weight > 0)).sum())
        print(f"  Step2: strong-season same-month={n_seasonal_same} (λ_same={lambda_same}), "
              f"strong-season fallback={n_seasonal_fallback} (λ={lambda_val}), "
              f"weak-season rows={n_weak_open} (λ={lambda_val})")

        self.df_raw = df
        return df

    # =========================================================
    # Step 3: 自身占比（含季节品类分支）
    # =========================================================
    def _add_weighted_cols(self):
        """加权列 = 衰减权重 × 原始量。"""
        df = self.df_raw
        for wh in WAREHOUSES:
            df[f"加权_{wh}_py"] = df["衰减权重_py"] * df[wh]
        df["加权合计_py"] = df["衰减权重_py"] * df[WAREHOUSES].sum(axis=1)
        self.df_raw = df
        return df

    def step3_self_ratio(self):
        df = self._add_weighted_cols()
        target_months = self._get_target_months()
        cats = self._get_seasonal_categories()

        is_same_month = df["月"].apply(lambda m: int(m) in target_months)
        same_month_df = df[is_same_month].copy()
        all_grouped = dict(tuple(df.groupby("运算SKU_py")))
        same_groups = dict(tuple(same_month_df.groupby("运算SKU_py")))

        high_thresh = self.concentration_threshold_high
        low_thresh = self.concentration_threshold_low

        sku_weighted = {}
        n_strong_same = 0
        n_strong_fallback = 0
        n_weak_all = 0
        n_auto_strong = 0
        n_auto_weak = 0

        for sku, all_rows in all_grouped.items():
            same_rows = same_groups.get(sku, pd.DataFrame())
            same_total = same_rows["加权合计_py"].sum() if len(same_rows) > 0 else 0

            same_raw = int(same_rows[WAREHOUSES].sum(axis=1).sum()) if len(same_rows) > 0 else 0
            all_raw = int(all_rows[WAREHOUSES].sum(axis=1).sum())
            concentration = same_raw / all_raw if all_raw > 0 else 0.0

            cat_is_seasonal = False
            if "一级分类" in all_rows.columns and len(all_rows) > 0:
                cat_val = str(all_rows.iloc[0]["一级分类"]).strip()
                cat_is_seasonal = cat_val in cats

            if concentration >= high_thresh:
                is_seasonal = True
                seasonality_source = "自动-高集中度"
                n_auto_strong += 1
            elif concentration <= low_thresh:
                is_seasonal = False
                seasonality_source = "自动-低集中度"
                n_auto_weak += 1
            else:
                is_seasonal = cat_is_seasonal
                seasonality_source = "品类" if cat_is_seasonal else "弱季节"

            if is_seasonal:
                if same_total > 0:
                    group = same_rows
                    n_strong_same += 1
                    used_months = len(same_rows[same_rows[WAREHOUSES].sum(axis=1) > 0])
                else:
                    fallback_total = all_rows["加权合计_py"].sum()
                    if fallback_total > 0:
                        group = all_rows
                        n_strong_fallback += 1
                        used_months = len(all_rows[all_rows[WAREHOUSES].sum(axis=1) > 0])
                    else:
                        continue
            else:
                fallback_total = all_rows["加权合计_py"].sum()
                if fallback_total > 0:
                    group = all_rows
                    n_weak_all += 1
                    used_months = len(all_rows[all_rows[WAREHOUSES].sum(axis=1) > 0])
                else:
                    continue

            total = group["加权合计_py"].sum()
            wh_sums = {wh: group[f"加权_{wh}_py"].sum() for wh in WAREHOUSES}
            raw_total = int(group[WAREHOUSES].sum(axis=1).sum())
            sku_weighted[sku] = {
                "total": total,
                "raw_total": raw_total,
                "wh_sums": wh_sums,
                "self_ratios": {wh: wh_sums[wh] / total for wh in WAREHOUSES},
                "used_months": used_months,
                "is_seasonal": is_seasonal,
                "concentration": concentration,
                "seasonality_source": seasonality_source,
            }

        for sku in sku_weighted:
            all_rows = all_grouped[sku]
            sku_weighted[sku]["history_months"] = len(all_rows[all_rows[WAREHOUSES].sum(axis=1) > 0])
            same_rows = same_groups.get(sku, pd.DataFrame())
            sku_weighted[sku]["target_months"] = len(same_rows[same_rows[WAREHOUSES].sum(axis=1) > 0]) if len(same_rows) > 0 else 0

        print(f"  Step3: {len(sku_weighted)} SKUs "
              f"(strong-seasonal same-month: {n_strong_same}, "
              f"strong-seasonal fallback: {n_strong_fallback}, "
              f"weak-seasonal all-months: {n_weak_all}, "
              f"auto-strong: {n_auto_strong}, auto-weak: {n_auto_weak}, "
              f"target months: {target_months})")
        self.sku_weighted = sku_weighted
        return sku_weighted

    # =========================================================
    # Step 4: 基准占比（含品类层贝叶斯收缩）
    # =========================================================
    def _shrink(self, child_ratios, child_n, parent_ratios, k_cat):
        """贝叶斯收缩：child_n/(child_n+k)×子层 + k/(child_n+k)×父层。

        k_cat 即"最小等效观测数"——把父层均值的可信度折算成 k_cat 个虚拟观测。
        k_cat 越大越保守（子层样本少时被拉向父层）。
        """
        if child_n <= 0:
            return dict(parent_ratios)
        w = child_n / (child_n + k_cat)
        return {wh: w * child_ratios.get(wh, 0.0) + (1 - w) * parent_ratios.get(wh, 0.0)
                for wh in WAREHOUSES}

    def step4_benchmark(self):
        k_cat = float(self.k_cat)
        spu_bm = {bm["label"]: bm for bm in self.benchmarks.get("SPU", [])}
        cat_bm = {bm["label"]: bm for bm in self.benchmarks.get("一级分类", [])}
        indoor_bm = {bm["label"]: bm for bm in self.benchmarks.get("室内外", [])}

        all_target = self.df_raw[self.df_raw["在目标期_py"] == 1]
        overall_total = all_target[WAREHOUSES].sum(axis=1).sum()
        overall_ratios = ({wh: all_target[wh].sum() / overall_total for wh in WAREHOUSES}
                          if overall_total > 0 else {wh: 0.25 for wh in WAREHOUSES})

        df = self.df_raw
        df_by_sku = dict(tuple(df.groupby("运算SKU_py")))
        sku_info = {}
        for sku in self.sku_weighted:
            if sku not in df_by_sku:
                continue
            rows = df_by_sku[sku]
            sku_info[sku] = {
                "SPU": rows["SPU"].iloc[0],
                "一级分类": rows["一级分类"].iloc[0],
                "室内外": rows["室内外"].iloc[0],
            }

        def compute_group_obs(group_col):
            """按分组的原始观测占比 + 观测数（不做收缩）。"""
            grouped = all_target.groupby(group_col)
            bm = {}
            for label, group in grouped:
                wh_sums = {wh: group[wh].sum() for wh in WAREHOUSES}
                total = sum(wh_sums.values())
                if total == 0:
                    continue
                bm[label] = {
                    "观测数": len(group),
                    "观测占比": {wh: wh_sums[wh] / total for wh in WAREHOUSES},
                }
            return bm

        spu_obs = compute_group_obs("SPU")
        cat_obs = compute_group_obs("一级分类")
        indoor_obs = compute_group_obs("室内外")

        # --- 建立收缩后的基准表 ---
        # 一级分类基准 = shrink(品类观测, n, 全公司)
        cat_shrunk = {}
        for label, o in cat_obs.items():
            cat_shrunk[label] = {
                "观测数": o["观测数"],
                "基准": self._shrink(o["观测占比"], o["观测数"], overall_ratios, k_cat),
            }
        # 室内外基准 = shrink(室内外观测, n, 全公司)
        indoor_shrunk = {}
        for label, o in indoor_obs.items():
            indoor_shrunk[label] = {
                "观测数": o["观测数"],
                "基准": self._shrink(o["观测占比"], o["观测数"], overall_ratios, k_cat),
            }
        # SPU 基准 = shrink(SPU观测, n, 所属一级分类收缩后基准)
        spu_to_cat = {}
        for label, group in all_target.groupby("SPU"):
            if len(group) > 0:
                spu_to_cat[label] = group["一级分类"].iloc[0]
        spu_shrunk = {}
        for label, o in spu_obs.items():
            pc = spu_to_cat.get(label)
            parent = cat_shrunk.get(pc, {}).get("基准", overall_ratios) if pc else overall_ratios
            spu_shrunk[label] = {
                "观测数": o["观测数"],
                "基准": self._shrink(o["观测占比"], o["观测数"], parent, k_cat),
                "父层": pc or "全公司",
            }

        sku_benchmarks = {}
        for sku, info in sku_info.items():
            spu = info["SPU"]
            cat1 = info["一级分类"]
            indoor = info["室内外"]
            used_level = "全公司"
            bm_ratios = dict(overall_ratios)
            bm_n = overall_total
            bm_parent = "—"

            if spu and spu in spu_bm and spu_bm[spu].get("观测数", 0) > 0:
                bm = spu_bm[spu]
                bm_ratios = {wh: bm[f"基准_{wh}"] for wh in WAREHOUSES}
                used_level = "SPU(预存)"
                bm_n = bm.get("观测数")
            elif spu and spu in spu_shrunk:
                bm_ratios = spu_shrunk[spu]["基准"]
                bm_n = spu_shrunk[spu]["观测数"]
                bm_parent = spu_shrunk[spu]["父层"]
                used_level = "SPU"
            elif cat1 and cat1 in cat_bm and cat_bm[cat1].get("观测数", 0) > 0:
                bm = cat_bm[cat1]
                bm_ratios = {wh: bm[f"基准_{wh}"] for wh in WAREHOUSES}
                used_level = "一级分类(预存)"
                bm_n = bm.get("观测数")
            elif cat1 and cat1 in cat_shrunk:
                bm_ratios = cat_shrunk[cat1]["基准"]
                bm_n = cat_shrunk[cat1]["观测数"]
                bm_parent = "全公司"
                used_level = "一级分类"
            elif indoor and indoor in indoor_bm and indoor_bm[indoor].get("观测数", 0) > 0:
                bm = indoor_bm[indoor]
                bm_ratios = {wh: bm[f"基准_{wh}"] for wh in WAREHOUSES}
                used_level = "室内外(预存)"
                bm_n = bm.get("观测数")
            elif indoor and indoor in indoor_shrunk:
                bm_ratios = indoor_shrunk[indoor]["基准"]
                bm_n = indoor_shrunk[indoor]["观测数"]
                bm_parent = "全公司"
                used_level = "室内外"

            sku_benchmarks[sku] = {
                "benchmark": bm_ratios,
                "level": used_level,
                "info": info,
                "benchmark_n": bm_n,
                "benchmark_parent": bm_parent,
            }

        print(f"  Step5: computed benchmarks for {len(sku_benchmarks)} SKUs "
              f"(k_cat={k_cat:g} 品类层收缩)")
        self.sku_benchmarks = sku_benchmarks
        self._cat_shrunk = cat_shrunk
        self._spu_shrunk = spu_shrunk
        self._indoor_shrunk = indoor_shrunk
        return sku_benchmarks

    # =========================================================
    # Step 5: 趋势调整 + 最终占比
    # 优化：建议4 - 趋势因子改用原始单量占比（不加衰减权重和季节因子）
    # 优化：建议5 - 新品增加订单量门槛（history≤阈值 或 目标期总单量<门槛）
    # =========================================================
    def step5_final_ratio(self, demand_qty=1, apply_trend=True):
        k = self.params["k"]
        a_min = self.params["a_min"]
        a_max = self.params["a_max"]
        new_threshold = self.new_product_threshold
        min_orders = int(self.params.get("new_product_min_orders", 10))
        alpha = float(self.params.get("alpha_trend", 0.3) or 0.0)
        cap = float(self.params.get("trend_cap", 0.05) or 0.0)
        trend_min_orders = int(self.params.get("trend_min_orders", 30))

        df = self.df_raw
        windows = self._resolve_trend_windows() if apply_trend and alpha > 0 else None
        trend_used = windows is not None

        if trend_used:
            r_lo, r_hi, f_lo, f_hi = windows
            print(f"  Step6: trend windows recent=[{r_lo},{r_hi}] far=[{f_lo},{f_hi}] "
                  f"α={alpha} cap=±{cap} min_orders={trend_min_orders} (raw data, dynamic α)")
        else:
            print(f"  Step6: trend disabled (α={alpha})")

        # 预先按 运算SKU 建索引，避免循环里反复切片
        df_by_sku = dict(tuple(df.groupby("运算SKU_py")))

        results = []
        for sku in self.sku_weighted:
            sw = self.sku_weighted[sku]
            sb = self.sku_benchmarks.get(
                sku,
                {"benchmark": {wh: 0.25 for wh in WAREHOUSES}, "level": "全公司", "info": {}}
            )

            n = sw.get("used_months", sw["target_months"])
            history = sw["history_months"]
            target_orders = int(sw.get("raw_total", sw.get("total", 0)))

            # 新品判定 = 历史月数≤阈值 OR 目标期原始总单量<门槛
            is_new = (history <= new_threshold) or (target_orders < min_orders)

            if is_new:
                a = 0.0
            else:
                a_raw = n / (n + k) if (n + k) > 0 else 0
                a = min(a_max, max(a_min, a_raw))

            final = {wh: a * sw["self_ratios"][wh] + (1 - a) * sb["benchmark"][wh]
                     for wh in WAREHOUSES}

            # 归一化（保证四仓合计 = 1）
            total_final = sum(final.values())
            if total_final > 0:
                final = {wh: v / total_final for wh, v in final.items()}

            # --- 趋势调整（用原始单量占比，加单量门槛+动态α） ---
            trend_diff = {wh: 0.0 for wh in WAREHOUSES}
            adjusted = dict(final)
            trend_alpha = 0.0  # 动态α，按数据量调整
            if trend_used and sku in df_by_sku:
                g = df_by_sku[sku]
                rec = g[g["月份序号_py"].between(r_lo, r_hi)]
                far = g[g["月份序号_py"].between(f_lo, f_hi)]
                # 用原始仓点量而非加权量
                rec_total = rec[WAREHOUSES].sum(axis=1).sum() if len(rec) else 0
                far_total = far[WAREHOUSES].sum(axis=1).sum() if len(far) else 0
                trend_total = rec_total + far_total
                # 单量门槛：去年+前年同期总单量 < 门槛 → 跳过趋势
                # 同时确保近期和远期各有数据，避免除零
                if trend_total >= trend_min_orders and rec_total > 0 and far_total > 0:
                    # 动态α：>100单用原始α，门槛-100单用α/2，<门槛不生效
                    if trend_total >= 100:
                        trend_alpha = alpha
                    else:
                        trend_alpha = alpha * 0.5
                    for wh in WAREHOUSES:
                        rr = rec[wh].sum() / rec_total
                        fr = far[wh].sum() / far_total
                        d = rr - fr
                        trend_diff[wh] = d
                        adjusted[wh] = final[wh] + trend_alpha * max(-cap, min(cap, d))
                    adj_total = sum(adjusted.values())
                    if adj_total > 0:
                        adjusted = {wh: v / adj_total for wh, v in adjusted.items()}

            # --- 落货量：最大余额法，整数合计精确等于需求量 ---
            allocation = self._allocate_integer(adjusted, demand_qty)

            results.append({
                "SKU": sku,
                "SPU": sb["info"].get("SPU", ""),
                "室内外": sb["info"].get("室内外", ""),
                "一级分类": sb["info"].get("一级分类", ""),
                "历史月数": sw["history_months"],
                "目标期月数": sw.get("used_months", sw["target_months"]),
                "目标期单量": target_orders,
                "是否新品": "是" if is_new else "否",
                "收缩权重_a": a,
                "趋势alpha": trend_alpha,
                "季节集中度": sw.get("concentration", 0.0),
                "季节性来源": sw.get("seasonality_source", ""),
                "基准层级": sb["level"],
                "基准观测数": sb.get("benchmark_n", ""),
                "基准父层": sb.get("benchmark_parent", ""),
                "自身_美西": sw["self_ratios"]["美西"],
                "自身_美东": sw["self_ratios"]["美东"],
                "自身_美南GA": sw["self_ratios"]["美南GA"],
                "自身_美南TX": sw["self_ratios"]["美南TX"],
                "基准_美西": sb["benchmark"]["美西"],
                "基准_美东": sb["benchmark"]["美东"],
                "基准_美南GA": sb["benchmark"]["美南GA"],
                "基准_美南TX": sb["benchmark"]["美南TX"],
                "最终_美西": final["美西"],
                "最终_美东": final["美东"],
                "最终_美南GA": final["美南GA"],
                "最终_美南TX": final["美南TX"],
                "趋势差_美西": trend_diff["美西"],
                "趋势差_美东": trend_diff["美东"],
                "趋势差_美南GA": trend_diff["美南GA"],
                "趋势差_美南TX": trend_diff["美南TX"],
                "调整后_美西": adjusted["美西"],
                "调整后_美东": adjusted["美东"],
                "调整后_美南GA": adjusted["美南GA"],
                "调整后_美南TX": adjusted["美南TX"],
                "调整后合计": sum(adjusted.values()),
                "落货量_美西": allocation["美西"],
                "落货量_美东": allocation["美东"],
                "落货量_美南GA": allocation["美南GA"],
                "落货量_美南TX": allocation["美南TX"],
            })

        df_results = pd.DataFrame(results)
        n_new = int((df_results["是否新品"] == "是").sum()) if "是否新品" in df_results.columns else 0
        print(f"  Step6: computed final ratios for {len(df_results)} SKUs "
              f"(new products: {n_new}, min_orders={min_orders})")
        return df_results

    @staticmethod
    def _allocate_integer(ratios, qty):
        """最大余额法：按占比分配整数，保证合计精确等于 qty。"""
        if qty <= 0:
            return {wh: 0 for wh in WAREHOUSES}
        raw = {wh: ratios.get(wh, 0.0) * qty for wh in WAREHOUSES}
        floor = {wh: int(np.floor(raw[wh])) for wh in WAREHOUSES}
        remainder = qty - sum(floor.values())
        if remainder > 0:
            # 按小数部分从大到小补足
            order = sorted(WAREHOUSES, key=lambda w: (raw[w] - floor[w]), reverse=True)
            for i in range(remainder):
                floor[order[i % len(order)]] += 1
        return floor

    # =========================================================
    # 自检：环形距离 / 占比合计 / 参数生效
    # =========================================================
    def self_check(self, df_results=None):
        """返回自检结果列表 [(项目, 状态, 说明)]。

        覆盖三个易错点：
        1. 四仓占比合计是否恒为 100%
        2. 趋势因子 α 是否真正生效
        3. 季节品类分支是否生效（强季节用同月、弱季节用全部）
        """
        checks = []

        if df_results is not None and len(df_results):
            for col, label in [("最终", "最终占比"), ("调整后", "调整后占比")]:
                cols = [f"{col}_{wh}" for wh in WAREHOUSES]
                if all(c in df_results.columns for c in cols):
                    tot = df_results[cols].sum(axis=1)
                    dev = (tot - 1.0).abs().max()
                    ok = dev < 1e-9
                    checks.append((f"{label}四仓合计=100%", ok,
                                   f"最大偏差 {dev:.2e}"))

        if self.sku_weighted:
            n_strong = sum(1 for sw in self.sku_weighted.values() if sw.get("is_seasonal"))
            n_weak = len(self.sku_weighted) - n_strong
            n_auto_strong = sum(1 for sw in self.sku_weighted.values() if sw.get("seasonality_source") == "自动-高集中度")
            n_auto_weak = sum(1 for sw in self.sku_weighted.values() if sw.get("seasonality_source") == "自动-低集中度")
            n_cat = sum(1 for sw in self.sku_weighted.values() if sw.get("seasonality_source") == "品类")
            cats = self._get_seasonal_categories()
            checks.append(("季节性判定生效", n_strong > 0 or n_weak > 0,
                           f"强季节SKU: {n_strong} 个（只用同月）, 弱季节SKU: {n_weak} 个（用全部月份加权）\n"
                           f"  其中：自动高集中度→强: {n_auto_strong}, 自动低集中度→弱: {n_auto_weak}, 品类决定: {n_cat}\n"
                           f"  集中度阈值: 高≥{self.concentration_threshold_high:.0%}, 低≤{self.concentration_threshold_low:.0%}, 适用品类: {cats}"))

        alpha = float(self.params.get("alpha_trend", 0) or 0)
        if df_results is not None and len(df_results) and "趋势差_美西" in df_results.columns:
            if alpha <= 0:
                checks.append(("趋势因子 α", True, "α=0，已按预期关闭"))
            else:
                nz = int((df_results[[f"趋势差_{w}" for w in WAREHOUSES]]
                          .abs().sum(axis=1) > 0).sum())
                checks.append(("趋势因子 α 生效", nz > 0,
                               f"{nz}/{len(df_results)} 个SKU取到趋势差（α={alpha}）"
                               if nz > 0 else
                               f"0 个SKU取到趋势差！历史数据不足以覆盖去年同期，趋势因子未生效"))

        return checks

    # =========================================================
    # 验证（与 Excel 结果对比）
    # =========================================================
    def verify(self, df_py):
        df_xl = pd.read_csv(
            os.path.join(self.data_dir, "sheet5_results.csv"),
            encoding="utf-8-sig",
            dtype={"SKU": str}
        )
        df_py["SKU"] = df_py["SKU"].astype(str)
        df_xl["SKU"] = df_xl["SKU"].astype(str)
        merged = df_py.merge(df_xl, left_on="SKU", right_on="SKU", suffixes=("_py", "_xl"))

        print(f"\n{'='*60}")
        print(f"VERIFICATION: {len(merged)} SKUs matched")
        print(f"{'='*60}\n")

        metrics = [
            ("自身_美西", "自身_美西_J"),
            ("基准_美西", "基准_美西_N"),
            ("最终_美西", "最终_美西_R"),
            ("最终_美东", "最终_美东_S"),
            ("最终_美南GA", "最终_美南GA_T"),
            ("最终_美南TX", "最终_美南TX_U"),
            ("收缩权重_a", "收缩权重_I"),
        ]

        all_pass = True
        for py_col, xl_col in metrics:
            if py_col not in merged.columns or xl_col not in merged.columns:
                print(f"  {py_col} vs {xl_col}: COLUMN NOT FOUND")
                continue
            diff = (merged[py_col] - merged[xl_col]).abs()
            max_diff = diff.max()
            mean_diff = diff.mean()
            count_match = (diff < 0.001).sum()
            status = "PASS" if max_diff < 0.001 else "FAIL"
            if status == "FAIL":
                all_pass = False
            print(f"  {py_col} vs {xl_col}: max_diff={max_diff:.6f}, mean={mean_diff:.6f}, "
                  f"match={count_match}/{len(merged)} [{status}]")
            if status == "FAIL":
                worst = merged.loc[diff.idxmax()]
                print(f"    Worst: SKU={worst['SKU']}, Python={worst[py_col]:.6f}, "
                      f"Excel={worst[xl_col]:.6f}")

        print(f"\n  Sample (first 5 SKUs):")
        print(f"  {'SKU':>20} {'Py_美西':>10} {'Xl_美西':>10} {'Diff':>10}")
        for _, row in merged.head().iterrows():
            d = row.get("最终_美西", 0) - row.get("最终_美西_R", 0)
            print(f"  {row['SKU']:>20} {row.get('最终_美西',0):>10.6f} "
                  f"{row.get('最终_美西_R',0):>10.6f} {d:>10.6f}")

        return all_pass


def main():
    print("=" * 60)
    print("分仓占比计算引擎")
    print("=" * 60)

    engine = AllocationEngine()
    engine.load_data()

    print("\n--- Step 1: 运算SKU映射 ---")
    engine.step1_sku_mapping()

    print("\n--- Step 2: 时间衰减加权 ---")
    engine.step2_decay_weight()

    print("\n--- Step 3: 自身占比 ---")
    engine.step3_self_ratio()

    print("\n--- Step 4: 基准占比 ---")
    engine.step4_benchmark()

    print("\n--- Step 5: 最终占比 ---")
    df_results = engine.step5_final_ratio()

    print("\n--- 自检 ---")
    for name, ok, msg in engine.self_check(df_results):
        print(f"  [{'OK' if ok else '!!'}] {name}: {msg}")

    print("\n--- 验证对比 ---")
    all_pass = engine.verify(df_results)

    print(f"\n{'='*60}")
    if all_pass:
        print("验证通过: Python结果与Excel一致")
    else:
        print("存在差异，需要排查")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
