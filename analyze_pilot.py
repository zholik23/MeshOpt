#!/usr/bin/env python3
"""
Turns pilot_results.csv into the go / no-go numbers for MeshOpt.

For each requirement PROFILE (what a downstream model needs) it answers:
  Q1  Silver bullet?   How far is the best single chain from the per-mesh oracle?
  Q2  Heavy tails?     What share of compute goes to the slowest 1% / 5% of runs?
  Q3  Predictable?     Do cheap features predict which chain fails (cross-validated AUC)?
  Q4  Worth it?        Does a learned router beat try-except (a fixed fallback cascade)?

Usage:  python analyze_pilot.py pilot_results.csv --out report.md
"""
import argparse
import numpy as np
import pandas as pd

FEATURES = ["n_faces", "n_verts", "aspect_ratio", "boundary_edge_ratio",
            "nonmanifold_edge_ratio", "degenerate_face_ratio", "n_components",
            "self_intersect_est", "sharp_edge_ratio"]

# What each kind of downstream consumer needs. Tolerances are relative to bbox diagonal = 1.
PROFILES = {
    "sdf (implicit-field models)": lambda r: r.watertight & (r.hausdorff_rel <= 0.02),
    "token (mesh LLMs, 500-10k faces)": lambda r: r.watertight & r.manifold
        & r.out_faces.between(500, 10_000) & (r.hausdorff_rel <= 0.01),
    "cad (token + sharp edges)": lambda r: r.watertight & r.manifold
        & r.out_faces.between(500, 10_000) & (r.hausdorff_rel <= 0.005)
        & (r.sharp_recall.fillna(1.0) >= 0.9),
}


