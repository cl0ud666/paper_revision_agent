"""
回归比较：判断新版本（Prompt / Workflow / Agent 修改）是否让系统退化。

规则：新版本在任一维度的平均分比基线下降达到阈值（默认 0.2）即判定退化，
命令会返回非零退出码，用于阻断迭代。

用法::

    python -m evals.regression data/runs/evals/eval-v1.json data/runs/evals/eval-v2.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from evals.judges import DIMENSIONS


def load_results(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compare(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    threshold: float = 0.2,
) -> dict[str, Any]:
    """比较两次评测结果。"""

    base_agg = baseline.get("aggregate") or {}
    cand_agg = candidate.get("aggregate") or {}

    dimensions: dict[str, dict[str, float]] = {}
    degraded_reasons: list[str] = []

    for dimension in DIMENSIONS:
        base_value = float((base_agg.get("dimensions") or {}).get(dimension, 0.0))
        cand_value = float((cand_agg.get("dimensions") or {}).get(dimension, 0.0))
        delta = round(cand_value - base_value, 3)
        dimensions[dimension] = {
            "baseline": base_value,
            "candidate": cand_value,
            "delta": delta,
        }
        if delta <= -threshold:
            degraded_reasons.append(f"{dimension} 下降 {abs(delta)}（阈值 {threshold}）")

    base_overall = float(base_agg.get("overall", 0.0))
    cand_overall = float(cand_agg.get("overall", 0.0))
    overall_delta = round(cand_overall - base_overall, 3)
    if overall_delta <= -threshold:
        degraded_reasons.append(f"综合平均分下降 {abs(overall_delta)}（阈值 {threshold}）")

    base_pass = float(base_agg.get("review_pass_rate", 0.0))
    cand_pass = float(cand_agg.get("review_pass_rate", 0.0))
    pass_delta = round(cand_pass - base_pass, 3)
    if pass_delta <= -threshold:
        degraded_reasons.append(f"Review 通过率下降 {abs(pass_delta)}（阈值 {threshold}）")

    fingerprint_match = baseline.get("fingerprint") == candidate.get("fingerprint")

    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "threshold": threshold,
        "baseline_tag": baseline.get("tag", ""),
        "candidate_tag": candidate.get("tag", ""),
        "fingerprint_match": fingerprint_match,
        "dimensions": dimensions,
        "overall": {
            "baseline": base_overall,
            "candidate": cand_overall,
            "delta": overall_delta,
        },
        "review_pass_rate": {
            "baseline": base_pass,
            "candidate": cand_pass,
            "delta": pass_delta,
        },
        "degraded": bool(degraded_reasons),
        "reasons": degraded_reasons,
    }


def format_report(comparison: dict[str, Any]) -> str:
    lines = ["", "========== 回归比较 =========="]
    if not comparison["fingerprint_match"]:
        lines.append("注意：两次评测的系统指纹不同，比较结果仅供参考。")
    lines.append(f"阈值：{comparison['threshold']}")
    lines.append("")
    lines.append("| 维度 | 基线 | 新版本 | 变化 |")
    lines.append("| --- | --- | --- | --- |")
    for dimension, values in comparison["dimensions"].items():
        lines.append(
            f"| {dimension} | {values['baseline']} | {values['candidate']} | {values['delta']:+} |"
        )
    overall = comparison["overall"]
    lines.append(
        f"| **综合** | {overall['baseline']} | {overall['candidate']} | {overall['delta']:+} |"
    )
    pass_rate = comparison["review_pass_rate"]
    lines.append(
        f"| Review 通过率 | {pass_rate['baseline']} | {pass_rate['candidate']} | {pass_rate['delta']:+} |"
    )
    lines.append("")
    if comparison["degraded"]:
        lines.append("结论：检测到退化，建议阻断本次迭代。")
        for reason in comparison["reasons"]:
            lines.append(f"  - {reason}")
    else:
        lines.append("结论：未检测到明显退化，可以进入下一轮迭代。")
    return "\n".join(lines)


def write_report(comparison: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="评测回归比较（退化检测）")
    parser.add_argument("baseline", help="基线评测结果 JSON")
    parser.add_argument("candidate", help="新版本评测结果 JSON")
    parser.add_argument("--threshold", type=float, default=0.2, help="退化阈值（默认 0.2）")
    parser.add_argument("--output", type=str, default="", help="比较结果保存路径")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    comparison = compare(
        load_results(args.baseline), load_results(args.candidate), threshold=args.threshold
    )
    print(format_report(comparison))

    if args.output:
        write_report(comparison, args.output)
        print(f"\n比较结果已保存：{args.output}")

    return 1 if comparison["degraded"] else 0


if __name__ == "__main__":
    sys.exit(main())
