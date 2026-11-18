#!/usr/bin/env python3
"""Generate the admission-matrix curve manuscripts as standalone request bodies.

Every output file is a complete POST /v1/plates JSON body written into an
isolated cases directory. The generator only emits deterministic bounded
geometry; production limits are not referenced or changed. Pairs are tuned so
their request byte sizes stay close while their flattened segment counts
(hence expansion complexity) differ by an order of magnitude.
"""
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
CASES = HERE / "cases"

SOLID_BRUSH = {"kind": "solid", "color": {"r": 0.2, "g": 0.5, "b": 0.7, "a": 0.9}}
BG = {"r": 0.04, "g": 0.05, "b": 0.08, "a": 1.0}
IDENTITY = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]


def lcg(seed):
    """Deterministic finite coordinate stream, well inside the 1e6 bound."""
    x = seed & 0xFFFFFFFF
    while True:
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        yield (x % 9000) / 10.0  # 0..900, always finite and modest


def rect_ring(x0, y0, x1, y1):
    return [
        {"kind": "M", "points": [{"x": x0, "y": y0}]},
        {"kind": "L", "points": [{"x": x1, "y": y0}]},
        {"kind": "L", "points": [{"x": x1, "y": y1}]},
        {"kind": "L", "points": [{"x": x0, "y": y1}]},
        {"kind": "Z"},
    ]


def line_ring(n_edges, seed):
    """One explicitly closed ring built from n_edges line verbs."""
    rnd = lcg(seed)
    ops = [{"kind": "M", "points": [{"x": next(rnd), "y": next(rnd)}]}]
    for _ in range(n_edges - 1):
        ops.append({"kind": "L", "points": [{"x": next(rnd), "y": next(rnd)}]})
    first = ops[0]["points"][0]
    ops.append({"kind": "L", "points": [{"x": first["x"], "y": first["y"]}]})
    ops.append({"kind": "Z"})
    return ops


def cubic_ring(n_curves, seed):
    """One explicitly closed ring; each cubic expands to 24 flattened edges."""
    rnd = lcg(seed)
    start = {"x": next(rnd), "y": next(rnd)}
    ops = [{"kind": "M", "points": [start]}]
    for _ in range(n_curves):
        ops.append({"kind": "C", "points": [
            {"x": next(rnd), "y": next(rnd)},
            {"x": next(rnd), "y": next(rnd)},
            {"x": next(rnd), "y": next(rnd)},
        ]})
    ops.append({"kind": "L", "points": [{"x": start["x"], "y": start["y"]}]})
    ops.append({"kind": "Z"})
    return ops


def document(width, height, path_ops, clip=None):
    layer = {
        "id": "sheet-0",
        "opacity": 1.0,
        "blend": "normal",
        "shapes": [{
            "id": "contour-0",
            "fill_rule": "nonzero",
            "path": path_ops,
            "transform": IDENTITY,
            "brush": SOLID_BRUSH,
        }],
    }
    if clip is not None:
        layer["clip"] = clip
    return {"width": width, "height": height, "background": BG, "layers": [layer]}


def request(case_id, doc, title=None):
    return {"id": case_id, "title": title or f"准入矩阵 {case_id}", "document": doc}


def write_case(name, payload):
    p = CASES / f"{name}.json"
    p.write_bytes(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    return p


def main():
    CASES.mkdir(parents=True, exist_ok=True)
    for old in CASES.glob("*.json"):
        old.unlink()
    manifest = []

    def emit(name, payload, note, family):
        p = write_case(name, payload)
        manifest.append({"case": name, "file": p.name, "bytes": p.stat().st_size,
                         "family": family, "note": note})

    # Family A: 192x144 near the 1.6e8 gate. Line vs cubic rings tuned to
    # similar byte sizes; only the cubic expansion crosses the static budget.
    emit("a1-line-loose-192",
         request("a1-line-loose-192", document(192, 144, line_ring(240, 11))),
         "240 line edges, low expansion cost", "A-near-gate"),
    emit("a2-curve-dense-192",
         request("a2-curve-dense-192", document(192, 144, cubic_ring(120, 23))),
         "120 cubic verbs (2880 flattened edges), similar body size, over budget",
         "A-near-gate"),
    emit("a3-curve-dense-occluded-192",
         request("a3-curve-occluded-192",
                 document(192, 144, cubic_ring(120, 23),
                          clip=rect_ring(-60.0, -60.0, -10.0, -10.0))),
         "same dense contour fully culled by an off-canvas clip; static product identical",
         "A-near-gate"),
    emit("a4-curve-occluded-legal-192",
         request("a4-curve-occluded-legal",
                 document(192, 144, cubic_ring(40, 29),
                          clip=rect_ring(-60.0, -60.0, -10.0, -10.0))),
         "960 flattened edges with the same off-canvas clip; admitted control proving cull cheapness",
         "A-near-gate"),

    # Family B: 96x72 where both manuscripts are admitted. The timing
    # difference is visible entirely inside the accepted region.
    emit("b1-line-loose-96",
         request("b1-line-loose-96", document(96, 72, line_ring(240, 47))),
         "admitted line ring, low complexity", "B-admitted-pair"),
    emit("b2-curve-dense-96",
         request("b2-curve-dense-96", document(96, 72, cubic_ring(120, 59))),
         "admitted cubic ring, ~12x flattened edges at similar body size",
         "B-admitted-pair"),

    # Family C: 512x512 extremes and bounded-deadline evidence. 252 cubic
    # verbs plus M/L/Z make 255 contour operations, inside the 256-op
    # structural gate, so the observed rejection comes from the 1.6e8 budget
    # gate rather than schema checks.
    emit("c1-curve-extreme-512",
         request("c1-curve-extreme-512", document(512, 512, cubic_ring(252, 71))),
         "252 cubic verbs (6048 edges) on the largest canvas; far over budget",
         "C-extreme"),
    emit("c2-curve-extreme-occluded-512",
         request("c2-curve-extreme-occluded",
                 document(512, 512, cubic_ring(252, 71),
                          clip=rect_ring(-200.0, -200.0, -40.0, -40.0))),
         "same extreme contour fully culled; force-compiled measurement proves cheap imaging",
         "C-extreme"),
    emit("c3-curve-max-admitted-512",
         request("c3-curve-max-admitted", document(512, 512, cubic_ring(6, 83))),
         "144+ edges near the largest admitted static product on 512x512",
         "C-extreme"),

    (CASES / "manifest.json").write_text(
        json.dumps({"cases": manifest}, ensure_ascii=False, indent=2) + "\n")
    for row in manifest:
        print(f"{row['case']:32s} {row['bytes']:8d} bytes  [{row['family']}]")


if __name__ == "__main__":
    main()
