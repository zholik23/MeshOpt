#!/usr/bin/env python3
"""
MeshOpt pilot: does the "best repair chain" actually depend on the mesh?

For every mesh in a folder (e.g. Thingi10K raw_meshes/) this script:
  1. computes CHEAP features (linear time or sampled) - what an optimizer could see;
  2. runs every repair CHAIN in a separate process with a hard timeout and memory cap;
  3. records time, peak memory, failure, and output quality.

One row per (mesh, chain) is appended to a CSV, so the run can be stopped and
resumed. Then run analyze_pilot.py on the CSV.

Usage:
  python run_pilot.py --input /path/to/Thingi10K/raw_meshes --out results.csv \
      --limit 2000 --timeout 120 --mem-gb 8 --workers 4

Optional external operators (skipped if not given):
  --manifoldplus /path/to/manifold      (ManifoldPlus binary)
  --ftetwild     /path/to/FloatTetwild_bin
"""
import argparse, csv, json, os, random, subprocess, sys, tempfile, time
try:
    import resource          # not available on Windows
except ImportError:
    resource = None
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

MESH_EXT = (".stl", ".obj", ".ply", ".off", ".glb", ".gltf")
N_SAMPLES = 5_000           # surface samples for quality metrics (exact point-to-surface)
SHARP_ANGLE_DEG = 30.0      # dihedral angle that counts as a sharp edge
SHARP_TOL = 1e-3            # an edge point is "kept" if the output passes within 0.1% of bbox diag

# --------------------------------------------------------------------------- io

def load_mesh(path):
    import trimesh
    m = trimesh.load(path, force="mesh", process=False)
    if not isinstance(m, trimesh.Trimesh) or len(m.faces) == 0:
        raise ValueError("empty or non-triangle mesh")
    # STL stores a triangle soup: weld coincident vertices so edges and adjacency exist.
    # This is the same trivial step for every chain, not a repair.
    m.merge_vertices()
    return m


def normalize(m):
    """Center and scale to unit bbox diagonal, so all tolerances are relative."""
    m = m.copy()
    lo, hi = m.bounds
    diag = float(np.linalg.norm(hi - lo)) or 1.0
    m.vertices = (m.vertices - (lo + hi) / 2) / diag
    return m

# --------------------------------------------------------------- cheap features

def cheap_features(m):
    """Features an optimizer could afford BEFORE choosing a plan.
    Everything is vectorized and at most O(N log N); the self-intersection probe uses a
    fixed-size sample, so the cost stays low even on multi-million-face meshes."""
    t0 = time.perf_counter()
    f = {}
    F, V = len(m.faces), len(m.vertices)
    f["n_faces"], f["n_verts"] = int(F), int(V)
    ext = np.sort(np.maximum(m.bounds[1] - m.bounds[0], 1e-12))
    f["aspect_ratio"] = float(ext[2] / ext[0])

    faces = np.asarray(m.faces, dtype=np.int64)
    # every face edge as one int64 key (lo * V + hi); 1-D sort is much faster than row-wise unique
    e = faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
    e.sort(axis=1)
    key = e[:, 0] * V + e[:, 1]
    order = np.argsort(key, kind="stable")
    ks = key[order]
    starts = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1]])
    counts = np.diff(np.r_[starts, len(ks)])
    n_edges = max(len(counts), 1)
    f["boundary_edge_ratio"] = float((counts == 1).sum() / n_edges)
    f["nonmanifold_edge_ratio"] = float((counts > 2).sum() / n_edges)

    tri = m.vertices[faces]
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    dbl_area = np.linalg.norm(cross, axis=1)
    f["degenerate_face_ratio"] = float((dbl_area < 2e-12).sum() / max(F, 1))

    # connected components: faces linked through shared edges (same as trimesh face_adjacency)
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    face_of = np.repeat(np.arange(F), 3)[order]
    same = np.flatnonzero(ks[1:] == ks[:-1])
    g = coo_matrix((np.ones(len(same), np.int8), (face_of[same], face_of[same + 1])), shape=(F, F))
    f["n_components"] = int(connected_components(g, directed=False)[0]) if F else 0

    # sharp edges: pair the two faces of every manifold edge, compare normals
    two = counts == 2
    if two.any():
        i = starts[two]
        fa, fb = face_of[i], face_of[i + 1]
        nrm = cross / np.maximum(dbl_area, 1e-30)[:, None]
        cosang = np.einsum("ij,ij->i", nrm[fa], nrm[fb])
        f["sharp_edge_ratio"] = float((cosang < np.cos(np.radians(SHARP_ANGLE_DEG))).mean())
    else:
        f["sharp_edge_ratio"] = 0.0

    f["self_intersect_est"] = sampled_self_intersections(m, tri=tri)
    f["feature_time_s"] = time.perf_counter() - t0
    return f


