# MeshOpt pilot on Thingi10K

Tests the one question everything depends on: **does the best repair chain depend on the mesh and on what the data is for, or does one chain (the "silver bullet") win everywhere?**

## Setup

```bash
pip install -r requirements.txt
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

Building them on Linux (needs `sudo apt install build-essential cmake libgmp-dev`), into the git-ignored `third_party/`:

```bash
mkdir -p third_party && cd third_party
git clone --recursive https://github.com/hjwdzh/ManifoldPlus
(cd ManifoldPlus && mkdir -p build && cd build && cmake .. -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5 && make -j8)
git clone --recursive https://github.com/wildmeshing/fTetWild
cd fTetWild && mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5   # fails once under CMake 4 (CMP0169); then:
for f in $(grep -Rl 'FetchContent_Populate(' --include=*.cmake _deps/libigl-src/cmake); do
  sed -i '0,/FetchContent_GetProperties\|FetchContent_Populate(/s//if(POLICY CMP0169)\n    cmake_policy(SET CMP0169 OLD)\nendif()\n&/' $f; done
cmake .. -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5 && make -j8
```

Then add `--manifoldplus third_party/ManifoldPlus/build/manifold --ftetwild third_party/fTetWild/build/FloatTetwild_bin`. fTetWild runs single-threaded per (mesh, chain), so its times are comparable to the other chains; large models can need a `--timeout` above 120 s.

## What it does

For each mesh: computes cheap features (face count, boundary and non-manifold edge ratio, components, sampled self-intersections, sharp-edge ratio), then runs every chain in its own process with a hard timeout and memory cap, and measures:

- status: ok / timeout / oom / crash
- runtime, peak memory
- watertight, manifold, output face count
- Hausdorff and Chamfer distance to the input (relative to bbox diagonal)
- sharp-edge recall: share of the input's sharp edges still on the output

Chains: `local` (PyMeshLab cleanup), `local+dec`, `udf128`, `udf256` (unsigned distance field + Marching Cubes, the TripoSG/MeshGen recipe), `udf256+dec`, `dec+udf256+dec`. Added on Linux: `meshfix`, `meshfix+dec` (MeshFix per part: holes, self-intersections), `wn256`, `wn256+dec` (generalized winding number + Marching Cubes: robust to overlapping parts), `cull+local`, `cull+local+dec` (drop parts hidden inside others, then repair), `local+remesh`, `udf256+remesh` (isotropic remeshing instead of QEM). Run a subset with `--chains local,meshfix,wn256+dec`.

Memory caps use a cgroup per run (`systemd-run --user --scope`, no root needed); without systemd an RSS watchdog takes over (`MESHOPT_NO_CGROUP=1` forces it).

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
