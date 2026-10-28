#!/usr/bin/env python3
"""
MeshOpt analysis for a run made with --draco-bits.

Part A (repair): the '+draco' rows are dropped; for each requirement profile it reports
per-chain success, best single chain vs per-mesh oracle, unique fixes, predictability
(cross-validated CatBoost AUC) and policies (best single, try-all cascade, learned router
with a skip threshold, oracle).
Part B (Draco): what Draco compression costs (time, RAM) and breaks (validity, distance),
and which quantization bits are enough per mesh.

Usage:  python analyze_draco.py objaverse_draco.csv --out draco_report.md
"""
import argparse
import os
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

FEATURES = ["n_faces", "n_verts", "aspect_ratio", "boundary_edge_ratio",
            "nonmanifold_edge_ratio", "degenerate_face_ratio", "n_components",
            "self_intersect_est", "sharp_edge_ratio"]

PROFILES = {
    "sdf": lambda r: r.watertight & (r.hausdorff_rel <= 0.02),
    "token": lambda r: r.watertight & r.manifold & r.out_faces.between(500, 10_000)
    & (r.hausdorff_rel <= 0.01),
    "cad": lambda r: r.watertight & r.manifold & r.out_faces.between(500, 10_000)
    & (r.hausdorff_rel <= 0.005) & (r.sharp_recall.fillna(1.0) >= 0.9),
}
SKIPS = [0.20, 0.10, 0.05, 0.0]


def load(path):
    df = pd.read_csv(path, low_memory=False)
    n_unreadable = int((df.chain == "_load").sum())
    df = df[df.chain != "_load"].copy()
    # When the cgroup OOM killer kills an external binary (fTetWild, ManifoldPlus), systemd
    # stops the whole run scope with SIGTERM, so the Python child exits with -15 and
    # run_pilot.py logged it as "error". Nothing else sends SIGTERM to a run.
    sigterm = df.status.eq("error") & df.error.astype(str).str.contains("exited with code -15")
    df.loc[sigterm, "status"] = "oom"
    for c in ["watertight", "manifold"]:
        df[c] = df[c].astype(str).str.lower().eq("true")
    num = ["out_faces", "hausdorff_rel", "sharp_recall", "time_s", "peak_mem_mb", "draco_bits",
           "draco_bytes", "raw_bytes", "draco_ratio", "draco_encode_s", "draco_decode_s",
           "draco_mem_mb"]
    for c in num:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ok"] = df.status.eq("ok")
    df["bits"] = df.chain.str.extract(r"\+draco(\d+)$")[0].astype(float)
    df["base"] = df.chain.str.replace(r"\+draco\d+$", "", regex=True)
    for p, rule in PROFILES.items():
        df[p] = df.ok & rule(df).fillna(False)
    return df, n_unreadable


def oof_probs(X, succ, seed=0):
    from catboost import CatBoostClassifier
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import StratifiedKFold
    P = pd.DataFrame(index=succ.index, columns=succ.columns, dtype=float)
    aucs = {}
    for c in succ.columns:
        y = succ[c].values.astype(int)
        if min(y.sum(), len(y) - y.sum()) < 10:
            P[c], aucs[c] = y.mean(), np.nan
            continue
        p = np.zeros(len(y))
        for tr, te in StratifiedKFold(5, shuffle=True, random_state=seed).split(X, y):
            m = CatBoostClassifier(iterations=300, learning_rate=0.05, depth=5, verbose=0,
                                   random_seed=seed, thread_count=4)
            m.fit(X[tr], y[tr])
            p[te] = m.predict_proba(X[te])[:, 1]
        P[c], aucs[c] = p, roc_auc_score(y, p)
    return P, aucs


def cascade(succ_row, time_row, order):
    cost = 0.0
    for c in order:
        cost += time_row[c]
        if succ_row[c]:
            return True, cost
    return False, cost


def md(df, fmt="{:.1f}"):
    return df.to_markdown(floatfmt=".3g") if hasattr(df, "to_markdown") else str(df)


