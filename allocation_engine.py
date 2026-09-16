"""
P1 计算引擎 - 分仓占比计算核心
纯Python实现6步计算链路，与v6 Excel结果对比验证。

6步链路:
1. 运算SKU映射 (VLOOKUP + TEXTBEFORE fallback)
2. 时间衰减加权 (w = λ^|anchor - month_seq|)
3. 季节因子 (V = β if matching month+category, else 1)
4. 自身占比 (SUMIFS by 运算SKU / SUMIFS total)
5. 基准占比 (SPU → 一级分类 → 室内外 → 全公司 fallback + Bayesian shrinkage)
6. 最终占比 (a×自身 + (1-a)×基准) + 落货量
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

    # 默认参数（文件不存在时使用）
    DEFAULT_PARAMS = {
        "anchor": 24325, "lambda": 0.85, "k": 6.0,
        "a_min": 0.0, "a_max": 0.9,
        "target_type": 1, "target_start": 1, "target_end": 3,
        "alpha_trend": 0.3, "trend_cap": 0.05,
        "recent_start": None, "recent_end": None,
        "far_start": None, "far_end": None,
        "seasonal_switch": 1, "seasonal_window": 1,
        "seasonal_beta": 3.0,
        "seasonal_cat1": "庭院、草坪与花园",
        "seasonal_cat2": "庭院",
    }

    def _load_params(self):
        path = os.path.join(self.data_dir, "params.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
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

    # Step 1: 运算SKU映射
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

    # Step 2: 时间衰减加权
    def step2_decay_weight(self):
        df = self.df_raw.copy()
        anchor = self.params["anchor"]
        lambda_val = self.params["lambda"]

        df["月份序号_py"] = df["年"] * 12 + df["月"]
        df["衰减权重_py"] = lambda_val ** (abs(anchor - df["月份序号_py"]))

        if "在目标期" in df.columns:
            df["在目标期_py"] = df["在目标期"]

        if "衰减权重_N" in df.columns:
            df["decay_diff"] = abs(df["衰减权重_py"] - df["衰减权重_N"])
            max_diff = df["decay_diff"].max()
            print(f"  Step2: max decay weight diff vs Excel: {max_diff:.10f}")

        self.df_raw = df
        return df

    # Step 3: 季节因子
    def step3_seasonal_factor(self):
        df = self.df_raw.copy()
        switch = self.params["seasonal_switch"]
        window = self.params["seasonal_window"]
        beta = self.params["seasonal_beta"]
        cat1 = self.params["seasonal_cat1"]
        cat2 = self.params["seasonal_cat2"]
        anchor = self.params["anchor"]
        anchor_month = (int(anchor) - 1) % 12 + 1

        def window_end_dist(m, target, win):
            return (target - m) % 12 <= win

        if "在目标期" in df.columns:
            target_rows = df[df["在目标期"] == 1]
            target_months = sorted(target_rows["月"].unique())
        else:
            target_months = [anchor_month]

        if switch == 1:
            df["季节因子_py"] = 1.0
            mask = df["一级分类"].isin([cat1, cat2]) & \
                   df["月"].apply(lambda m: any(
                       window_end_dist(m, tm, window) for tm in target_months
                   ))
            df.loc[mask, "季节因子_py"] = beta
        else:
            df["季节因子_py"] = 1.0

        if "季节因子_V" in df.columns:
            mismatches = df[abs(df["季节因子_py"] - df["季节因子_V"]) > 0.01]
            print(f"  Step3: seasonal factor mismatches vs Excel: {len(mismatches)}")

        self.df_raw = df
        return df

    # Step 4: 自身占比
    def step4_self_ratio(self):
        df = self.df_raw.copy()

        for wh in WAREHOUSES:
            df[f"加权_{wh}_py"] = df["衰减权重_py"] * df["季节因子_py"] * df[wh]
        df["加权合计_py"] = df["衰减权重_py"] * df["季节因子_py"] * df[WAREHOUSES].sum(axis=1)

        target_df = df[df["在目标期_py"] == 1].copy()
        grouped = target_df.groupby("运算SKU_py")

        sku_weighted = {}
        for sku, group in grouped:
            total = group["加权合计_py"].sum()
            if total == 0:
                continue
            wh_sums = {wh: group[f"加权_{wh}_py"].sum() for wh in WAREHOUSES}
            sku_weighted[sku] = {
                "total": total,
                "wh_sums": wh_sums,
                "self_ratios": {wh: wh_sums[wh] / total for wh in WAREHOUSES},
            }

        for sku in sku_weighted:
            all_rows = df[df["运算SKU_py"] == sku]
            sku_weighted[sku]["history_months"] = len(all_rows[all_rows[WAREHOUSES].sum(axis=1) > 0])
            target_rows = all_rows[all_rows["在目标期_py"] == 1]
            sku_weighted[sku]["target_months"] = len(target_rows[target_rows[WAREHOUSES].sum(axis=1) > 0])

        print(f"  Step4: computed self ratios for {len(sku_weighted)} unique 运算SKUs")
        self.sku_weighted = sku_weighted
        return sku_weighted

    # Step 5: 基准占比
    def step5_benchmark(self):
        spu_bm = {bm["label"]: bm for bm in self.benchmarks["SPU"]}
        cat_bm = {bm["label"]: bm for bm in self.benchmarks["一级分类"]}
        indoor_bm = {bm["label"]: bm for bm in self.benchmarks["室内外"]}

        all_target = self.df_raw[self.df_raw["在目标期_py"] == 1]
        overall_total = all_target[WAREHOUSES].sum(axis=1).sum()
        overall_ratios = {wh: all_target[wh].sum() / overall_total for wh in WAREHOUSES} if overall_total > 0 else {wh: 0.25 for wh in WAREHOUSES}

        df = self.df_raw
        sku_info = {}
        for sku in self.sku_weighted:
            rows = df[df["运算SKU_py"] == sku]
            if len(rows) == 0:
                continue
            sku_info[sku] = {
                "SPU": rows["SPU"].iloc[0],
                "一级分类": rows["一级分类"].iloc[0],
                "室内外": rows["室内外"].iloc[0],
            }

        sku_benchmarks = {}
        for sku, info in sku_info.items():
            spu = info["SPU"]
            cat1 = info["一级分类"]
            indoor = info["室内外"]
            used_level = "全公司"
            bm_ratios = overall_ratios.copy()

            if spu and spu in spu_bm and spu_bm[spu]["观测数"] > 0:
                bm = spu_bm[spu]
                bm_ratios = {wh: bm[f"基准_{wh}"] for wh in WAREHOUSES}
                used_level = "SPU"
            elif cat1 and cat1 in cat_bm and cat_bm[cat1]["观测数"] > 0:
                bm = cat_bm[cat1]
                bm_ratios = {wh: bm[f"基准_{wh}"] for wh in WAREHOUSES}
                used_level = "一级分类"
            elif indoor and indoor in indoor_bm and indoor_bm[indoor]["观测数"] > 0:
                bm = indoor_bm[indoor]
                bm_ratios = {wh: bm[f"基准_{wh}"] for wh in WAREHOUSES}
                used_level = "室内外"

            sku_benchmarks[sku] = {
                "benchmark": bm_ratios,
                "level": used_level,
                "info": info,
            }

        print(f"  Step5: computed benchmarks for {len(sku_benchmarks)} SKUs")
        self.sku_benchmarks = sku_benchmarks
        return sku_benchmarks

    # Step 6: 最终占比 + 落货量
    def step6_final_ratio(self, demand_qty=3000):
        k = self.params["k"]
        a_min = self.params["a_min"]
        a_max = self.params["a_max"]
        new_threshold = self.new_product_threshold

        results = []
        for sku in self.sku_weighted:
            sw = self.sku_weighted[sku]
            sb = self.sku_benchmarks.get(sku, {"benchmark": {wh: 0.25 for wh in WAREHOUSES}, "level": "全公司", "info": {}})

            n = sw["target_months"]
            history = sw["history_months"]

            # New product logic: if history months <= threshold, a=0 (use benchmark only)
            if history <= new_threshold:
                a = 0.0
            else:
                a_raw = n / (n + k) if (n + k) > 0 else 0
                a = min(a_max, max(a_min, a_raw))

            final = {}
            for wh in WAREHOUSES:
                final[wh] = a * sw["self_ratios"][wh] + (1 - a) * sb["benchmark"][wh]

            total_final = sum(final.values())
            if total_final > 0:
                final = {wh: v / total_final for wh, v in final.items()}

            allocation = {}
            for wh in WAREHOUSES:
                allocation[wh] = round(final[wh] * demand_qty)
            allocated_total = sum(allocation.values())
            diff = demand_qty - allocated_total
            if diff != 0:
                max_wh = max(allocation, key=allocation.get)
                allocation[max_wh] += diff

            results.append({
                "SKU": sku,
                "SPU": sb["info"].get("SPU", ""),
                "室内外": sb["info"].get("室内外", ""),
                "一级分类": sb["info"].get("一级分类", ""),
                "历史月数": sw["history_months"],
                "目标期月数": sw["target_months"],
                "收缩权重_a": a,
                "基准层级": sb["level"],
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
                "落货量_美西": allocation["美西"],
                "落货量_美东": allocation["美东"],
                "落货量_美南GA": allocation["美南GA"],
                "落货量_美南TX": allocation["美南TX"],
            })

        df_results = pd.DataFrame(results)
        print(f"  Step6: computed final ratios for {len(df_results)} SKUs")
        return df_results

    # Verify against Excel
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
            print(f"  {py_col} vs {xl_col}: max_diff={max_diff:.6f}, mean={mean_diff:.6f}, match={count_match}/{len(merged)} [{status}]")
            if status == "FAIL":
                worst = merged.loc[diff.idxmax()]
                print(f"    Worst: SKU={worst['SKU']}, Python={worst[py_col]:.6f}, Excel={worst[xl_col]:.6f}")

        print(f"\n  Sample (first 5 SKUs):")
        print(f"  {'SKU':>20} {'Py_美西':>10} {'Xl_美西':>10} {'Diff':>10}")
        for _, row in merged.head().iterrows():
            d = row.get("最终_美西", 0) - row.get("最终_美西_R", 0)
            print(f"  {row['SKU']:>20} {row.get('最终_美西',0):>10.6f} {row.get('最终_美西_R',0):>10.6f} {d:>10.6f}")

        return all_pass


def main():
    print("=" * 60)
    print("P1 计算引擎 - 分仓占比计算")
    print("=" * 60)

    engine = AllocationEngine()
    engine.load_data()

    print("\n--- Step 1: 运算SKU映射 ---")
    engine.step1_sku_mapping()

    print("\n--- Step 2: 时间衰减加权 ---")
    engine.step2_decay_weight()

    print("\n--- Step 3: 季节因子 ---")
    engine.step3_seasonal_factor()

    print("\n--- Step 4: 自身占比 ---")
    engine.step4_self_ratio()

    print("\n--- Step 5: 基准占比 ---")
    engine.step5_benchmark()

    print("\n--- Step 6: 最终占比 ---")
    df_results = engine.step6_final_ratio()

    print("\n--- 验证对比 ---")
    all_pass = engine.verify(df_results)

    print(f"\n{'='*60}")
    if all_pass:
        print("P1 计算引擎验证通过: Python结果与Excel一致")
    else:
        print("存在差异，需要排查")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
