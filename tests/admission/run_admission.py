#!/usr/bin/env python3
"""End-to-end admission evidence over real HTTP against an ephemeral service.

The harness builds the unmodified production binary, starts it on an
ephemeral port with an isolated data root, and exercises every matrix case
through the same public paths an integrator uses (POST /v1/plates and
POST /v1/bundles). It records raw responses and verifies that admission
rejections leave no partial plate or artifact behind. Production limits are
never modified; this file only observes and reports them.
"""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent
CASES = HERE / "cases"

BUDGET = 160_000_000
SAMPLES = 4
BODY_LIMIT = 8 << 20


def http_call(base, method, path, body=None, headers=None, timeout=40):
    data = body if isinstance(body, bytes) else (
        json.dumps(body).encode() if body is not None else None)
    hdr = {"Content-Type": "application/json"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(base + path, data=data, method=method, headers=hdr)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            code, hs = r.status, {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        payload = e.read()
        code, hs = e.code, {k.lower(): v for k, v in e.headers.items()}
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    return {"code": code, "payload": payload, "headers": hs, "elapsed_ms": elapsed_ms}


def wait_health(base, proc, log):
    for _ in range(120):
        if proc.poll() is not None:
            raise RuntimeError(f"service exited early; log:\n{log.read_text()[:2000]}")
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=1) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("startup timed out")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def list_state(data_root):
    snap = data_root / "state.json"
    if not snap.exists():
        return {"present": False}
    state = json.loads(snap.read_text())
    return {
        "present": True,
        "plates": sorted(state["plates"].keys()),
        "artifact_count": len(state["artifacts"]),
        "proof_count": len(state["proofs"]),
        "snapshot_bytes": snap.stat().st_size,
    }


def independent_static(doc):
    """Harness-side re-accounting of the subdivision and budget contract."""
    segs, contours = 0, 0
    for layer in doc["layers"]:
        for key in ("clip", "mask"):
            s, c = count_ops(layer.get(key) or [])
            segs += s
            contours += c
        for shape in layer["shapes"]:
            s, c = count_ops(shape["path"])
            segs += s
            contours += c
    product = segs * doc["width"] * doc["height"] * SAMPLES
    return {"segments": segs, "contours": contours, "static_product": product,
            "independent_reject": product > BUDGET}


def count_ops(ops):
    add = {"M": 1, "L": 1, "Q": 16, "C": 24, "Z": 0}
    segs = open_v = 0
    for op in ops:
        if op["kind"] == "M":
            open_v = 1
        elif op["kind"] == "Z":
            if open_v >= 3:
                segs += open_v
            open_v = 0
        else:
            open_v += add[op["kind"]]
    return segs, 0


def check_png(raw):
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        return "missing PNG signature"
    width, height = struct.unpack(">II", raw[16:24])
    if not (1 <= width <= 512 and 1 <= height <= 512):
        return "IHDR dimensions outside canvas contract"
    if b"IEND" not in raw[-12:]:
        return "missing IEND trailer"
    return None


SVG_FORBIDDEN = (b"<script", b"javascript:", b"xlink:href", b"<!entity", b"<!doctype",
                 b"http://www.w3.org/1999/xlink")


def check_svg(raw):
    if not raw.lstrip().startswith(b"<svg") or not raw.rstrip().endswith(b"</svg>"):
        return "malformed SVG envelope"
    # The W3C SVG namespace URL is a required identifier, not an external fetch.
    low = raw.lower().replace(b"http://www.w3.org/2000/svg", b"")
    for token in SVG_FORBIDDEN:
        if token in low:
            return f"forbidden token in SVG: {token.decode()}"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", default=os.environ.get("GO_BIN", "go"))
    ap.add_argument("--probe", required=True, help="path to compiled costprobe binary")
    ap.add_argument("--out", required=True, help="evidence output directory")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix="prism-admission-"))
    try:
        binary = work / "prismplate"
        build = subprocess.run([args.go, "build", "-o", str(binary), "./cmd/prismplate"],
                               cwd=ROOT, capture_output=True, text=True)
        if build.returncode != 0:
            raise RuntimeError(f"production build failed: {build.stderr}")

        port = free_port()
        base = f"http://127.0.0.1:{port}"
        data_root = work / "state"
        log = (work / "service.log").open("w")
        proc = subprocess.Popen([str(binary), "-listen", f"127.0.0.1:{port}",
                                 "-data", str(data_root)], stdout=log, stderr=log)
        rows = []
        try:
            wait_health(base, proc, log)
            case_files = sorted(p for p in CASES.glob("*.json") if p.name != "manifest.json")
            for idx, cf in enumerate(case_files, start=1):
                raw = cf.read_bytes()
                req = json.loads(raw)
                static = independent_static(req["document"])
                row = {"case": cf.stem, "request_bytes": len(raw), "static": static,
                       "channel": "http-create"}

                before = list_state(data_root)
                resp = http_call(base, "POST", "/v1/plates", body=raw)
                after = list_state(data_root)
                row["create"] = {"http_status": resp["code"], "elapsed_ms": resp["elapsed_ms"],
                                 "error": (json.loads(resp["payload"]).get("error")
                                           if resp["code"] >= 400 and resp["payload"] else None)}
                row["state_before"] = before
                row["state_after"] = after

                if resp["code"] == 201:
                    plate = json.loads(resp["payload"])
                    row["revision_digest"] = plate["revisions"][0]["digest"]
                    rendered = {}
                    for fmt, verifier in (("png", check_png), ("svg", check_svg)):
                        r = http_call(base, "POST",
                                      f"/v1/plates/{req['id']}/render?revision=1&format={fmt}")
                        entry = {"http_status": r["code"], "elapsed_ms": r["elapsed_ms"]}
                        if r["code"] == 201:
                            art = json.loads(r["payload"])
                            got = http_call(base, "GET", "/v1/artifacts/" + art["id"])
                            got_digest = hashlib.sha256(got["payload"]).hexdigest()
                            entry["artifact_status"] = got["code"]
                            entry["digest"] = got_digest
                            entry["digest_match"] = got_digest == art["digest"]
                            entry["content_type"] = got["headers"].get("content-type")
                            entry["bytes"] = len(got["payload"])
                            entry["format_check"] = verifier(got["payload"])
                            # Determinism: re-render must return the same artifact id.
                            again = http_call(base, "POST",
                                              f"/v1/plates/{req['id']}/render?revision=1&format={fmt}")
                            entry["rerender_status"] = again["code"]
                            entry["deterministic_id"] = (
                                json.loads(again["payload"]).get("id") == art["id"])
                        rendered[fmt] = entry
                    row["rendered"] = rendered
                    # A rejected-looking static claim must never happen on an admitted row.
                    row["decision_consistent"] = not static["independent_reject"]
                else:
                    # Rejection contract: 400, no plate, no partial revision/artifact.
                    row["no_partial_residue"] = (
                        resp["code"] == 400
                        and req["id"] not in after["plates"]
                        and after["artifact_count"] == before.get("artifact_count", 0))
                    row["decision_consistent"] = bool(static["independent_reject"])
                rows.append(row)

            # ---- bundle channel: seal with production bundle code via the probe ----
            # The importer uses a separate plate namespace; re-seal after the id
            # rename so the envelope digest still authenticates the payload.
            bundle_rows = []
            for cf in case_files:
                req = json.loads(cf.read_bytes())
                renamed = work / f"{cf.stem}.renamed.json"
                bid = "imp-" + req["id"]
                req["id"] = bid
                renamed.write_text(json.dumps(req, separators=(",", ":")))
                sealed_path = work / f"{cf.stem}.bundle.json"
                seal = subprocess.run([args.probe, "-mode", "seal", "-case", str(renamed),
                                       "-out", str(sealed_path)], capture_output=True, text=True)
                if seal.returncode != 0:
                    raise RuntimeError(f"seal failed for {cf.stem}: {seal.stderr}")
                envelope = sealed_path.read_bytes()
                static = independent_static(req["document"])
                before = list_state(data_root)
                r = http_call(base, "POST", "/v1/bundles", body=envelope)
                after = list_state(data_root)
                entry = {"case": cf.stem, "bundle_bytes": len(envelope),
                         "http_status": r["code"], "elapsed_ms": r["elapsed_ms"],
                         "error": (json.loads(r["payload"]).get("error")
                                   if r["code"] >= 400 and r["payload"] else None),
                         "no_partial_residue": (
                             # On success the plate SHOULD exist; residue checks
                             # only apply to rejected imports.
                             (r["code"] == 201 and bid in after["plates"])
                             or (r["code"] != 201 and bid not in after["plates"]
                                 and after["artifact_count"] == before.get("artifact_count", 0)))}
                if r["code"] == 201:
                    entry["decision_consistent"] = not static["independent_reject"]
                    # Duplicate import must be a 409 conflict, never an overwrite.
                    dup = http_call(base, "POST", "/v1/bundles", body=envelope)
                    entry["duplicate_status"] = dup["code"]
                    entry["duplicate_no_overwrite"] = dup["code"] == 409
                    # Tampered digest must be rejected by the importer as well.
                    tampered = json.loads(envelope)
                    tampered["digest"] = "0" * 64
                    tr = http_call(base, "POST", "/v1/bundles",
                                   body=json.dumps(tampered).encode())
                    entry["tampered_status"] = tr["code"]
                else:
                    entry["decision_consistent"] = bool(static["independent_reject"])
                bundle_rows.append(entry)

            # ---- 8 MiB body guard, independent of expansion complexity ----
            pad_case = CASES / "a1-line-loose-192.json"
            padded = pad_case.read_bytes()
            padded = padded[:-1] + b" " * (BODY_LIMIT + 1024 - len(padded) + 1) + b"}"
            before_pad = list_state(data_root)
            pad_resp = http_call(base, "POST", "/v1/plates", body=padded)
            after_pad = list_state(data_root)
            body_row = {"padded_bytes": len(padded), "http_status": pad_resp["code"],
                        "no_partial_residue": (
                            after_pad["plates"] == before_pad["plates"]
                            and after_pad["artifact_count"] == before_pad["artifact_count"])}

            # ---- PUT revision channel: rejection keeps the old revision intact ----
            put_rows = []
            admitted = next(r for r in rows if r["case"] == "a1-line-loose-192")
            dense_doc = json.loads((CASES / "a2-curve-dense-192.json").read_text())["document"]
            before = list_state(data_root)
            put_resp = http_call(
                base, "PUT", "/v1/plates/a1-line-loose-192",
                body=json.dumps({"expected_revision": 1, "document": dense_doc}).encode())
            after = list_state(data_root)
            plate_now = json.loads(
                http_call(base, "GET", "/v1/plates/a1-line-loose-192")["payload"])
            put_rows = {
                "over_budget_put_status": put_resp["code"],
                "error": (json.loads(put_resp["payload"]).get("error")
                          if put_resp["code"] >= 400 else None),
                "revision_count_unchanged": len(plate_now["revisions"]) == 1,
                "revision_digest_unchanged": (
                    plate_now["revisions"][0]["digest"] == admitted["revision_digest"]),
                "no_artifact_growth": (
                    after["artifact_count"] == before.get("artifact_count", 0)),
            }

            # ---- cross-instance stability: fresh data root, bundle import, same digests ----
            cross = {}
            ref_case = CASES / "b2-curve-dense-96.json"
            ref_req = json.loads(ref_case.read_text())
            cross_id = "cross-instance-b2"
            ref_req["id"] = cross_id
            cross_req_path = work / "cross-b2.json"
            cross_req_path.write_text(json.dumps(ref_req, separators=(",", ":")))
            cross_bundle = work / "cross-b2.bundle.json"
            seal = subprocess.run([args.probe, "-mode", "seal", "-case", str(cross_req_path),
                                   "-out", str(cross_bundle)], capture_output=True, text=True)
            if seal.returncode != 0:
                raise RuntimeError(f"cross-instance seal failed: {seal.stderr}")
            ref_digests = {
                fmt: next(r for r in rows if r["case"] == "b2-curve-dense-96")
                ["rendered"][fmt]["digest"]
                for fmt in ("png", "svg")
            }
            root2 = work / "state2"
            port2 = free_port()
            base2 = f"http://127.0.0.1:{port2}"
            log2 = (work / "service2.log").open("w")
            proc2 = subprocess.Popen([str(binary), "-listen", f"127.0.0.1:{port2}",
                                      "-data", str(root2)], stdout=log2, stderr=log2)
            try:
                wait_health(base2, proc2, log2)
                imp = http_call(base2, "POST", "/v1/bundles", body=cross_bundle.read_bytes())
                cross["import_status"] = imp["code"]
                cross_matches = {}
                if imp["code"] == 201:
                    for fmt in ("png", "svg"):
                        rr = http_call(base2, "POST",
                                       f"/v1/plates/{cross_id}/render?revision=1&format={fmt}")
                        art = json.loads(rr["payload"])
                        got = http_call(base2, "GET", "/v1/artifacts/" + art["id"])
                        got_digest = hashlib.sha256(got["payload"]).hexdigest()
                        cross_matches[fmt] = {
                            "status": rr["code"],
                            "first_instance_digest": ref_digests[fmt],
                            "second_instance_digest": got_digest,
                            "digest_equal_to_first_instance": got_digest == ref_digests[fmt],
                        }
                cross["rendered"] = cross_matches
                cross["all_digests_match"] = all(
                    v["digest_equal_to_first_instance"] for v in cross_matches.values())
            finally:
                if proc2.poll() is None:
                    proc2.send_signal(signal.SIGTERM)
                    try:
                        proc2.wait(timeout=45)
                    except subprocess.TimeoutExpired:
                        proc2.kill()
                        proc2.wait()
                log2.close()

            # ---- bounded cancellation through the real async pool ----
            cancel_rows = []
            dense = json.loads((CASES / "b2-curve-dense-96.json").read_text())
            j = http_call(base, "POST",
                          f"/v1/plates/{dense['id']}/exposures?revision=1&format=png")
            if j["code"] == 202:
                job = json.loads(j["payload"])
                c = http_call(base, "DELETE", "/v1/exposures/" + job["id"])
                terminal = None
                for _ in range(250):
                    g = http_call(base, "GET", "/v1/exposures/" + job["id"])
                    terminal = json.loads(g["payload"])
                    if terminal["state"] in ("succeeded", "cancelled", "failed"):
                        break
                    time.sleep(0.02)
                cancel_rows = {"submit_status": j["code"], "cancel_status": c["code"],
                               "terminal_state": terminal["state"],
                               "terminal_is_bounded": terminal["state"] in
                               ("succeeded", "cancelled", "failed")}
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            log.close()

        final_state = json.loads((data_root / "state.json").read_text()) if (data_root / "state.json").exists() else {}
        report = {
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "budget_observed": BUDGET,
            "body_limit_observed": BODY_LIMIT,
            "http_create_rows": rows,
            "bundle_import_rows": bundle_rows,
            "body_guard": body_row,
            "revision_rejection": put_rows,
            "cross_instance": cross,
            "cancellation": cancel_rows,
            "final_state_summary": {
                "plates": sorted(final_state.get("plates", {}).keys()),
                "artifact_count": len(final_state.get("artifacts", {})),
                "proof_count": len(final_state.get("proofs", {})),
            },
            "service_log_tail": log_tail(log.name),
        }
        (out / "raw-http-matrix.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"result": "written", "out": str(out / "raw-http-matrix.json"),
                          "create_cases": len(rows), "bundle_cases": len(bundle_rows)},
                         ensure_ascii=False))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def log_tail(path):
    try:
        return pathlib.Path(path).read_text().splitlines()[-10:]
    except OSError:
        return []


if __name__ == "__main__":
    main()
