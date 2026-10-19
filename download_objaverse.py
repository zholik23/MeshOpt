#!/usr/bin/env python3
"""
Download a random sample of Objaverse (v1) models for the MeshOpt pilot.

Downloads directly from Hugging Face (allenai/objaverse); no `objaverse` package needed.
Files are saved as <out>/<uid>.glb. Already-downloaded files are skipped, so you can
stop and rerun safely.

Usage (Windows PowerShell or Linux):
  python download_objaverse.py --out objaverse_sample --n 2000

Then run the pilot on that folder:
  python run_pilot.py --input objaverse_sample --out objaverse_results.csv --limit 2000 --workers 4 --mem-gb 4
"""
import argparse, gzip, json, os, random, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://huggingface.co/datasets/allenai/objaverse/resolve/main/"
INDEX = "object-paths.json.gz"          # uid -> "glbs/000-023/<uid>.glb"


def fetch(url, dst, max_bytes=None, retries=3):
    """Stream url to dst. Returns 'ok', 'too_big' or raises after retries."""
    tmp = dst + ".part"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "meshopt-pilot"})
            with urllib.request.urlopen(req, timeout=60) as r:
                size = int(r.headers.get("Content-Length") or 0)
                if max_bytes and size > max_bytes:
                    return "too_big"
                got = 0
                with open(tmp, "wb") as fh:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        got += len(chunk)
                        if max_bytes and got > max_bytes:
                            fh.close(); os.remove(tmp)
                            return "too_big"
                        fh.write(chunk)
            os.replace(tmp, dst)
            return "ok"
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out", default="objaverse_sample", help="folder to save .glb files")
    ap.add_argument("--n", type=int, default=2000, help="number of models to download")
    ap.add_argument("--seed", type=int, default=0, help="random seed (same seed = same sample)")
    ap.add_argument("--max-mb", type=float, default=50,
                    help="skip files larger than this (very large scenes; 0 = no limit)")
    ap.add_argument("--threads", type=int, default=8)
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    idx_path = os.path.join(a.out, INDEX)
    if not os.path.exists(idx_path):
        print("Downloading the Objaverse index (~20 MB)...")
        fetch(BASE + INDEX, idx_path)
    with gzip.open(idx_path, "rt") as fh:
        paths = json.load(fh)
    uids = sorted(paths)
    print(f"Index has {len(uids):,} objects.")

    # Draw extra candidates so skipped (too big / failed) files can be replaced.
    rng = random.Random(a.seed)
    candidates = rng.sample(uids, min(len(uids), a.n * 2))
    max_bytes = int(a.max_mb * 1024 ** 2) if a.max_mb else None

    have = {f[:-4] for f in os.listdir(a.out) if f.endswith(".glb")}
    done = len(have)
    stats = {"ok": 0, "too_big": 0, "failed": 0}
    queue = [u for u in candidates if u not in have]
    print(f"{done} already downloaded, need {max(0, a.n - done)} more.")

    pos = 0
    with ThreadPoolExecutor(a.threads) as pool:
        while done < a.n and pos < len(queue):
            batch = queue[pos: pos + (a.n - done)]
            pos += len(batch)
            futs = {pool.submit(fetch, BASE + paths[u], os.path.join(a.out, u + ".glb"),
                                max_bytes): u for u in batch}
            for f in as_completed(futs):
                try:
                    res = f.result()
                except Exception as e:
                    res = "failed"
                    print(f"  failed {futs[f]}: {type(e).__name__}: {str(e)[:80]}", file=sys.stderr)
                stats[res] += 1
                if res == "ok":
                    done += 1
                    if done % 50 == 0:
                        print(f"  {done}/{a.n} downloaded", flush=True)

    print(f"Finished: {done} models in {a.out} "
          f"(this run: {stats['ok']} ok, {stats['too_big']} skipped as too big, {stats['failed']} failed).")
    print("Next: python run_pilot.py --input", a.out,
          "--out objaverse_results.csv --limit", a.n, "--workers 4 --mem-gb 4")


if __name__ == "__main__":
    main()