def load(path):
    df = pd.read_csv(path)
    df = df[df.chain != "_load"].copy()
    for c in ["watertight", "manifold"]:
        df[c] = df[c].astype(str).str.lower().eq("true")
    for c in ["out_faces", "hausdorff_rel", "sharp_recall", "time_s"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ok"] = df.status.eq("ok")
    return df


def tail_share(times, q):
    t = np.sort(times.dropna().values)[::-1]
    k = max(1, int(np.ceil(len(t) * q)))
    return t[:k].sum() / t.sum() if t.sum() > 0 else float("nan")


def cascade_cost(succ, time, order):
    """Try chains in `order` until one succeeds; pay every attempt. Returns (success, cost)."""
    cost = 0.0
    for c in order:
        cost += time[c]
        if succ[c]:
            return True, cost
    return False, cost


def oof_success_probs(feat, succ, chains, seed=0):
    """Out-of-fold P(chain succeeds | cheap features), one classifier per chain."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.model_selection import StratifiedKFold
    probs = pd.DataFrame(index=feat.index, columns=chains, dtype=float)
    aucs = {}
    X = np.log1p(feat[FEATURES].fillna(-1).clip(lower=0))
    for c in chains:
        y = succ[c].astype(int).values
        if y.min() == y.max() or min(np.bincount(y)) < 5:
            probs[c] = y.mean()
            aucs[c] = float("nan")
            continue
        skf = StratifiedKFold(5, shuffle=True, random_state=seed)
        p = np.zeros(len(y))
        for tr, te in skf.split(X, y):
            clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05,
                                                 random_state=seed)
            clf.fit(X.iloc[tr], y[tr])
            p[te] = clf.predict_proba(X.iloc[te])[:, 1]
        probs[c] = p
        from sklearn.metrics import roc_auc_score
        aucs[c] = roc_auc_score(y, p)
    return probs, aucs


def analyze_profile(df, name, rule, out):
    df = df.copy()
    df["succ"] = df.ok & rule(df).fillna(False)
    succ = df.pivot_table(index="mesh", columns="chain", values="succ", aggfunc="first").fillna(False).astype(bool)
    time = df.pivot_table(index="mesh", columns="chain", values="time_s", aggfunc="first")
    meshes = succ.index.intersection(time.dropna().index)
    succ, time = succ.loc[meshes], time.loc[meshes]
    feat = df.groupby("mesh")[FEATURES].first().loc[meshes]
    chains = list(succ.columns)
    n = len(meshes)

    out.append(f"\n## Profile: {name}\n\n{n} meshes, {len(chains)} chains.\n")

    # per-chain table
    per = pd.DataFrame({
        "success %": succ.mean() * 100,
        "fail (timeout/oom/crash) %": df.assign(f=~df.ok).groupby("chain").f.mean().reindex(chains) * 100,
        "median time s": time.median(),
        "p99 time s": time.quantile(0.99),
        "total time h": time.sum() / 3600,
        "median hausdorff": df[df.ok].groupby("chain").hausdorff_rel.median().reindex(chains),
        "median sharp recall": df[df.ok].groupby("chain").sharp_recall.median().reindex(chains),
    }).sort_values("success %", ascending=False)
    out.append(per.round(3).to_markdown() + "\n")

    # Q1 silver bullet
    best = per.index[0]
    oracle = succ.any(axis=1)
    rescued = (oracle & ~succ[best]).sum()
    tw = time.where(succ)
    solvable = tw.notna().any(axis=1)
    cheapest_ok = tw[solvable].idxmin(axis=1)
    best_share = (cheapest_ok == best).mean() if len(cheapest_ok) else float("nan")
    gap = (oracle.mean() - succ[best].mean()) * 100
    out.append(f"**Q1 Silver bullet.** Best single chain: `{best}` "
               f"({succ[best].mean()*100:.1f}% success). Per-mesh oracle: {oracle.mean()*100:.1f}%. "
               f"Gap: **{gap:.1f} points** ({rescued} meshes only another chain can fix). "
               f"`{best}` is the cheapest successful chain for {best_share*100:.0f}% of meshes.\n")
    discard = "local" if "local" in chains else chains[0]
    out.append(f"Filter-and-discard baseline (`{discard}` then drop failures) keeps "
               f"{succ[discard].mean()*100:.1f}% of meshes.\n")

    # Q2 tails
    t_best = time[best]
    out.append(f"**Q2 Heavy tails.** For `{best}`: slowest 1% of meshes take "
               f"{tail_share(t_best, .01)*100:.0f}% of its total time, slowest 5% take "
               f"{tail_share(t_best, .05)*100:.0f}%. p99/median = "
               f"{t_best.quantile(.99)/max(t_best.median(),1e-9):.0f}x.\n")

    # Q3 predictability + Q4 routing
    probs, aucs = oof_success_probs(feat, succ, chains)
    auc_txt = ", ".join(f"`{c}` {a:.2f}" for c, a in aucs.items() if not np.isnan(a))
    out.append(f"**Q3 Predictable from cheap features?** Cross-validated AUC for "
               f"\"this chain succeeds\": {auc_txt or 'n/a (too few failures)'}. "
               f"(0.5 = random, >0.75 = useful.)\n")

    mean_t = time.median()
    fixed_order = list(mean_t.sort_values().index)          # try-except: cheapest first
    res = {"best single chain": [], "try-except cascade": [], "learned router": [], "oracle": []}
    for m in meshes:
        s, t = succ.loc[m], time.loc[m]
        res["best single chain"].append((bool(s[best]), t[best]))
        res["try-except cascade"].append(cascade_cost(s, t, fixed_order))
        # router: order by expected time-to-success = median time / P(success)
        p = probs.loc[m].clip(lower=0.02)
        order = list((mean_t / p).sort_values().index)
        res["learned router"].append(cascade_cost(s, t, order))
        ok_t = t[s]
        res["oracle"].append((bool(s.any()), ok_t.min() if s.any() else t.min()))
    rows = []
    for k, v in res.items():
        v = np.array(v, dtype=float)
        rows.append({"policy": k, "kept %": v[:, 0].mean() * 100,
                     "total compute h": v[:, 1].sum() / 3600,
                     "sec per kept mesh": v[:, 1].sum() / max(v[:, 0].sum(), 1)})
    pol = pd.DataFrame(rows).set_index("policy")
    out.append("**Q4 Policies.**\n\n" + pol.round(3).to_markdown() + "\n")

    ce = pol.loc["try-except cascade"]
    lr = pol.loc["learned router"]
    saving = (1 - lr["total compute h"] / ce["total compute h"]) * 100 if ce["total compute h"] else 0
    out.append(f"Learned router vs try-except: {lr['kept %'] - ce['kept %']:+.1f} points kept, "
               f"{saving:.0f}% less compute.\n")

    # verdict
    if gap >= 10 and saving >= 20:
        v = "GO: real heterogeneity and a router that pays off."
    elif gap >= 10:
        v = "PARTIAL: different meshes need different chains, but cheap features don't route well yet; focus on adaptive execution."
    elif tail_share(t_best, .05) >= 0.3:
        v = "NARROW: one chain mostly wins, but heavy tails make adaptive execution worth a smaller paper."
    else:
        v = "NO-GO for this profile: one chain dominates and costs are uniform."
    out.append(f"**Verdict for this profile:** {v}\n")
    return {"profile": name, "best chain": best, "gap pts": gap, "router saving %": saving,
            "top5% time share": tail_share(t_best, .05) * 100}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--out", default="pilot_report.md")
    a = ap.parse_args()
    import os, sys
    if not os.path.exists(a.csv):
        sys.exit(f"Results file '{a.csv}' not found. Run run_pilot.py first "
                 f"(it creates this file), then run this script again.")
    df = load(a.csv)
    if df.empty:
        sys.exit(f"'{a.csv}' has no results yet. Wait until run_pilot.py has processed some meshes.")
    out = ["# MeshOpt pilot report\n",
           f"{df.mesh.nunique()} meshes, {df.chain.nunique()} chains, {len(df)} runs. "
           f"Status counts: {df.status.value_counts().to_dict()}.\n",
           f"Cheap-feature extraction: median {df.groupby('mesh').feature_time_s.first().median():.3f} s "
           f"per mesh (must stay far below repair time).\n"]
    summary = [analyze_profile(df, n, r, out) for n, r in PROFILES.items()]
    out.insert(3, "\n## Summary\n\n" + pd.DataFrame(summary).set_index("profile").round(1).to_markdown()
               + "\n\nGo/no-go rule of thumb: gap >= 10 points on some profile AND router saves >= 20% "
                 "compute -> build MeshOpt. Thresholds are a starting point to agree with your advisor.\n")
    open(a.out, "w", encoding="utf-8").write("\n".join(out))
    print("\n".join(out))


if __name__ == "__main__":
    main()