def repair_part(df, out):
    raw = df[df.bits.isna()]
    feat = raw.groupby("mesh")[FEATURES].first()
    tm = raw.pivot_table(index="mesh", columns="chain", values="time_s", aggfunc="first")
    tm = tm.fillna(tm.max().max())          # missing time = treat as the slowest observed
    X = np.log1p(feat.loc[tm.index].fillna(-1).clip(lower=0)).values
    summary = []
    for prof in PROFILES:
        succ = raw.pivot_table(index="mesh", columns="chain", values=prof,
                               aggfunc="first").loc[tm.index].fillna(False).astype(bool)
        n = len(succ)
        rate = succ.mean().sort_values(ascending=False)
        best = rate.index[0]
        oracle = succ.any(axis=1)
        only = succ[oracle & (succ.sum(axis=1) == 1)]
        uniq = only.sum().sort_values(ascending=False)
        uniq = uniq[uniq > 0]
        fail = raw.assign(f=~raw.ok).groupby("chain").f.mean()
        per = pd.DataFrame({"pass %": rate * 100,
                            "unique fixes": uniq.reindex(rate.index).fillna(0).astype(int),
                            "fail (timeout/oom/err) %": fail.reindex(rate.index) * 100,
                            "median s": tm.median().reindex(rate.index),
                            "total h": tm.sum().reindex(rate.index) / 3600})
        out.append(f"\n### Profile `{prof}`  ({n} meshes)\n")
        out.append(per.round(2).to_markdown() + "\n")
        out.append(f"Best single chain **`{best}` {rate[best]*100:.1f}%**, per-mesh oracle "
                   f"**{oracle.mean()*100:.1f}%** (gap {100*(oracle.mean()-rate[best]):.1f} points).\n")

        P, aucs = oof_probs(X, succ)
        a = pd.Series(aucs).dropna()
        out.append(f"CatBoost cross-validated AUC (\"chain passes\"): mean **{a.mean():.3f}**, "
                   f"min {a.min():.3f} (`{a.idxmin()}`), max {a.max():.3f} (`{a.idxmax()}`).\n")

        med = tm.median()
        rows = [("always " + best, succ[best].mean() * 100, tm[best].sum() / 3600)]
        order_fixed = list(med.sort_values().index)
        res = [cascade(succ.loc[m], tm.loc[m], order_fixed) for m in succ.index]
        rows.append(("try all (cheapest first)", np.mean([r[0] for r in res]) * 100,
                     sum(r[1] for r in res) / 3600))
        for s in SKIPS:
            kept = cost = 0.0
            for m in succ.index:
                p = P.loc[m]
                cand = p[p >= s] if s > 0 else p
                if len(cand) == 0:
                    continue
                order = list((med[cand.index] / cand.clip(lower=0.02)).sort_values().index)
                k, c = cascade(succ.loc[m], tm.loc[m], order)
                kept += k
                cost += c
            rows.append((f"router, skip chains with p<{int(s*100)}%" if s else "router, no skip",
                         kept / n * 100, cost / 3600))
        tw = tm.where(succ)
        rows.append(("oracle (cheapest passing chain)", oracle.mean() * 100,
                     tw.min(axis=1).fillna(0).sum() / 3600))
        pol = pd.DataFrame(rows, columns=["policy", "kept %", "compute h"]).set_index("policy")
        out.append(pol.round(2).to_markdown() + "\n")
        summary.append({"profile": prof, "best chain": best, "best %": rate[best] * 100,
                        "oracle %": oracle.mean() * 100, "mean AUC": a.mean()})
    return pd.DataFrame(summary).set_index("profile")


