# MeshOpt pilot on Thingi10K

Tests the one question everything depends on: **does the best repair chain depend on the mesh and on what the data is for, or does one chain (the "silver bullet") win everywhere?**

## Setup

```bash
pip install trimesh pymeshlab scikit-image scipy pandas scikit-learn rtree tabulate
# Linux without a desktop may also need:  sudo apt install libopengl0
```

## Run

```bash
# 1. quick check on 50 meshes (~15-30 min)
python run_pilot.py --input /path/to/Thingi10K/raw_meshes --out pilot_results.csv \
    --limit 50 --timeout 120 --mem-gb 8 --workers 4

# 2. the real pilot: 2000 random meshes (several hours; resumable, just rerun the same command)
python run_pilot.py --input /path/to/Thingi10K/raw_meshes --out pilot_results.csv \
    --limit 2000 --timeout 120 --mem-gb 8 --workers 8

# 3. report
python analyze_pilot.py pilot_results.csv --out pilot_report.md
```

`--workers` × `--mem-gb` must fit in your RAM. Optional: `--manifoldplus <binary>` and `--ftetwild <binary>` add those operators as extra chains.

## What it does

For each mesh: computes cheap features (face count, boundary and non-manifold edge ratio, components, sampled self-intersections, sharp-edge ratio), then runs every chain in its own process with a hard timeout and memory cap, and measures:

- status: ok / timeout / oom / crash
- runtime, peak memory
- watertight, manifold, output face count
- Hausdorff and Chamfer distance to the input (relative to bbox diagonal)
- sharp-edge recall: share of the input's sharp edges still on the output

Chains: `local` (PyMeshLab cleanup), `local+dec`, `udf128`, `udf256` (unsigned distance field + Marching Cubes, the TripoSG/MeshGen recipe), `udf256+dec`, `dec+udf256+dec`.

## What the report tells you

Three requirement profiles: **sdf** (watertight only), **token** (watertight, manifold, 500–10k faces), **cad** (token + sharp edges kept). For each:

| Question | Number to look at |
| --- | --- |
| Q1 Silver bullet? | gap between the best single chain and the per-mesh oracle |
| Q2 Heavy tails? | share of compute spent on the slowest 5% of meshes |
| Q3 Predictable? | cross-validated AUC of "chain succeeds" from cheap features |
| Q4 Worth it? | learned router vs try-except cascade: data kept and compute |

Rule of thumb: **gap ≥ 10 points on some profile and router saves ≥ 20% compute → build MeshOpt.** Agree on the thresholds with your advisor before you look at the results.

## Caveats

- The UDF chain here runs on CPU. The GPU version is much faster, so treat the UDF *runtimes* as an upper bound; the *quality* results (sharp edges, overlaps) carry over.
- Thingi10K is 3D-printing models, not Objaverse. It is a fair test of operator heterogeneity, not of Objaverse-scale claims; repeat on an Objaverse sample before writing the paper.
- STL files are welded (coincident vertices merged) on load for every chain; that is not counted as repair.
