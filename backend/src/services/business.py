"""检验失败项的Apriori频繁项集与关联规则，不作因果断言。"""
from itertools import combinations
import numpy as np

ITEMS = ["heat_fail", "ph_fail", "viscosity_fail", "ash_fail", "color_fail"]
NAMES = {"heat_fail": "热稳定失败", "ph_fail": "pH偏离", "viscosity_fail": "黏度失败", "ash_fail": "灰分失败", "color_fail": "色差失败"}


def metadata():
    return {
        "name": "检验失败组合追因", "short": "assay-rules",
        "tagline": "从复检批次找出高支持度的共败组合，为原因复核提供线索。",
        "client": "恒谱材料检测室 · 9名检验员 · 240批复检记录",
        "workflow": "实验室按单项失败率开会，检验员临时筛选同批次失败项来猜测共因。",
        "pains": ["罕见组合容易被误当规律", "高置信度可能只是后项本身常见", "复核假设没有记录支持度与提升度口径"],
        "upstream": "同一批次五项检验复核标签 assay_rechecks_202611.csv",
        "downstream": "质量工程师挑选共败组合追查制样与仪器记录，再安排独立复验",
        "entities": ["批次检验事务", "共败规则试验", "质量复核简报"],
        "dataset_name": "恒谱240批五项检验复核标签",
        "columns": [{"key": "batch_id", "label": "复检批次", "type": "string"}] + [{"key": item, "label": f"{NAMES[item]}(1/0)", "type": "number"} for item in ITEMS],
        "parameters": [{"key": "min_support", "label": "最小支持度(比例)", "type": "number", "default": 0.12, "min": 0.05, "max": 0.5}, {"key": "min_confidence", "label": "最小置信度(比例)", "type": "number", "default": 0.65, "min": 0.2, "max": 0.95}],
        "workbench_title": "检验共败规则搜索", "action_label": "挖掘频繁项集与规则", "unit": "复检批次",
        "accent": "oklch(52% 0.13 350)", "style": "laboratory",
        "seed_story": ["08:30 检验员导入 assay_rechecks_202611.csv 的240批结果", "08:40 确认1表示失败、0表示通过，每批仅保留最终复核结果", "09:00 设最小支持度0.12、最小置信度0.65", "09:01 后端逐层剪枝计算最多三项的频繁项集", "09:10 质量工程师先看提升度，再核对规则覆盖批次", "09:20 把支持度提高到0.3，剔除低覆盖线索", "09:30 下载关联规则交制样和仪器小组复核", "15:00 管理员配置每周复检简报的内部归档任务"],
        "method": "每批构成一条失败项事务；Apriori逐层连接并以子集频繁性剪枝，最多3项。支持度=P(A∩B)，置信度=P(B|A)，提升度=P(B|A)/P(B)；仅展示提升度>1的规则。",
        "limitations": ["共败关联不等于共同根因", "仅在已进入复检的样本中分析，存在选择偏差", "最多3项组合，不挖掘更长规则"],
    }


def seed_records():
    rng = np.random.default_rng(1012)
    rows = []
    for i in range(240):
        preparation = rng.random() < 0.32
        furnace = rng.random() < 0.20
        heat = rng.random() < (0.92 if preparation else 0.05)
        viscosity = rng.random() < (0.94 if preparation else 0.06)
        ph = rng.random() < (0.72 if preparation else 0.10)
        ash = rng.random() < (0.92 if furnace else 0.04)
        color = rng.random() < (0.88 if furnace else 0.08)
        rows.append({"batch_id": f"HP-R{i+1:03}", **{k: int(v) for k, v in zip(ITEMS, [heat, ph, viscosity, ash, color])}})
    return rows


