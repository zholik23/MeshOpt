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
from __future__ import annotations

import abc
import argparse
import csv
import ctypes
import enum
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

try:
    import resource          # Linux / macOS kernel resource limits
except ImportError:
    resource = None

try:
    import psutil
except ImportError:
    psutil = None

import numpy as np

MESH_EXT = (".stl", ".obj", ".ply", ".off", ".glb", ".gltf", ".drc")
N_SAMPLES = 5_000           # surface samples for quality metrics (exact point-to-surface)
SHARP_ANGLE_DEG = 30.0      # dihedral angle that counts as a sharp edge
SHARP_TOL = 1e-3            # an edge point is "kept" if the output passes within 0.1% of bbox diag

# ---------------------------------------------------------------------------
# Structured Status & Execution Result
# ---------------------------------------------------------------------------

class ExecutionStatus(str, enum.Enum):
    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    OOM = "OOM"
    ERROR = "ERROR"

    @property
    def csv_status(self) -> str:
        """Maps structured status to CSV schema expected by analyze_pilot.py."""
        return {
            ExecutionStatus.SUCCESS: "ok",
            ExecutionStatus.TIMEOUT: "timeout",
            ExecutionStatus.OOM: "oom",
            ExecutionStatus.ERROR: "error",
        }[self]


@dataclass
class OperatorResult:
    status: ExecutionStatus
    execution_time: float
    wall_time: float = 0.0
    peak_mem_mb: float = 0.0
    error_message: str = ""
    returncode: Optional[int] = None
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_csv_dict(self) -> Dict[str, Any]:
        """Convert result into the flat dictionary format used by the pilot CSV."""
        row: Dict[str, Any] = {
            "status": self.status.csv_status,
            "error": self.error_message[:200] if self.error_message else "",
            "time_s": self.execution_time,
            "wall_s": self.wall_time or self.execution_time,
            "peak_mem_mb": self.peak_mem_mb,
        }
        row.update(self.metrics)
        return row


# ---------------------------------------------------------------------------
# Linux Resource Control (cgroup v2 via systemd-run, RSS watchdog fallback)
# ---------------------------------------------------------------------------
#
# Each (mesh, chain) child runs in its own transient systemd scope, i.e. its own cgroup,
# with MemoryMax set to --mem-gb. The kernel then counts the real RAM of the child AND every
# process it starts (e.g. ManifoldPlus / fTetWild) and OOM-kills inside that cgroup only when
# the cap is crossed. This is the Linux counterpart of a Windows Job Object memory limit and
# needs no root: systemd delegates the memory controller to the user session.
#
# If systemd-run is missing or the user manager is unreachable (containers, plain ssh without
# lingering), we fall back to an RSS watchdog in the parent that kills the child's process
# group once it crosses the cap. RLIMIT_AS is not used: it caps VIRTUAL memory, which numpy,
# OpenBLAS and pymeshlab reserve far beyond what they touch, so it fails healthy runs.
# Set MESHOPT_NO_CGROUP=1 to force the fallback.

_cgroup_ok: Optional[bool] = None


