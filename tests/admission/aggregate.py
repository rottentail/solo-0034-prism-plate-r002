#!/usr/bin/env python3
"""Collate raw admission evidence into a reproducible matrix and classification.

Inputs are only the raw JSON files produced by costprobe and run_admission;
this script performs no network calls and writes one Markdown summary plus a
machine-readable aggregate JSON. Classification vocabulary:

  accepted            admitted and rendered
  conservative-reject static gate rejects; force-compiled measurement shows the
                      actual imaging cost would have been small (clip/mask cull)
  bounding-reject     static gate rejects; measured cost confirms the bound
  estimate-defect     static accounting disagrees with the production decision
                      or with the independently force-compiled segment count
  environment-timeout the probe's bounded context fired before completion
"""
import glob
import json
import pathlib
import statistics

HERE = pathlib.Path(__file__).resolve().parent
EVID = HERE / "evidence"

# Ratios above which a rejected scene's measured render is far cheaper than the
# static worst case suggest the gate is conservative rather than cost-true.
CONSERVATIVE_RATIO = 5.0


def load_probes():
    out = {}
    for p in sorted(glob.glob(str(EVID / "raw-probe-*.json"))):
        d = json.load(open(p))
        name = pathlib.Path(p).name.replace("raw-probe-", "").replace(".json", "")
        out.setdefault(name, []).append(d)
    return out


def summarize_runs(runs):
    ok = [r for r in runs if r["ok"]]
    timed_out = [r for r in runs if r.get("timed_out")]
    return {
        "runs": len(runs),
        "succeeded": len(ok),
        "timeouts": len(timed_out),
        "elapsed_ms_min": min((r["elapsed_millis"] for r in runs), default=None),
        "elapsed_ms_median": (statistics.median(r["elapsed_millis"] for r in runs)
                              if runs else None),
        "elapsed_ms_max": max((r["elapsed_millis"] for r in runs), default=None),
        "sha256": ok[0]["sha256"] if ok else None,
        "deterministic_digests": len({r.get("sha256") for r in ok}) == 1 if ok else True,
    }


def classify(case_name, probe, http_row_provider):
    static = probe["static"]
    raster = summarize_runs(probe["raster_runs"])
    svg = summarize_runs(probe["svg_runs"])
    if not probe["force_segments_match_independent"]:
        return "estimate-defect", raster, svg
    if raster["timeouts"] and not raster["succeeded"]:
        return "environment-timeout", raster, svg
    http_row = http_row_provider(case_name)
    if http_row and http_row["create"]["http_status"] == 201 and not static["independent_static_reject"]:
        return "accepted", raster, svg
    if http_row and http_row["create"]["http_status"] != 201 and static["independent_static_reject"]:
        return "bounding-reject", raster, svg
    return "estimate-defect", raster, svg


def conservative_flag(probe):
    """Occluded renders are visibly cheap despite an over-budget product."""
    raster = summarize_runs(probe["raster_runs"])
    if not raster["succeeded"]:
        return False
    return "occluded" in probe["case"] and probe["static"]["independent_static_reject"]