def analyze(records, params):
    try:
        min_support = float(params.get("min_support", 0.12))
        min_confidence = float(params.get("min_confidence", 0.65))
    except (ValueError, TypeError) as exc:
        raise ValueError("支持度与置信度须为有效比例。") from exc
    if not 0.05 <= min_support <= 0.5 or not 0.2 <= min_confidence <= 0.95:
        raise ValueError("最小支持度须0.05~0.5，最小置信度须0.2~0.95。")
    if len(records) < 30:
        raise ValueError("至少需要30批复检事务，避免极少样本误导规则。")
    transactions = []
    for row in records:
        try:
            values = [float(row[key]) for key in ITEMS]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("五项检验标记须完整且仅为0或1。") from exc
        if any(value not in (0, 1) for value in values):
            raise ValueError("检验标记仅接受0(通过)或1(失败)。")
        transactions.append(frozenset(key for key, value in zip(ITEMS, values) if value == 1))
    total = len(transactions)
    support = {}
    previous = set()
    for item in ITEMS:
        itemset = frozenset([item])
        fraction = sum(itemset <= t for t in transactions) / total
        if fraction >= min_support:
            support[itemset] = fraction
            previous.add(itemset)
    for size in (2, 3):
        candidates = {a | b for a in previous for b in previous if len(a | b) == size}
        candidates = {c for c in candidates if all(frozenset(s) in previous for s in combinations(sorted(c), size - 1))}
        current = set()
        for itemset in sorted(candidates, key=lambda s: sorted(s)):
            fraction = sum(itemset <= t for t in transactions) / total
            if fraction >= min_support:
                support[itemset] = fraction
                current.add(itemset)
        previous = current
        if not previous:
            break
    rules = []
    for itemset, joint in support.items():
        if len(itemset) < 2:
            continue
        for length in range(1, len(itemset)):
            for subset in combinations(sorted(itemset), length):
                antecedent = frozenset(subset)
                consequent = itemset - antecedent
                confidence = joint / support[antecedent]
                lift = confidence / support[consequent]
                if confidence >= min_confidence and lift > 1 + 1e-12:
                    rules.append({"antecedent": " + ".join(NAMES[k] for k in sorted(antecedent)), "consequent": " + ".join(NAMES[k] for k in sorted(consequent)), "count": int(round(joint * total)), "support_pct": round(joint * 100, 2), "confidence_pct": round(confidence * 100, 2), "lift": round(lift, 4)})
    rules.sort(key=lambda row: (-row["lift"], -row["support_pct"], row["antecedent"], row["consequent"]))
    for rank, rule in enumerate(rules, 1):
        rule["rule_id"] = f"R{rank:02}"
    top = rules[:8]
    combination_count = sum(len(key) >= 2 for key in support)
    return {
        "summary": f"{total}批复检事务中找到{combination_count}个多项频繁组合、{len(rules)}条提升度大于1的有效规则。",
        "metrics": [{"label": "复检批次", "value": total}, {"label": "多项频繁组合", "value": combination_count}, {"label": "筛选后规则", "value": len(rules)}, {"label": "最高提升度", "value": max([row["lift"] for row in rules], default=0)}],
        "table": {"columns": [{"key": "rule_id", "label": "规则编号"}, {"key": "antecedent", "label": "前项A"}, {"key": "consequent", "label": "后项B"}, {"key": "count", "label": "共同失败批次"}, {"key": "support_pct", "label": "支持度(%)"}, {"key": "confidence_pct", "label": "置信度(%)"}, {"key": "lift", "label": "提升度"}], "rows": rules},
        "chart": {"type": "bar", "labels": [row["rule_id"] for row in top], "series": ([{"name": "提升度（规则编号见明细）", "values": [row["lift"] for row in top]}] if top else [])},
        "findings": ([f"首条规则覆盖{rules[0]['count']}批，置信度{rules[0]['confidence_pct']}%，提升度{rules[0]['lift']}。"] if rules else ["当前阈值没有规则通过；可降低阈值，但应先确认复检样本量。"])+["提升度大于1表示后项在前项出现时更常见，仍需对照仪器与制样记录复核。", "反向规则单独计算，A→B与B→A的置信度通常不同。"],
        "method": metadata()["method"], "warnings": ["规则是统计关联，不能直接归因于材料或仪器故障。"] + (["未找到满足阈值的规则。"] if not rules else []),
    }