def _cgroup_available() -> bool:
    """True if `systemd-run --user --scope` can start a process in a memory-capped cgroup.
    Probed once per process."""
    global _cgroup_ok
    if _cgroup_ok is None:
        _cgroup_ok = False
        exe = shutil.which("systemd-run")
        if exe and os.environ.get("MESHOPT_NO_CGROUP") != "1":
            try:
                r = subprocess.run(
                    [exe, "--user", "--scope", "--quiet", "--collect", "-p", "MemoryMax=64M", "--", "true"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                _cgroup_ok = r.returncode == 0
            except Exception:
                pass
    return _cgroup_ok


# Every run is single-threaded: the pool already keeps all cores busy, and BLAS / OpenMP
# (PyMeshLab) / libigl would each start one thread per core in every run, oversubscribing
# the CPU and making timings depend on how many workers run at once.
_SINGLE_THREAD_ENV = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
                          MKL_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1", IGL_NUM_THREADS="1")

_SLICE = "meshopt.slice"           # parent cgroup of every run, capped by --total-mem-gb


def _cap_all_runs(total_mem_gb: float) -> None:
    """Caps the summed RAM of all concurrent runs, so many workers with a generous per-run
    cap can never exhaust the machine; if the total is hit the kernel kills the largest run."""
    if total_mem_gb > 0 and _cgroup_available():
        subprocess.run(["systemctl", "--user", "set-property", "--runtime", _SLICE,
                        f"MemoryMax={int(total_mem_gb * 1024 ** 3)}", "MemorySwapMax=0"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _cgroup_wrap(cmd: List[str], max_mem_mb: float) -> List[str]:
    """Prefixes `cmd` so it runs in its own cgroup capped at `max_mem_mb` of RAM, with swap
    disabled so an over-budget run is killed instead of thrashing. systemd-run --scope execs
    the command in place, so the PID we get back from Popen is the command itself."""
    limit_bytes = int(max_mem_mb * 1024 * 1024)
    return [
        "systemd-run", "--user", "--scope", "--quiet", "--collect", f"--slice={_SLICE}",
        "-p", f"MemoryMax={limit_bytes}",
        "-p", "MemorySwapMax=0",
        "--",
    ] + cmd


def _die_with_parent() -> None:
    """prctl(PR_SET_PDEATHSIG, SIGKILL): the kernel kills this process if the pool worker that
    started it dies (Ctrl-C, crash), so no orphaned children keep eating RAM."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    except Exception:
        pass
    # The parent may have died before prctl took effect.
    if os.getppid() == 1:
        os._exit(1)


def _kill_process_tree(pid: int) -> None:
    """SIGKILLs the child's whole process group. Children are started with
    start_new_session=True, so the group id equals the child's PID and also covers any
    external binary it launched."""
    if pid <= 0:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def _sample_process_tree_rss_bytes(pid: int) -> int:
    """Sum of resident memory of `pid` and all of its descendants."""
    if psutil is None or pid <= 0:
        return 0
    try:
        root = psutil.Process(pid)
        procs = [root] + root.children(recursive=True)
    except psutil.Error:
        return 0
    total = 0
    for p in procs:
        try:
            total += int(p.memory_info().rss)
        except psutil.Error:
            continue
    return total


class _ProcessTreeWatchdog:
    """Background thread that samples the RSS of a running child (and its descendants) to
    record peak memory. When `enforce` is set (no cgroup available) it also kills the tree
    once the cap is crossed."""

    def __init__(self, pid: int, max_mem_mb: float, enforce: bool, poll_interval: float = 0.05):
        self.pid = pid
        self.cap_bytes = int(max_mem_mb * 1024 * 1024)
        self.enforce = enforce
        self.poll_interval = poll_interval
        self.peak_bytes: int = 0
        self.killed_for_oom: bool = False
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        if psutil is not None:
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            rss = _sample_process_tree_rss_bytes(self.pid)
            if rss > self.peak_bytes:
                self.peak_bytes = rss
            if self.enforce and self.cap_bytes > 0 and rss > self.cap_bytes:
                self.killed_for_oom = True
                _kill_process_tree(self.pid)
                break
            self._stop_event.wait(self.poll_interval)


def _classify_failure(returncode: int, stderr_text: str, child_payload: Optional[Dict[str, Any]] = None) -> tuple[ExecutionStatus, str]:
    """Determines whether a non-zero exit or child failure was an OOM or general ERROR.
    A SIGKILL we did not send ourselves comes from the cgroup OOM killer."""
    if child_payload and child_payload.get("status") == ExecutionStatus.OOM.value:
        return ExecutionStatus.OOM, child_payload.get("error", "MemoryError in child worker")

    oom_tokens = ("MemoryError", "bad_alloc", "Out of memory", "out of memory", "Cannot allocate memory", "Killed")

    if returncode in (-signal.SIGKILL, 128 + signal.SIGKILL):
        return ExecutionStatus.OOM, f"Process killed by OS memory limit (returncode={returncode})"

    combined_err = (stderr_text or "") + " " + (child_payload.get("error", "") if child_payload else "")
    if any(tok in combined_err for tok in oom_tokens):
        return ExecutionStatus.OOM, combined_err.strip()[-200:]

    if child_payload and child_payload.get("error"):
        return ExecutionStatus.ERROR, child_payload["error"][:200]

    msg = stderr_text.strip()[-200:] if stderr_text else f"Subprocess exited with code {returncode}"
    return ExecutionStatus.ERROR, msg


class BaseMeshOperator(abc.ABC):
    """Abstract base class that standardizes isolated subprocess execution, timeout,
    cgroup memory limits (RSS watchdog as fallback), process-tree cleanup,
    and temporary file cleanup for any mesh repair method."""

    def __init__(self, name: str, timeout_s: float = 120.0, max_mem_mb: float = 8192.0):
        self.name = name
        self.timeout_s = float(timeout_s)
        self.max_mem_mb = float(max_mem_mb)

    @abc.abstractmethod
    def run_in_worker(self, mesh, timeout_s: float, max_mem_mb: float, work_dir: str):
        """Executes the actual repair logic inside the isolated worker process."""
        raise NotImplementedError

    @staticmethod
    def run_external_command(
        cmd: List[str],
        timeout_s: float,
        max_mem_mb: float,
        cwd: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """Executes a third-party C++ binary from inside the child worker. It inherits the
        child's cgroup and process group, so the memory cap and the parent's
        kill-on-timeout already cover it; here we only add its own timeout."""
        return subprocess.run(
            cmd,
            cwd=cwd,
            check=True,
            timeout=timeout_s,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )

    def execute(self, mesh_path: str, cli_args: argparse.Namespace) -> OperatorResult:
        """Runs this operator on `mesh_path` in an isolated child process (own cgroup, own
        process group). Guarantees process-tree termination and cleanup of temporary
        directories even on OOM, SIGKILL or TIMEOUT."""
        t0 = time.perf_counter()
        # Dedicated temporary directory for this (mesh, chain) run; wiped in `finally`
        work_dir = tempfile.mkdtemp(prefix=f"meshopt_{self.name.replace('+', '_')}_")
        out_json = os.path.join(work_dir, "result.json")

        cmd = [
            sys.executable,
            os.path.abspath(__file__),
            "--_child",
            mesh_path,
            self.name,
            out_json,
            work_dir,
            json.dumps(vars(cli_args)),
        ]
        use_cgroup = _cgroup_available()
        if use_cgroup:
            cmd = _cgroup_wrap(cmd, self.max_mem_mb)

        proc: Optional[subprocess.Popen] = None
        watchdog: Optional[_ProcessTreeWatchdog] = None
        try:
            proc = subprocess.Popen(
                cmd,
                env=_SINGLE_THREAD_ENV,
                start_new_session=True,      # own process group -> killpg reaches all descendants
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
            )
            watchdog = _ProcessTreeWatchdog(proc.pid, self.max_mem_mb, enforce=not use_cgroup)
            watchdog.start()
            _, stderr = proc.communicate(timeout=self.timeout_s)
            wall = time.perf_counter() - t0
            watchdog.stop()

            watchdog_peak_mb = watchdog.peak_bytes / (1024.0 ** 2)

            if watchdog.killed_for_oom:
                return OperatorResult(
                    status=ExecutionStatus.OOM,
                    execution_time=wall,
                    wall_time=wall,
                    peak_mem_mb=watchdog_peak_mb,
                    error_message=f"Process exceeded memory cap ({self.max_mem_mb:.0f} MB)",
                    returncode=proc.returncode,
                )

            payload: Optional[Dict[str, Any]] = None
            if os.path.exists(out_json):
                try:
                    with open(out_json, "r", encoding="utf-8") as fh:
                        payload = json.load(fh)
                except Exception:
                    payload = None

            child_peak_mb = float(payload.get("peak_mem_mb", 0.0)) if payload else 0.0
            peak_mem_mb = max(child_peak_mb, watchdog_peak_mb)

            if proc.returncode == 0 and payload and payload.get("status") == ExecutionStatus.SUCCESS.value:
                return OperatorResult(
                    status=ExecutionStatus.SUCCESS,
                    execution_time=float(payload.get("time_s", wall)),
                    wall_time=wall,
                    peak_mem_mb=peak_mem_mb,
                    returncode=0,
                    metrics=payload.get("metrics", {}),
                )

            status, err_msg = _classify_failure(proc.returncode, stderr, payload)
            return OperatorResult(
                status=status,
                execution_time=wall,
                wall_time=wall,
                peak_mem_mb=peak_mem_mb,
                error_message=err_msg,
                returncode=proc.returncode,
            )

        except subprocess.TimeoutExpired:
            wall = time.perf_counter() - t0
            return OperatorResult(
                status=ExecutionStatus.TIMEOUT,
                execution_time=wall,
                wall_time=wall,
                peak_mem_mb=watchdog.peak_bytes / (1024.0 ** 2) if watchdog else 0.0,
                error_message=f"Timed out after {self.timeout_s:.1f}s",
            )
        except Exception as e:
            wall = time.perf_counter() - t0
            return OperatorResult(
                status=ExecutionStatus.ERROR,
                execution_time=wall,
                wall_time=wall,
                peak_mem_mb=watchdog.peak_bytes / (1024.0 ** 2) if watchdog else 0.0,
                error_message=f"{type(e).__name__}: {str(e)[:200]}",
            )
        finally:
            if watchdog is not None:
                watchdog.stop()
            if proc is not None and proc.returncode is None:
                # Timeout or error: the child is still running, so kill its whole group and reap it.
                _kill_process_tree(proc.pid)
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
            # Always clean up temporary files created by the worker or crashed C++ binaries
            shutil.rmtree(work_dir, ignore_errors=True)


class CallableChainOperator(BaseMeshOperator):
    """Concrete operator wrapping a Python/C++ repair chain function."""

    def __init__(
        self,
        name: str,
        fn: Callable[[Any, float, float, str], Any],
        timeout_s: float = 120.0,
        max_mem_mb: float = 8192.0,
    ):
        super().__init__(name=name, timeout_s=timeout_s, max_mem_mb=max_mem_mb)
        self._fn = fn

    def run_in_worker(self, mesh, timeout_s: float, max_mem_mb: float, work_dir: str):
        return self._fn(mesh, timeout_s, max_mem_mb, work_dir)


# --------------------------------------------------------------------------- io

def load_mesh(path):
    import trimesh
    ext = os.path.splitext(path)[1].lower()
    if ext == ".drc":
        m = _load_drc(path)
    elif ext in (".glb", ".gltf") and _gltf_uses_draco(path):
        # trimesh cannot read KHR_draco_mesh_compression; decode those primitives with DracoPy
        m = _load_gltf_draco(path)
    else:
        m = trimesh.load(path, force="mesh", process=False)
    if not isinstance(m, trimesh.Trimesh) or len(m.faces) == 0:
        raise ValueError("empty or non-triangle mesh")
    # STL stores a triangle soup: weld coincident vertices so edges and adjacency exist.
    # This is the same trivial step for every chain, not a repair.
    m.merge_vertices()
    return m


# ------------------------------------------------------------------ draco (optional)
# DracoPy is imported lazily: nothing here is needed unless the input has Draco data or
# --draco-bits is set, so the script still runs without it installed.

def _draco():
    try:
        import DracoPy
    except ImportError as e:
        raise RuntimeError("DracoPy is not installed (pip install DracoPy)") from e
    return DracoPy


def _draco_to_trimesh(blob):
    import trimesh
    d = _draco().decode(blob)
    v = np.asarray(d.points, dtype=np.float64).reshape(-1, 3)
    f = np.asarray(d.faces, dtype=np.int64).reshape(-1, 3)
    if len(f) == 0:
        raise ValueError("Draco data has no faces (point cloud)")
    return trimesh.Trimesh(v, f, process=False)


def _load_drc(path):
    with open(path, "rb") as fh:
        return _draco_to_trimesh(fh.read())


def _gltf_read(path):
    """Returns (json, embedded GLB binary chunk or None)."""
    import struct
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:4] != b"glTF":
        return json.loads(data.decode("utf-8")), None
    js, binc, off = None, None, 12
    while off + 8 <= len(data):
        ln, typ = struct.unpack_from("<I4s", data, off)
        off += 8
        chunk = data[off:off + ln]
        off += ln
        if typ == b"JSON":
            js = json.loads(chunk.decode("utf-8"))
        elif typ == b"BIN\x00":
            binc = chunk
    if js is None:
        raise ValueError("GLB without JSON chunk")
    return js, binc


def _gltf_uses_draco(path):
    try:
        js, _ = _gltf_read(path)
    except Exception:
        return False
    return "KHR_draco_mesh_compression" in js.get("extensionsUsed", [])


def _gltf_buffer(js, i, binc, base_dir, cache):
    if i in cache:
        return cache[i]
    uri = js["buffers"][i].get("uri")
    if uri is None:
        data = binc
    elif uri.startswith("data:"):
        import base64
        data = base64.b64decode(uri.split(",", 1)[1])
    else:
        from urllib.parse import unquote
        with open(os.path.join(base_dir, unquote(uri)), "rb") as fh:
            data = fh.read()
    cache[i] = data
    return data


def _gltf_view(js, vi, binc, base_dir, cache):
    bv = js["bufferViews"][vi]
    buf = _gltf_buffer(js, bv["buffer"], binc, base_dir, cache)
    o = bv.get("byteOffset", 0)
    return buf[o:o + bv["byteLength"]], bv.get("byteStride")


_GLTF_DTYPE = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16,
               5125: np.uint32, 5126: np.float32}
_GLTF_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def _gltf_accessor(js, ai, binc, base_dir, cache):
    """Plain (non-Draco) accessor -> numpy array (count, ncomp). Sparse accessors unsupported."""
    acc = js["accessors"][ai]
    dt = np.dtype(_GLTF_DTYPE[acc["componentType"]])
    nc = _GLTF_NCOMP[acc["type"]]
    n = acc["count"]
    view, stride = _gltf_view(js, acc["bufferView"], binc, base_dir, cache)
    o = acc.get("byteOffset", 0)
    item = dt.itemsize * nc
    if stride and stride != item:
        raw = np.frombuffer(view, np.uint8, count=(n - 1) * stride + item, offset=o)
        rows = np.lib.stride_tricks.as_strided(raw, (n, item), (stride, 1))
        return np.ascontiguousarray(rows).view(dt).reshape(n, nc)
    return np.frombuffer(view, dt, count=n * nc, offset=o).reshape(n, nc)


def _node_matrix(node):
    if "matrix" in node:
        return np.asarray(node["matrix"], dtype=np.float64).reshape(4, 4).T  # column-major
    t = np.asarray(node.get("translation", [0, 0, 0]), dtype=np.float64)
    x, y, z, w = node.get("rotation", [0, 0, 0, 1])
    sc = np.asarray(node.get("scale", [1, 1, 1]), dtype=np.float64)
    r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    M = np.eye(4)
    M[:3, :3] = r * sc
    M[:3, 3] = t
    return M


def _load_gltf_draco(path):
    """Reads a .glb/.gltf whose primitives may use KHR_draco_mesh_compression: decodes each
    triangle primitive (Draco or plain), applies node transforms, concatenates into one mesh."""
    import trimesh
    js, binc = _gltf_read(path)
    base_dir, cache = os.path.dirname(os.path.abspath(path)), {}

    def prim_mesh(p):
        if p.get("mode", 4) != 4:                    # triangles only
            return None
        ext = p.get("extensions", {}).get("KHR_draco_mesh_compression")
        if ext is not None:
            view, _ = _gltf_view(js, ext["bufferView"], binc, base_dir, cache)
            return _draco_to_trimesh(bytes(view))
        if "POSITION" not in p.get("attributes", {}):
            return None
        v = _gltf_accessor(js, p["attributes"]["POSITION"], binc, base_dir, cache).astype(np.float64)
        if "indices" in p:
            f = _gltf_accessor(js, p["indices"], binc, base_dir, cache).reshape(-1, 3).astype(np.int64)
        else:
            f = np.arange(len(v) - len(v) % 3, dtype=np.int64).reshape(-1, 3)
        return trimesh.Trimesh(v, f, process=False)

    mesh_cache = {}

    def mesh_parts(mi):
        if mi not in mesh_cache:
            mesh_cache[mi] = [pm for pm in (prim_mesh(p) for p in js["meshes"][mi]["primitives"])
                              if pm is not None and len(pm.faces)]
        return mesh_cache[mi]

    parts = []
    nodes = js.get("nodes", [])
    scenes = js.get("scenes", [])
    if scenes:
        roots = scenes[js.get("scene", 0)].get("nodes", [])
        stack = [(n, np.eye(4)) for n in roots]
        while stack:
            ni, parent = stack.pop()
            node = nodes[ni]
            M = parent @ _node_matrix(node)
            if "mesh" in node:
                for pm in mesh_parts(node["mesh"]):
                    pm = pm.copy()
                    pm.apply_transform(M)
                    parts.append(pm)
            stack.extend((c, M) for c in node.get("children", []))
    else:
        for mi in range(len(js.get("meshes", []))):
            parts.extend(pm.copy() for pm in mesh_parts(mi))
    if not parts:
        raise ValueError("glTF has no triangle primitives")
    return trimesh.util.concatenate(parts) if len(parts) > 1 else parts[0]


def op_draco(m, bits, level=7):
    """Draco round trip: encode positions+faces with `bits`-bit quantization, decode, and
    return the decoded (lossy) mesh. Sizes go to out.metadata for the CSV."""
    v = np.ascontiguousarray(m.vertices, dtype=np.float32)
    f = np.ascontiguousarray(m.faces, dtype=np.uint32)
    t0 = time.perf_counter()
    blob = _draco().encode(v, f, quantization_bits=int(bits), compression_level=int(level))
    t1 = time.perf_counter()
    out = _draco_to_trimesh(blob)
    t2 = time.perf_counter()
    out.metadata["draco_encode_s"] = t1 - t0
    out.metadata["draco_decode_s"] = t2 - t1
    # No merge_vertices here: Draco keeps the connectivity it was given, and welding coincident
    # vertices would wrongly break watertight double-layer outputs (ManifoldPlus, fTetWild).
    out.metadata["draco_bits"] = int(bits)
    out.metadata["draco_bytes"] = len(blob)
    out.metadata["raw_bytes"] = int(v.nbytes + f.nbytes)   # float32 xyz + uint32 indices
    return out


class _PeakMem:
    """Extra RAM (MiB) a block of code needs above what the process already holds.
    Linux: resets the kernel's peak-RSS counter (VmHWM) and reads it back, so even a
    millisecond-long native allocation inside DracoPy is counted. Elsewhere: samples RSS."""

    def __enter__(self):
        self.mb, self._hwm = 0.0, False
        try:
            with open("/proc/self/clear_refs", "w") as fh:
                fh.write("5")
            self._hwm = True
        except OSError:
            pass
        self.base = self._status("VmRSS") if self._hwm else self._rss()
        self._peak, self._stop = self.base, threading.Event()
        if not self._hwm and psutil is not None:
            self._t = threading.Thread(target=self._sample, daemon=True)
            self._t.start()
        return self

    def __exit__(self, *exc):
        if self._hwm:
            self._peak = max(self._peak, self._status("VmHWM"))
        else:
            self._stop.set()
            if psutil is not None:
                self._t.join()
            self._peak = max(self._peak, self._rss())
        self.mb = max(0.0, self._peak - self.base)
        return False

    @staticmethod
    def _status(key):
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith(key + ":"):
                    return int(line.split()[1]) / 1024.0      # kB -> MiB
        return 0.0

    @staticmethod
    def _rss():
        return psutil.Process().memory_info().rss / 1024.0 ** 2 if psutil is not None else 0.0

    def _sample(self):
        while not self._stop.wait(0.002):
            self._peak = max(self._peak, self._rss())


def _parse_draco_bits(args):
    raw = getattr(args, "draco_bits", None)
    return [int(b) for b in str(raw).split(",") if b.strip()] if raw else []


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


def op_fix_if_broken(m):
    """Local repair only when the mesh is not already closed and manifold. Volume meshers
    (ManifoldPlus, fTetWild) emit valid surfaces that may touch themselves at a vertex;
    running the repair on those merges the touching vertices and makes them non-manifold."""
    if m.is_watertight and m.is_winding_consistent:
        return m
    return op_local_repair(m)


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


def split_parts(m):
    """Connected components as compact (vertices, faces) pairs. trimesh's split() gives each
    part a copy of the full vertex buffer, which needs GBs on models with hundreds of parts."""
    from trimesh.graph import connected_component_labels
    labels = connected_component_labels(m.face_adjacency, node_count=len(m.faces))
    order = np.argsort(labels, kind="stable")
    bounds = np.flatnonzero(np.diff(labels[order])) + 1
    parts = []
    for fids in np.split(order, bounds):
        used, f = np.unique(m.faces[fids], return_inverse=True)
        parts.append((np.asarray(m.vertices[used], np.float64), f.reshape(-1, 3)))
    return parts


def op_meshfix(m):
    """MeshFix (Attene): fills holes and removes self-intersections, a second local repair.
    MeshFix always returns a single component, so each part is repaired on its own and the
    results are joined; parts MeshFix deletes entirely (e.g. flat sheets) are dropped."""
    import pymeshfix
    import trimesh
    out_v, out_f, n = [], [], 0
    for v, f in split_parts(m):
        # MeshFix sometimes deletes a valid closed part as-is but keeps it with flipped
        # orientation; retry flipped and flip the result back
        for flip in (False, True):
            mf = pymeshfix.MeshFix(v, (f[:, ::-1] if flip else f).astype(np.int32))
            mf.repair(joincomp=False, remove_smallest_components=False)
            if len(mf.faces):
                break
        if len(mf.faces):
            out_v.append(mf.points)
            out_f.append((mf.faces[:, ::-1] if flip else mf.faces) + n)
            n += len(mf.points)
    if not out_f:
        raise RuntimeError("meshfix removed every face")
    return trimesh.Trimesh(np.concatenate(out_v), np.concatenate(out_f), process=False)


def op_remesh(m, target=8_000, featuredeg=SHARP_ANGLE_DEG, iterations=5, rounds=3):
    """Isotropic explicit remeshing (PyMeshLab) to about `target` faces: an alternative to QEM
    decimation that gives well-shaped triangles. The edge length comes from the surface area
    (equilateral triangles) and is rescaled from the face count actually reached if a round
    still lands above 10k. Edges sharper than `featuredeg` are kept as features; pass ~60 for
    Marching Cubes output, whose voxel staircase would otherwise lock most edges."""
    import pymeshlab
    if len(m.faces) <= target:
        return m
    edge = float(np.sqrt(4.0 * m.area / (np.sqrt(3.0) * target)))
    for _ in range(rounds):
        ms = to_ms(m)
        ms.meshing_isotropic_explicit_remeshing(
            iterations=iterations, targetlen=pymeshlab.PureValue(edge), featuredeg=featuredeg)
        out = from_ms(ms)
        if len(out.faces) <= 10_000:
            break
        edge *= float(np.sqrt(len(out.faces) / target))
    return out


def op_winding_mc(m, res=256):
    """Generalized winding number (libigl, Jacobson et al. 2013) on a res^3 grid + Marching
    Cubes at 0.5. Robust inside/outside for overlapping, self-intersecting and slightly open
    parts; thin open sheets with no volume vanish. Same grid layout as op_udf_mc."""
    import igl
    from skimage import measure
    import trimesh
    pad = 4
    lo, hi = m.bounds
    size = (hi - lo).max()
    h = size / (res - 2 * pad)
    # samples at cell centres: the bbox faces (and any plane aligned with them) fall between
    # samples instead of on them, where w == 0.5 exactly makes Marching Cubes degenerate
    axis = (np.arange(res) - pad + 0.5) * h
    gx, gy, gz = np.meshgrid(axis + lo[0], axis + lo[1], axis + lo[2], indexing="ij")
    q = np.stack([gx.ravel(), gy.ravel(), gz.ravel()], axis=1)
    del gx, gy, gz
    w = igl.fast_winding_number(np.asarray(m.vertices, np.float64), np.asarray(m.faces, np.int64), q)
    del q
    # |w|: parts with inverted orientation count as inside too
    vol = np.abs(w).reshape(res, res, res).astype(np.float32)
    vol[np.abs(vol - 0.5) < 1e-4] = 0.5 - 1e-4
    if vol.max() <= 0.5:
        raise RuntimeError("winding number never exceeds 0.5: no enclosed volume")
    verts, faces, _, _ = measure.marching_cubes(vol, level=0.5)
    verts = (verts - pad + 0.5) * h + lo
    out = trimesh.Trimesh(verts, faces, process=True)
    out.fix_normals()
    return out


def op_remove_hidden(m, samples_per_part=64, eps=1e-3, hidden_share=0.9):
    """Drops components fully enclosed by the rest of the model (engine internals, inner
    copies), which only add faces and overlaps. A part is hidden if points pushed just off
    BOTH sides of its surface are inside the other parts (|winding number| > 0.5). The winding
    number is additive over faces, so the other parts' share is the whole mesh's value minus
    the part's own: one evaluation on the whole mesh plus one small one per part."""
    import igl
    import trimesh
    parts = [trimesh.Trimesh(v, f, process=False) for v, f in split_parts(m)]
    parts = [p for p in parts if p.area > 0]
    if len(parts) < 2:
        return m
    k = samples_per_part
    pts, own = [], []
    for i, p in enumerate(parts):
        s, fi = trimesh.sample.sample_surface(p, k, seed=i)
        n = p.face_normals[fi]
        q = np.concatenate([s + eps * n, s - eps * n])
        pts.append(q)
        own.append(igl.fast_winding_number(p.vertices, np.asarray(p.faces, np.int64), q))
    w = igl.fast_winding_number(np.asarray(m.vertices, np.float64),
                                np.asarray(m.faces, np.int64), np.concatenate(pts))
    others = w - np.concatenate(own)
    # (part, side, sample); |w| so parts with inverted orientation still enclose
    inside = (np.abs(others).reshape(len(parts), 2, k) > 0.5).all(axis=1)
    hidden = inside.mean(axis=1) >= hidden_share
    keep = [p for p, h in zip(parts, hidden) if not h]
    if not keep:
        return m
    return trimesh.util.concatenate(keep)


def op_external(binary, kind, m, timeout, max_mem_mb=8192.0, work_dir=None):
    """ManifoldPlus or fTetWild via their CLI, isolated with subprocess.run,
    strict timeout, the child's cgroup memory cap, and guaranteed temp file cleanup."""
    import trimesh
    tmp_dir = tempfile.mkdtemp(prefix=f"ext_{kind}_", dir=work_dir)
    try:
        src, dst = os.path.join(tmp_dir, "in.obj"), os.path.join(tmp_dir, "out.obj")
        m.export(src)
        if kind == "manifoldplus":
            cmd = [binary, "--input", src, "--output", dst, "--depth", "8"]
        else:  # ftetwild, extract surface
            # one thread per run: the pool already keeps every core busy, and fTetWild's
            # default of all cores would oversubscribe the machine and skew its timings
            cmd = [binary, "-i", src, "-o", os.path.join(tmp_dir, "out.msh"), "--level", "2",
                   "--no-binary", "--manifold-surface", "--max-threads", "1"]
            dst = os.path.join(tmp_dir, "out.msh__sf.obj")

        BaseMeshOperator.run_external_command(
            cmd=cmd,
            timeout_s=timeout,
            max_mem_mb=max_mem_mb,
            cwd=tmp_dir,
        )
        if not os.path.exists(dst):
            raise RuntimeError(f"{kind} finished without producing output file")
        # process=False: ManifoldPlus may place distinct vertices at the same position;
        # merging them would wrongly break the (topologically closed) output.
        return trimesh.load(dst, force="mesh", process=False)
    except subprocess.CalledProcessError as e:
        status, msg = _classify_failure(e.returncode, e.stderr or "")
        if status == ExecutionStatus.OOM:
            raise MemoryError(msg) from e
        raise RuntimeError(f"{kind} failed (code {e.returncode}): {msg}") from e
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def build_operators(args) -> Dict[str, BaseMeshOperator]:
    """Constructs the suite of fault-tolerant BaseMeshOperator instances."""
    timeout_s = float(args.timeout)
    max_mem_mb = float(args.mem_gb) * 1024.0

    specs: Dict[str, Callable[[Any, float, float, str], Any]] = {
        "local":          lambda m, t, mem, wd: op_local_repair(m),
        "local+dec":      lambda m, t, mem, wd: op_decimate(op_local_repair(m)),
        "udf128":         lambda m, t, mem, wd: op_udf_mc(m, 128),
        "udf256":         lambda m, t, mem, wd: op_udf_mc(m, 256),
        "udf256+dec":     lambda m, t, mem, wd: op_decimate(op_udf_mc(m, 256), preserve_boundary=False),
        "dec+udf256+dec": lambda m, t, mem, wd: op_decimate(
            op_udf_mc(op_decimate(m, 200_000), 256), preserve_boundary=False
        ),
        "meshfix":        lambda m, t, mem, wd: op_meshfix(m),
        "meshfix+dec":    lambda m, t, mem, wd: op_decimate(op_meshfix(m)),
        "wn256":          lambda m, t, mem, wd: op_winding_mc(m, 256),
        "wn256+dec":      lambda m, t, mem, wd: op_decimate(op_winding_mc(m, 256), preserve_boundary=False),
        "cull+local":     lambda m, t, mem, wd: op_local_repair(op_remove_hidden(m)),
        "cull+local+dec": lambda m, t, mem, wd: op_decimate(op_local_repair(op_remove_hidden(m))),
        "local+remesh":   lambda m, t, mem, wd: op_remesh(op_local_repair(m)),
        "udf256+remesh":  lambda m, t, mem, wd: op_remesh(op_udf_mc(m, 256), featuredeg=60.0),
    }
    if args.manifoldplus:
        # ManifoldPlus wraps open/thin surfaces in a zero-thickness double shell; decimation can
        # fuse the two layers at a few edges, so a light local repair closes them again.
        specs["manifoldplus+dec+fix"] = lambda m, t, mem, wd: op_fix_if_broken(
            op_decimate(
                op_external(args.manifoldplus, "manifoldplus", m, t, mem, wd),
                preserve_boundary=False,
            )
        )
    if args.ftetwild:
        specs["ftetwild+dec+fix"] = lambda m, t, mem, wd: op_fix_if_broken(
            op_decimate(
                op_external(args.ftetwild, "ftetwild", m, t, mem, wd),
                preserve_boundary=False,
            )
        )

    if getattr(args, "chains", None):
        wanted = [c.strip() for c in args.chains.split(",") if c.strip()]
        unknown = [c for c in wanted if c not in specs]
        if unknown:
            raise SystemExit(f"unknown chain(s) {unknown}; available: {list(specs)}")
        specs = {c: specs[c] for c in wanted}

    return {
        name: CallableChainOperator(name=name, fn=fn, timeout_s=timeout_s, max_mem_mb=max_mem_mb)
        for name, fn in specs.items()
    }


def build_chains(args):
    """Backwards-compatible helper returning operator names -> callables."""
    ops = build_operators(args)
    return {
        name: (lambda m, _op=op: _op.run_in_worker(m, _op.timeout_s, _op.max_mem_mb, tempfile.gettempdir()))
        for name, op in ops.items()
    }

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

# ------------------------------------------------------------ isolated worker & pipeline

def _get_peak_rss_mb() -> float:
    """Returns the high-water mark RSS of this process (or its largest child) in MiB."""
    if resource is not None:
        try:
            ru_self = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            ru_child = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            # Linux reports ru_maxrss in kilobytes
            return float(max(ru_self, ru_child)) / 1024.0
        except Exception:
            pass
    return 0.0


def _child(path: str, chain_name: str, args_dict: Dict[str, Any], conn_path: str, work_dir: str) -> None:
    """Runs inside a fresh process spawned by BaseMeshOperator.execute().
    The memory cap is enforced from outside (its cgroup, or the parent's RSS watchdog);
    runs the operator, measures quality, and writes structured JSON."""
    args = argparse.Namespace(**args_dict)

    _die_with_parent()

    res: Dict[str, Any] = {}
    t0 = time.perf_counter()
    try:
        m = normalize(load_mesh(path))
        op = build_operators(args)[chain_name]
        t_op = time.perf_counter()
        out = op.run_in_worker(m, op.timeout_s, op.max_mem_mb, work_dir)
        res["time_s"] = time.perf_counter() - t_op
        res["metrics"] = quality(m, out)
        # Optional Draco step on the repaired output. Every bit depth is one extra CSV row
        # "<chain>+draco<bits>" (see process_mesh); the repair itself runs only once.
        draco_rows = []
        for bits in _parse_draco_bits(args):
            t_d = time.perf_counter()
            try:
                with _PeakMem() as pm:
                    dm = op_draco(out, bits, getattr(args, "draco_level", 7))
                q = quality(m, dm)
                md = dm.metadata
                q.update(draco_bits=bits, draco_bytes=md["draco_bytes"], raw_bytes=md["raw_bytes"],
                         draco_ratio=md["raw_bytes"] / max(md["draco_bytes"], 1),
                         draco_encode_s=md["draco_encode_s"], draco_decode_s=md["draco_decode_s"],
                         draco_mem_mb=pm.mb)
                draco_rows.append({"status": "ok", "metrics": q,
                                   "draco_time_s": time.perf_counter() - t_d})
            except Exception as e:
                draco_rows.append({"status": "error", "metrics": {"draco_bits": bits},
                                   "error": f"{type(e).__name__}: {str(e)[:200]}",
                                   "draco_time_s": time.perf_counter() - t_d})
        if draco_rows:
            res["metrics"]["_draco"] = draco_rows
        res["status"] = ExecutionStatus.SUCCESS.value
    except MemoryError as e:
        res["time_s"] = time.perf_counter() - t0
        res["status"] = ExecutionStatus.OOM.value
        res["error"] = f"MemoryError: {str(e)[:200]}"
    except subprocess.TimeoutExpired as e:
        res["time_s"] = time.perf_counter() - t0
        res["status"] = ExecutionStatus.TIMEOUT.value
        res["error"] = f"TimeoutExpired: {str(e)[:200]}"
    except Exception as e:
        res["time_s"] = time.perf_counter() - t0
        res["status"] = ExecutionStatus.ERROR.value
        res["error"] = f"{type(e).__name__}: {str(e)[:200]}"

    res["peak_mem_mb"] = _get_peak_rss_mb()
    with open(conn_path, "w", encoding="utf-8") as fh:
        json.dump(res, fh)


def run_isolated(path: str, chain_name: str, args: argparse.Namespace) -> Dict[str, Any]:
    """Backwards-compatible wrapper executing a single operator via BaseMeshOperator."""
    op = build_operators(args)[chain_name]
    result = op.execute(path, args)
    return result.to_csv_dict()


def process_mesh(path: str, args: argparse.Namespace) -> List[Dict[str, Any]]:
    """Fault-tolerant pipeline for a single mesh:
    1. Extracts cheap features.
    2. Iterates sequentially over all configured BaseMeshOperators.
    3. Isolates each operator in a subprocess via subprocess.run; on TIMEOUT, OOM, or ERROR,
       logs the structured failure, cleans up temporary files, and continues to the next tool."""
    mesh_name = os.path.basename(path)
    try:
        feats = cheap_features(load_mesh(path))
    except Exception as e:
        return [{
            "mesh": mesh_name,
            "chain": "_load",
            "status": "unreadable",
            "error": str(e)[:200],
        }]

    rows: List[Dict[str, Any]] = []
    operators = build_operators(args)

    for chain_name, operator in operators.items():
        op_result: OperatorResult = operator.execute(path, args)

        draco_rows = op_result.metrics.pop("_draco", [])
        row: Dict[str, Any] = {"mesh": mesh_name, "chain": chain_name}
        row.update(feats)
        row.update(op_result.to_csv_dict())
        rows.append(row)
        # Draco variants of a successful run: time = repair time + encode/decode/measure
        for d in draco_rows:
            drow = dict(row)
            for k in ("out_faces", "watertight", "manifold", "hausdorff_rel",
                      "chamfer_rel", "sharp_recall"):
                drow.pop(k, None)
            drow.update(d["metrics"])
            drow["chain"] = f"{chain_name}+draco{d['metrics']['draco_bits']}"
            drow["status"] = d["status"]
            drow["error"] = d.get("error", "")
            drow["time_s"] = row["time_s"] + d["draco_time_s"]
            rows.append(drow)

        # On OOM, TIMEOUT, or ERROR, operator.execute() has already reaped the child
        # process and wiped its temporary directory; proceed cleanly to the next tool.
        if op_result.status in (ExecutionStatus.OOM, ExecutionStatus.TIMEOUT, ExecutionStatus.ERROR):
            continue

    return rows

# ---------------------------------------------------------------------- main

COLUMNS = ["mesh", "chain", "status", "error", "time_s", "wall_s", "peak_mem_mb",
           "out_faces", "watertight", "manifold", "hausdorff_rel", "chamfer_rel", "sharp_recall",
           "n_faces", "n_verts", "aspect_ratio", "boundary_edge_ratio", "nonmanifold_edge_ratio",
           "degenerate_face_ratio", "n_components", "self_intersect_est", "sharp_edge_ratio",
           "feature_time_s", "draco_bits", "draco_bytes", "raw_bytes", "draco_ratio",
           "draco_encode_s", "draco_decode_s", "draco_mem_mb"]
DRACO_COLUMNS = ["draco_bits", "draco_bytes", "raw_bytes", "draco_ratio",
                 "draco_encode_s", "draco_decode_s", "draco_mem_mb"]


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--_child":
        _, _, path, chain, out_json, work_dir, args_json = sys.argv
        _child(path, chain, json.loads(args_json), out_json, work_dir)
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
    ap.add_argument("--total-mem-gb", type=float, default=0.8 * (psutil.virtual_memory().total / 1024 ** 3 if psutil else 0),
                    help="cap on the summed RAM of all concurrent runs (default: 80%% of RAM)")
    ap.add_argument("--chains", default=None,
                    help="comma-separated subset of chains to run (default: all)")
    ap.add_argument("--draco-bits", default=None,
                    help="e.g. 10,12,14: after each successful chain, Draco-compress its output at\n"
                         "these quantization bits and add a row '<chain>+draco<bits>' (needs DracoPy)")
    ap.add_argument("--draco-level", type=int, default=7, help="Draco compression level 0-10")
    args = ap.parse_args()
    if args.draco_bits:
        try:
            bits = _parse_draco_bits(args)
        except ValueError:
            ap.error("--draco-bits must be comma-separated integers, e.g. 10,12,14")
        if any(not 1 <= b <= 30 for b in bits):
            ap.error("--draco-bits values must be between 1 and 30")
        try:
            _draco()   # fail early if DracoPy is missing
        except RuntimeError as e:
            ap.error(str(e))
    # external binaries run with cwd = their temp dir, so relative paths must be resolved here
    for opt in ("manifoldplus", "ftetwild"):
        exe = getattr(args, opt)
        if exe:
            exe = shutil.which(exe) or exe
            if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
                ap.error(f"--{opt}: {exe} is not an executable file")
            setattr(args, opt, os.path.abspath(exe))

    files = sorted(os.path.join(r, f) for r, _, fs in os.walk(args.input)
                   for f in fs if f.lower().endswith(MESH_EXT))
    random.Random(args.seed).shuffle(files)
    if args.limit:
        files = files[:args.limit]

    _cap_all_runs(args.total_mem_gb)

    done = set()
    if os.path.exists(args.out):
        with open(args.out) as fh:
            done = {row["mesh"] for row in csv.DictReader(fh)}
    todo = [f for f in files if os.path.basename(f) not in done]
    print(f"{len(files)} meshes selected, {len(done)} already done, {len(todo)} to run, "
          f"chains: {list(build_operators(args))}")

    new_file = not os.path.exists(args.out)
    fieldnames = COLUMNS
    if not new_file:
        # Append in the file's own column order. A file from before the Draco columns can be
        # resumed as long as no Draco rows are written; anything else would misalign rows.
        with open(args.out, newline="") as fh:
            header = next(csv.reader(fh), [])
        needed = COLUMNS if args.draco_bits else [c for c in COLUMNS if c not in DRACO_COLUMNS]
        if header and (set(header) - set(COLUMNS) or set(needed) - set(header)):
            ap.error(f"{args.out} has a different column layout (older version of this script, "
                     f"or no Draco columns while --draco-bits is set). Use a new --out file.")
        fieldnames = header or COLUMNS
    with open(args.out, "a", newline="") as fh, ProcessPoolExecutor(args.workers) as pool:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
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