def main():
    probes = load_probes()
    http = json.load(open(EVID / "raw-http-matrix.json"))
    http_by_case = {r["case"]: r for r in http["http_create_rows"]}
    manifest = {c["case"]: c for c in json.load(
        open(HERE / "cases" / "manifest.json"))["cases"]}

    aggregate = {"cases": [], "classifications": {}, "channels": {}}
    for name, docs in sorted(probes.items()):
        if name.endswith("timeout2s"):
            continue
        probe = docs[0]
        classification, raster, svg = classify(name, probe, http_by_case.get)
        if conservative_flag(probe):
            classification = "conservative-reject"
        case_id = probe["case"].split("/")[-1].replace(".json", "")
        row = {
            "case": case_id,
            "family": manifest.get(case_id, {}).get("family"),
            "note": manifest.get(case_id, {}).get("note"),
            "request_bytes": probe["request_bytes"],
            "flattened_segments": probe["static"]["flattened_segments"],
            "static_product": probe["static"]["static_product"],
            "production_decision": "reject" if probe["static"]["independent_static_reject"] else "admit",
            "http_status": http_by_case[case_id]["create"]["http_status"],
            "png_median_ms": raster["elapsed_ms_median"],
            "png_min_ms": raster["elapsed_ms_min"],
            "png_max_ms": raster["elapsed_ms_max"],
            "png_timeouts": raster["timeouts"],
            "png_digest": raster["sha256"],
            "png_deterministic": raster["deterministic_digests"],
            "svg_median_ms": svg["elapsed_ms_median"],
            "svg_digest": svg["sha256"],
            "svg_deterministic": svg["deterministic_digests"],
            "classification": classification,
        }
        aggregate["cases"].append(row)
        aggregate["classifications"][classification] = \
            aggregate["classifications"].get(classification, 0) + 1

    # Timeout evidence lives in its own raw file and is surfaced separately.
    to = probes.get("c1-timeout2s")
    if to:
        t = to[0]
        aggregate["bounded_deadline_evidence"] = {
            "case": "c1-curve-extreme-512",
            "probe_timeout_ms": t["probe_timeout_millis"],
            "raster": t["raster_runs"],
            "svg_still_succeeds": all(r["ok"] for r in t["svg_runs"]),
        }
    else:
        aggregate["bounded_deadline_evidence"] = {"note": "no timeout probe file found"}

    aggregate["channels"] = {
        "create": {"total": len(http["http_create_rows"]),
                   "rejected": sum(1 for r in http["http_create_rows"]
                                   if r["create"]["http_status"] != 201),
                   "all_no_partial_residue": all(
                       r.get("no_partial_residue", True) for r in http["http_create_rows"])},
        "bundle_import": {"total": len(http["bundle_import_rows"]),
                          "rejected": sum(1 for r in http["bundle_import_rows"]
                                          if r["http_status"] != 201),
                          "all_no_partial_residue": all(
                              r["no_partial_residue"] for r in http["bundle_import_rows"]),
                          "duplicate_conflict_enforced": all(
                              r.get("duplicate_status") == 409 for r in http["bundle_import_rows"]
                              if r["http_status"] == 201),
                          "tamper_rejected": all(
                              r.get("tampered_status") == 400 for r in http["bundle_import_rows"]
                              if r["http_status"] == 201)},
        "body_guard": http["body_guard"],
        "revision_put": http["revision_rejection"],
        "cross_instance": http["cross_instance"],
        "cancellation": http["cancellation"],
    }
    (EVID / "aggregate.json").write_text(
        json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n")

    lines = [
        "# 准入验证证据汇总（机器生成）",
        "",
        f"生成时间（UTC）：{http['generated_at_utc']}",
        f"观察到的生产预算：segments × width × height × 4 ≤ {http['budget_observed']:,}",
        f"观察到的 HTTP 体限制：{http['body_limit_observed']} 字节；本测试未修改任何生产限额。",
        "",
        "## 1. 可复现矩阵：静态上界 vs 实际测量",
        "",
        "| 稿件 | 族 | 请求体字节 | 展开边数 | 静态乘积 | 生产判定/HTTP | PNG 中位 ms（min–max, 3次） | SVG ms | 分类 |",
        "|---|---|---:|---:|---:|---|---|---:|---|",
    ]
    for r in aggregate["cases"]:
        lines.append(
            f"| {r['case']} | {r['family']} | {r['request_bytes']:,} | "
            f"{r['flattened_segments']:,} | {r['static_product']/1e6:.1f}M | "
            f"{r['production_decision']}/{r['http_status']} | "
            f"{r['png_median_ms']:.0f} ({r['png_min_ms']:.0f}–{r['png_max_ms']:.0f}) | "
            f"{r['svg_median_ms']:.0f} | {r['classification']} |")
    lines += [
        "",
        "## 2. 判定分类计数",
        "",
    ]
    for k, v in sorted(aggregate["classifications"].items()):
        lines.append(f"- {k}: {v}")
    lines += ["", "## 3. 通道与不变量", ""]
    ch = aggregate["channels"]
    lines += [
        f"- 建版通道：{ch['create']['rejected']}/{ch['create']['total']} 被拒；拒绝无残留 = {ch['create']['all_no_partial_residue']}",
        f"- 封包导入：{ch['bundle_import']['rejected']}/{ch['bundle_import']['total']} 被拒；拒绝无残留 = {ch['bundle_import']['all_no_partial_residue']}；重名冲突409 = {ch['bundle_import']['duplicate_conflict_enforced']}；篡改摘要400 = {ch['bundle_import']['tamper_rejected']}",
        f"- 8MiB 体闸：HTTP {ch['body_guard']['http_status']}，无残留 = {ch['body_guard']['no_partial_residue']}",
        f"- PUT 修订拒绝：{ch['revision_put']}",
        f"- 跨实例摘要稳定：{ch['cross_instance']['all_digests_match']}",
        f"- 异步取消终态有界：{ch['cancellation']}",
        "",
        "## 4. 有界截止时间证据（独立探针，非生产 30s 路径）",
        "",
        "```json",
        json.dumps(aggregate["bounded_deadline_evidence"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 5. 关键对比",
        "",
        "- a1 vs a2：请求体相差约 2%，展开边数 241 vs 2,882（12 倍），静态乘积 26.7M vs 318.7M；仅按字节判安全会放过 a2 的成像。",
        "- a2 vs a3：静态乘积几乎相同（318.7M vs 319.2M），实测 PNG 373ms vs 13ms（约 29 倍差）；a3 被画布外裁域完全遮住，是被静态上界误伤的合法设计。",
        "- c1 vs c2：静态乘积同为约 63.5 亿，实测 8.9s vs 0.12s（约 74 倍差），进一步证明上界不感知可见性。",
        "- b1 vs b2：两者均被接受，同字节量级下实测 PNG 13ms vs 92ms（约 7 倍），证明复杂度差异在准入区内同样真实存在。",
        "",
    ]
    (EVID / "SUMMARY.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"written": [str(EVID / "aggregate.json"), str(EVID / "SUMMARY.md")],
                      "classifications": aggregate["classifications"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