def draco_part(df, out):
    raw = df[df.bits.isna()]
    keep = ["mesh", "base", "watertight", "manifold", "hausdorff_rel", "out_faces", "time_s",
            "peak_mem_mb"] + list(PROFILES)
    dr = df[df.bits.notna()].merge(raw[keep], on=["mesh", "base"], suffixes=("", "_raw"))
    if dr.empty:
        out.append("\nNo `+draco` rows in this CSV.\n")
        return
    out.append(f"\n{dr.mesh.nunique()} meshes, {len(dr)} Draco rows "
               f"(status: {dr.status.value_counts().to_dict()}).\n")
    g = dr.groupby("bits")
    t = pd.DataFrame({
        "median raw KB": g.raw_bytes.median() / 1024,
        "median Draco KB": g.draco_bytes.median() / 1024,
        "median ratio": g.draco_ratio.median() if "draco_ratio" in dr else g.apply(
            lambda x: (x.raw_bytes / x.draco_bytes).median()),
        "total size vs raw %": g.draco_bytes.sum() / g.raw_bytes.sum() * 100,
        "|dHausdorff| p50": g.apply(lambda x: (x.hausdorff_rel - x.hausdorff_rel_raw).abs().median()),
        "|dHausdorff| p99": g.apply(lambda x: (x.hausdorff_rel - x.hausdorff_rel_raw).abs().quantile(.99)),
        "lost watertight %": g.apply(lambda x: (x.watertight_raw & ~x.watertight).mean() * 100),
        "lost token pass": g.apply(lambda x: int((x.token_raw & ~x.token).sum())),
        "gained token pass": g.apply(lambda x: int((~x.token_raw & x.token).sum())),
    })
    out.append("**Size and quality**\n\n" + t.round(4).to_markdown() + "\n")
    if "draco_encode_s" in dr:
        c = pd.DataFrame({
            "encode s p50": g.draco_encode_s.median(), "encode s max": g.draco_encode_s.max(),
            "decode s p50": g.draco_decode_s.median(), "decode s max": g.draco_decode_s.max(),
            "extra RAM MB p50": g.draco_mem_mb.median(), "extra RAM MB p99": g.draco_mem_mb.quantile(.99),
            "extra RAM MB max": g.draco_mem_mb.max()})
        ok = raw[raw.ok]
        out.append("**Cost** (repair for comparison: median "
                   f"{ok.time_s.median():.2f} s, {ok.peak_mem_mb.median():.0f} MB peak)\n\n"
                   + c.round(4).to_markdown() + "\n")
    lw = dr[dr.watertight_raw & ~dr.watertight]
    if len(lw):
        out.append("**Watertightness lost after Draco, by chain** (rows per bit setting):\n\n"
                   + (lw.groupby(["base"]).size() / dr.bits.nunique()).astype(int)
                   .sort_values(ascending=False).to_frame("outputs").to_markdown() + "\n")
    # oracle after compression
    rows = []
    for prof in PROFILES:
        base_or = raw.groupby("mesh")[prof].any()
        r = {"profile": prof, "no Draco %": base_or.mean() * 100}
        for b in sorted(dr.bits.unique()):
            o = dr[dr.bits == b].groupby("mesh")[prof].any().reindex(base_or.index).fillna(False)
            r[f"{int(b)} bits %"] = o.mean() * 100
        rows.append(r)
    out.append("**Per-mesh oracle after compression**\n\n"
               + pd.DataFrame(rows).set_index("profile").round(1).to_markdown() + "\n")
    # lowest passing bits per (mesh, chain) that passes token before compression
    bits = sorted(dr.bits.unique())
    tp = dr[dr.token_raw].pivot_table(index=["mesh", "base"], columns="bits", values="token",
                                      aggfunc="first").fillna(False)
    by = dr[dr.token_raw].pivot_table(index=["mesh", "base"], columns="bits",
                                      values="draco_bytes", aggfunc="first")
    if len(tp):
        low = tp.apply(lambda r: next((b for b in bits if r[b]), np.nan), axis=1)
        vc = low.value_counts(dropna=False)
        txt = ", ".join(f"{'none' if pd.isna(k) else str(int(k)) + ' bits'}: {v}" for k, v in vc.items())
        sel = np.array([by.loc[i, k] if not pd.isna(k) else by.loc[i, bits[-1]]
                        for i, k in low.items()])
        out.append(f"**Lowest bits that keep a token-passing output passing** "
                   f"({len(tp)} outputs): {txt}. Adaptive bits = "
                   f"{sel.sum()/by[bits[-1]].sum()*100:.0f}% of the bytes of fixed {int(bits[-1])} bits; "
                   f"fixed {int(bits[0])} bits = {by[bits[0]].sum()/by[bits[-1]].sum()*100:.0f}%.\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--out", default="draco_report.md")
    a = ap.parse_args()
    if not os.path.exists(a.csv):
        sys.exit(f"{a.csv} not found")
    df, unread = load(a.csv)
    raw = df[df.bits.isna()]
    out = ["# MeshOpt run report (repair + Draco)\n",
           f"{df.mesh.nunique()} meshes analysed, {unread} unreadable, {raw.chain.nunique()} repair "
           f"chains, Draco bits {sorted(int(b) for b in df.bits.dropna().unique())}.\n",
           "Repair status counts: " + str(raw.status.value_counts().to_dict()) + "\n",
           "\n## A. Repair (Draco rows excluded)\n"]
    summ = repair_part(df, out)
    out.insert(4, "\n## Summary\n\n" + summ.round(3).to_markdown() + "\n")
    out.append("\n## B. Draco compression of repaired outputs\n")
    draco_part(df, out)
    text = "\n".join(out)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)


if __name__ == "__main__":
    main()