def sampled_self_intersections(m, k=200, max_cand=2000, seed=0, tri=None):
    """Share of k random faces whose edges cross a nearby face that shares no vertex.
    Candidates come from a KD-tree over face centroids, so no full ray BVH is built.
    Approximate: a crossing with a very large far-away triangle can be missed."""
    try:
        from scipy.spatial import cKDTree
        faces = np.asarray(m.faces)
        if tri is None:
            tri = m.vertices[faces]
        F = len(faces)
        if F < 4:
            return 0.0
        cen = tri.mean(axis=1)
        rad = np.linalg.norm(tri - cen[:, None], axis=2).max(axis=1)
        reach = float(np.percentile(rad, 95))
        tree = cKDTree(cen)
        rng = np.random.default_rng(seed)
        probes = rng.choice(F, size=min(k, F), replace=False)
        hit = 0
        for p in probes:
            cand = np.asarray(tree.query_ball_point(cen[p], rad[p] + reach), dtype=np.int64)
            if len(cand) > max_cand:
                cand = rng.choice(cand, max_cand, replace=False)
            cand = cand[~np.isin(faces[cand], faces[p]).any(axis=1)]
            if len(cand) and _edges_cross_triangles(tri[p], tri[cand]):
                hit += 1
        return float(hit / len(probes))
    except Exception:
        return float("nan")


def _edges_cross_triangles(t, T, eps=1e-9):
    """True if any edge of triangle t passes through any triangle in T (Moller-Trumbore)."""
    v0, e1, e2 = T[:, 0], T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]
    for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
        d = b - a
        pvec = np.cross(d, e2)
        det = np.einsum("ij,ij->i", e1, pvec)
        ok = np.abs(det) > eps
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        s = a - v0
        u = np.einsum("ij,ij->i", s, pvec) * inv
        q = np.cross(s, e1)
        v = (q @ d) * inv
        tt = np.einsum("ij,ij->i", e2, q) * inv
        if np.any(ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (tt > eps) & (tt < 1 - eps)):
            return True
    return False


def sharp_edges(m):
    ang = m.face_adjacency_angles
    return m.face_adjacency_edges[ang > np.radians(SHARP_ANGLE_DEG)]

# ------------------------------------------------------------- repair chains

def to_ms(m):
    import pymeshlab
    ms = pymeshlab.MeshSet()
    ms.add_mesh(pymeshlab.Mesh(np.asarray(m.vertices, float), np.asarray(m.faces, np.int32)))
    return ms


def from_ms(ms):
    import trimesh
    cm = ms.current_mesh()
    return trimesh.Trimesh(cm.vertex_matrix(), cm.face_matrix(), process=False)


def op_local_repair(m):
    """Local, detail-preserving cleanup (PyMeshLab)."""
    ms = to_ms(m)
    ms.meshing_remove_duplicate_vertices()
    ms.meshing_remove_duplicate_faces()
    ms.meshing_remove_null_faces()
    ms.meshing_repair_non_manifold_edges()
    ms.meshing_repair_non_manifold_vertices()
    ms.meshing_remove_unreferenced_vertices()
    try:
        ms.meshing_close_holes(maxholesize=1000)
    except Exception:
        pass
    try:
        ms.meshing_re_orient_faces_coherently()
    except Exception:
        pass
    return from_ms(ms)


def op_decimate(m, target=10_000, preserve_boundary=True):
    if len(m.faces) <= target:
        return m
    ms = to_ms(m)
    ms.meshing_decimation_quadric_edge_collapse(
        targetfacenum=target, preserveboundary=preserve_boundary,
        preservenormal=True, preservetopology=True, planarquadric=True)
    return from_ms(ms)


def op_udf_mc(m, res=256, band=1.5):
    """Unsigned distance field on a res^3 grid + Marching Cubes + keep outer shell.
    CPU stand-in for the GPU recipe used by TripoSG / MeshGen / CLAY.
    Memory grows as res^3 - that is the point."""
    from scipy import ndimage
    from skimage import measure
    import trimesh
    pad = 4
    lo, hi = m.bounds
    size = (hi - lo).max()
    h = size / (res - 2 * pad)
    # rasterize the surface: dense samples -> occupied voxels
    n = int(min(max(res * res * 8, 200_000), 4_000_000))
    pts, _ = trimesh.sample.sample_surface(m, n, seed=0)
    ijk = np.floor((pts - lo) / h).astype(np.int64) + pad
    ijk = np.clip(ijk, 0, res - 1)
    surf = np.zeros((res, res, res), dtype=bool)
    surf[ijk[:, 0], ijk[:, 1], ijk[:, 2]] = True
    udf = ndimage.distance_transform_edt(~surf)          # in voxels
    solid = udf <= band
    # everything not reachable from the outside counts as inside -> fills cavities/holes
    outside = ~solid
    lab, _ = ndimage.label(outside)
    outer = lab == lab[0, 0, 0]
    occ = ~outer
    verts, faces, _, _ = measure.marching_cubes(occ.astype(np.float32), level=0.5)
    verts = (verts - pad) * h + lo
    out = trimesh.Trimesh(verts, faces, process=True)
    out.fix_normals()
    return out


def op_external(binary, kind, m, timeout):
    """ManifoldPlus or fTetWild via their CLI."""
    import trimesh
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in.obj"), os.path.join(d, "out.obj")
        m.export(src)
        if kind == "manifoldplus":
            cmd = [binary, "--input", src, "--output", dst, "--depth", "8"]
        else:  # ftetwild, extract surface
            cmd = [binary, "-i", src, "-o", os.path.join(d, "out.msh"), "--level", "2",
                   "--no-binary", "--manifold-surface"]
            dst = os.path.join(d, "out.msh__sf.obj")
        subprocess.run(cmd, check=True, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # process=False: ManifoldPlus may place distinct vertices at the same position;
        # merging them would wrongly break the (topologically closed) output.
        return trimesh.load(dst, force="mesh", process=False)


def build_chains(args):
    chains = {
        "local":            lambda m: op_local_repair(m),
        "local+dec":        lambda m: op_decimate(op_local_repair(m)),
        "udf128":           lambda m: op_udf_mc(m, 128),
        "udf256":           lambda m: op_udf_mc(m, 256),
        "udf256+dec":       lambda m: op_decimate(op_udf_mc(m, 256), preserve_boundary=False),
        "dec+udf256+dec":   lambda m: op_decimate(op_udf_mc(op_decimate(m, 200_000), 256),
                                                  preserve_boundary=False),
    }
    if args.manifoldplus:
        # ManifoldPlus wraps open/thin surfaces in a zero-thickness double shell; decimation can
        # fuse the two layers at a few edges, so a light local repair closes them again.
        chains["manifoldplus+dec+fix"] = lambda m: op_local_repair(op_decimate(
            op_external(args.manifoldplus, "manifoldplus", m, args.timeout), preserve_boundary=False))
    if args.ftetwild:
        chains["ftetwild+dec+fix"] = lambda m: op_local_repair(op_decimate(
            op_external(args.ftetwild, "ftetwild", m, args.timeout), preserve_boundary=False))
    return chains

# ------------------------------------------------------------ quality metrics

def quality(src, out):
    """Validity of the output and how much geometry it kept (all relative to bbox diag = 1)."""
    import trimesh
    q = {}
    q["out_faces"] = int(len(out.faces))
    q["watertight"] = bool(out.is_watertight)
    e = np.sort(out.edges, axis=1)
    _, c = np.unique(e, axis=0, return_counts=True)
    q["manifold"] = bool(len(c) and (c == 2).all() and out.is_winding_consistent)
    # two-sided point-to-SURFACE distance (exact closest point, not sample-to-sample)
    ps, _ = trimesh.sample.sample_surface(src, N_SAMPLES, seed=1)
    po, _ = trimesh.sample.sample_surface(out, N_SAMPLES, seed=2)
    d_so = surface_distance(out, ps)
    d_os = surface_distance(src, po)
    q["hausdorff_rel"] = float(max(np.percentile(d_so, 99), np.percentile(d_os, 99)))
    q["chamfer_rel"] = float(d_so.mean() + d_os.mean())
    # sharp-edge recall: share of points on input sharp edges that still lie on the output
    se = sharp_edges(src)
    if len(se):
        a, b = src.vertices[se[:, 0]], src.vertices[se[:, 1]]
        t = np.random.default_rng(3).random((len(se), 4))[..., None]
        pts = (a[:, None] + t * (b - a)[:, None]).reshape(-1, 3)
        if len(pts) > N_SAMPLES:
            pts = pts[np.random.default_rng(4).choice(len(pts), N_SAMPLES, replace=False)]
        q["sharp_recall"] = float((surface_distance(out, pts) < SHARP_TOL).mean())
    else:
        q["sharp_recall"] = float("nan")
    return q


def surface_distance(mesh, pts):
    from trimesh.proximity import closest_point
    return closest_point(mesh, pts)[1]

# ------------------------------------------------------------ one isolated run

def _child(path, chain_name, args_dict, conn_path):
    """Runs inside a fresh process: memory cap, run chain, measure quality, write JSON."""
    args = argparse.Namespace(**args_dict)
    if resource is not None:   # Linux/macOS: hard cap inside the process as well
        lim = int(args.mem_gb * 1024 ** 3)
        resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
    res = {}
    try:
        m = normalize(load_mesh(path))
        t0 = time.perf_counter()
        out = build_chains(args)[chain_name](m)
        res["time_s"] = time.perf_counter() - t0
        res.update(quality(m, out))
        res["status"] = "ok"
    except MemoryError:
        res["status"] = "oom"
    except Exception as e:
        res["status"] = "error"
        res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    with open(conn_path, "w") as fh:
        json.dump(res, fh)


def run_isolated(path, chain_name, args):
    """Run one (mesh, chain) in a subprocess so a hang or OOM cannot kill the pilot.
    The parent watches the child: kills it past the timeout or the memory cap and records
    its peak memory. Works on Windows, Linux and macOS."""
    import psutil
    fd, out_json = tempfile.mkstemp(suffix=".json"); os.close(fd)
    fd, err_path = tempfile.mkstemp(suffix=".err"); os.close(fd)
    cmd = [sys.executable, os.path.abspath(__file__), "--_child", path, chain_name, out_json,
           json.dumps(vars(args))]
    cap = args.mem_gb * 1024 ** 3
    peak, status = 0, None
    t0 = time.perf_counter()
    with open(err_path, "w") as err:
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
        try:
            proc = psutil.Process(p.pid)
        except psutil.Error:
            proc = None
        while p.poll() is None:
            try:
                rss = proc.memory_info().rss if proc else 0
            except psutil.Error:
                rss = 0
            peak = max(peak, rss)
            if rss > cap:
                status = "oom"
            elif time.perf_counter() - t0 > args.timeout:
                status = "timeout"
            if status:
                p.kill(); p.wait()
                break
            time.sleep(0.05)
    wall = time.perf_counter() - t0
    if status:
        res = {"status": status}
    else:
        try:
            with open(out_json) as fh:
                res = json.load(fh)
        except Exception:
            # died before writing (segfault in C++ code, OS out-of-memory kill)
            with open(err_path, errors="ignore") as fh:
                res = {"status": "crash", "error": fh.read()[-200:]}
    for f in (out_json, err_path):
        try:
            os.remove(f)
        except OSError:
            pass
    res["peak_mem_mb"] = peak / 1024 ** 2
    res["wall_s"] = wall
    if res["status"] != "ok":
        res["time_s"] = wall   # a failed attempt still costs its wall time
    return res


def process_mesh(path, args):
    rows = []
    try:
        feats = cheap_features(load_mesh(path))
    except Exception as e:
        return [{"mesh": os.path.basename(path), "chain": "_load", "status": "unreadable",
                 "error": str(e)[:200]}]
    for chain in build_chains(args):
        r = {"mesh": os.path.basename(path), "chain": chain}
        r.update(feats)
        r.update(run_isolated(path, chain, args))
        rows.append(r)
    return rows

# ---------------------------------------------------------------------- main

COLUMNS = ["mesh", "chain", "status", "error", "time_s", "wall_s", "peak_mem_mb",
           "out_faces", "watertight", "manifold", "hausdorff_rel", "chamfer_rel", "sharp_recall",
           "n_faces", "n_verts", "aspect_ratio", "boundary_edge_ratio", "nonmanifold_edge_ratio",
           "degenerate_face_ratio", "n_components", "self_intersect_est", "sharp_edge_ratio",
           "feature_time_s"]


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--_child":
        _, _, path, chain, out_json, args_json = sys.argv
        _child(path, chain, json.loads(args_json), out_json)
        return

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--input", required=True, help="folder with meshes (searched recursively)")
    ap.add_argument("--out", default="pilot_results.csv")
    ap.add_argument("--limit", type=int, default=2000, help="random subset size (0 = all)")
    ap.add_argument("--timeout", type=float, default=120, help="seconds per (mesh, chain)")
    ap.add_argument("--mem-gb", type=float, default=8, help="memory cap per (mesh, chain)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--manifoldplus", default=None)
    ap.add_argument("--ftetwild", default=None)
    args = ap.parse_args()

    files = sorted(os.path.join(r, f) for r, _, fs in os.walk(args.input)
                   for f in fs if f.lower().endswith(MESH_EXT))
    random.Random(args.seed).shuffle(files)
    if args.limit:
        files = files[:args.limit]

    done = set()
    if os.path.exists(args.out):
        with open(args.out) as fh:
            done = {row["mesh"] for row in csv.DictReader(fh)}
    todo = [f for f in files if os.path.basename(f) not in done]
    print(f"{len(files)} meshes selected, {len(done)} already done, {len(todo)} to run, "
          f"chains: {list(build_chains(args))}")

    new_file = not os.path.exists(args.out)
    with open(args.out, "a", newline="") as fh, ProcessPoolExecutor(args.workers) as pool:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        futs = {pool.submit(process_mesh, f, args): f for f in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            for row in fut.result():
                w.writerow(row)
            fh.flush()
            if i % 10 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)} meshes done", flush=True)


if __name__ == "__main__":
    main()
