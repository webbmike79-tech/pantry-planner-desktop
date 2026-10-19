"""
Vision benchmark runner harness.
Evaluates storage scan vision models against ground truth photo fixtures.
Runs a throwaway hub instance on port 8002 with isolated temp DB and single-provider config.
Never touches or alters live hub on port 8001 or live database.
"""

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import requests

ROOT_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT_DIR / "tests" / "fixtures" / "vision"
MANIFEST_FILE = FIXTURES_DIR / "ground_truth.json"


def normalize_words(text: str) -> Set[str]:
    """Tokenize text into lowercase alphanumeric words, filtering out container units and non-identifying generic words."""

    stop = {
        "a", "an", "the", "in", "with", "of", "and", "or", "for",
        "box", "can", "cans", "bag", "bags", "package", "pack", "tub", "bottle", "jar",
        "container", "cup", "cups", "sliced", "diced", "chunks", "packets", "loaf",
        "frozen", "unidentified", "item", "items", "food", "seasoning", "mccormick"
    }
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    return {w for w in words if w not in stop}


def is_match(detected_name: str, gt_name: str) -> bool:
    """Check if detected name matches ground truth item (brand-less matches count)."""
    d_norm = detected_name.lower().strip()
    g_norm = gt_name.lower().strip()

    if d_norm == g_norm:
        return True
    if d_norm in g_norm or g_norm in d_norm:
        return True

    # Word set overlap
    d_words = normalize_words(d_norm)
    g_words = normalize_words(g_norm)
    if not d_words or not g_words:
        return False

    overlap = d_words & g_words
    if overlap:
        return True

    # Specific synonyms
    synonyms = [
        ({"ketchup", "catsup"}, True),
        ({"salsa", "hot sauce", "chili sauce"}, False), # distinct
        ({"peaches", "peach"}, True),
        ({"vegetables", "veggies", "peas"}, True),
        ({"shake", "shakes", "protein"}, True),
        ({"chips", "tortilla chips", "doritos", "tostitos"}, True),
    ]
    for syn_set, exact in synonyms:
        if any(w in syn_set for w in d_words) and any(w in syn_set for w in g_words):
            return True

    return False


def score_photo_results(
    photo_key: str,
    detected_items: List[str],
    ground_truth: List[str],
) -> Dict[str, Any]:
    """Score detected item list against ground truth for a photo."""
    matched_gt: Set[str] = set()
    matched_detected: Set[str] = set()
    matches: List[Tuple[str, str]] = []

    for d in detected_items:
        # Check against ground truth
        matched_any = False
        for gt in ground_truth:
            if is_match(d, gt):
                matched_gt.add(gt)
                matched_detected.add(d)
                matches.append((d, gt))
                matched_any = True
                break

    # Invented: detected items that had no match with any ground truth item (deduplicated)
    invented_all = [d for d in detected_items if d not in matched_detected]
    # Unique invented items preserve first seen order
    unique_invented = list(dict.fromkeys(invented_all))

    # Missed: ground truth items not matched
    missed = [gt for gt in ground_truth if gt not in matched_gt]

    return {
        "photo_key": photo_key,
        "total_detected": len(detected_items),
        "total_ground_truth": len(ground_truth),
        "matched_count": len(matched_gt),
        "missed_count": len(missed),
        "invented_count": len(unique_invented),
        "invented_items": unique_invented,
        "missed_items": missed,
        "matches": matches,
    }



def get_live_config() -> Dict[str, Any]:
    """Read provider keys from pantry_desktop_config.json."""
    candidates = [
        ROOT_DIR / "pantry_desktop_config.json",
        ROOT_DIR.parent / "pantry_desktop_config.json",
    ]
    for p in candidates:
        if p.is_file():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    return {}


def probe_gemini_quota(key: str) -> Tuple[bool, str]:
    """Probe Gemini to check if quota is available."""
    try:
        from google import genai
        client = genai.Client(api_key=key)
        client.models.generate_content(model="gemini-3.8-flash", contents="ping")
        return True, "ok"
    except Exception as exc:
        err_msg = str(exc)
        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "quota" in err_msg.lower():
            return False, f"429 RESOURCE_EXHAUSTED ({err_msg[:120]})"
        return False, err_msg[:120]


def run_benchmark_for_provider(provider: str, port: int = 8002) -> Dict[str, Any]:
    """Run benchmark against a single provider on throwaway port 8002."""
    if not MANIFEST_FILE.is_file():
        raise FileNotFoundError(f"Manifest file not found: {MANIFEST_FILE}")

    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    cfg = get_live_config()
    provider_keys = cfg.get("provider_keys", {})
    provider_models = cfg.get("provider_models", {})

    api_key = provider_keys.get(provider, "")
    if not api_key:
        raise ValueError(f"No API key configured for provider '{provider}' in pantry_desktop_config.json")

    # If gemini, check probe first
    if provider == "gemini":
        ok, reason = probe_gemini_quota(api_key)
        if not ok:
            print(f"Gemini provider probe check: FAILED — {reason}")
            print("SKIPPING Gemini benchmark leg (quota exhausted; pending reset).")
            return {"status": "skipped", "provider": "gemini", "reason": reason}

    # Set up temp throwaway environment
    temp_dir = tempfile.mkdtemp(prefix="pantry_bench_")
    temp_db = os.path.join(temp_dir, "throwaway.db")
    temp_config = os.path.join(temp_dir, "throwaway_config.json")

    throwaway_config = {
        "provider_keys": {provider: api_key},
        "provider_models": {provider: provider_models.get(provider, "")},
    }
    with open(temp_config, "w", encoding="utf-8") as f:
        json.dump(throwaway_config, f)

    env = os.environ.copy()
    env["PANTRY_DB_FILE"] = temp_db
    env["PANTRY_CONFIG_FILE"] = temp_config
    env["PANTRY_PORT"] = str(port)
    env["PANTRY_TEST_MODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    proc = None
    results = {}
    try:
        print(f"Starting throwaway hub for '{provider}' on port {port}...")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(ROOT_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Wait for hub ready
        base_url = f"http://127.0.0.1:{port}"
        ready = False
        for _ in range(40):
            try:
                r = requests.get(f"{base_url}/health", timeout=1.0)
                if r.status_code == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.5)

        if not ready:
            raise RuntimeError(f"Throwaway hub failed to start on port {port}")
        print("Throwaway hub is ready.")

        # Process each fixture photo
        for key, entry in manifest.items():
            fname = entry["filename"]
            loc = entry["location"]
            gt = entry["ground_truth"]
            photo_path = FIXTURES_DIR / fname

            if not photo_path.is_file():
                print(f"WARNING: Fixture file {photo_path} does not exist. Skipping.")
                continue

            detected_names = []
            for attempt in range(1, 4):
                print(f"Scanning '{key}' ({fname}, location='{loc}') via {provider} (attempt {attempt}/3)...")
                with open(photo_path, "rb") as img_f:
                    files = {"file": (fname, img_f, "image/jpeg")}
                    data = {"location": loc}
                    resp = requests.post(f"{base_url}/api/scan-photo", files=files, data=data, timeout=120)

                if resp.status_code == 200:
                    body = resp.json()
                    items = body.get("items", [])
                    detected_names = [it.get("item_name", "") for it in items if it.get("item_name")]
                    print(f"  -> Detected {len(detected_names)} items (served_by={body.get('served_by')})")
                    break
                else:
                    print(f"  -> Attempt {attempt} returned {resp.status_code}: {resp.text[:120]}")
                    if attempt < 3:
                        wait_sec = 25 * attempt
                        print(f"  -> Waiting {wait_sec}s for rate limit reset...")
                        time.sleep(wait_sec)

            scored = score_photo_results(key, detected_names, gt)
            results[key] = scored

            # Rate-limit safety pause between vision calls
            time.sleep(15)


    finally:
        if proc:
            print("Shutting down throwaway hub...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(temp_dir, ignore_errors=True)

    return {"status": "completed", "provider": provider, "results": results}


def print_benchmark_table(provider: str, run_data: Dict[str, Any]) -> None:
    """Print markdown benchmark table and itemized list of invented items."""
    results = run_data.get("results", {})
    if not results:
        print("No results to display.")
        return

    print(f"\n### {provider.upper()} Vision Benchmark Results")
    print("| Photo Fixture | Ground Truth | Matched | Missed | Invented | Invented Items |")
    print("| :--- | :---: | :---: | :---: | :---: | :--- |")

    tot_gt = 0
    tot_matched = 0
    tot_missed = 0
    tot_invented = 0

    for key, sc in results.items():
        gt_len = sc["total_ground_truth"]
        matched = sc["matched_count"]
        missed = sc["missed_count"]
        inv_cnt = sc["invented_count"]
        inv_names = ", ".join(sc["invented_items"]) if sc["invented_items"] else "—"

        tot_gt += gt_len
        tot_matched += matched
        tot_missed += missed
        tot_invented += inv_cnt

        print(f"| `{key}` | {gt_len} | {matched} | {missed} | {inv_cnt} | {inv_names} |")

    print(f"| **TOTALS** | **{tot_gt}** | **{tot_matched}** | **{tot_missed}** | **{tot_invented}** | |")
    print()


def main():
    parser = argparse.ArgumentParser(description="Run vision benchmark harness")
    parser.add_argument("--provider", default="groq", choices=["groq", "gemini", "openrouter"], help="Provider to test")
    parser.add_argument("--port", type=int, default=8002, help="Port for throwaway server")
    args = parser.parse_args()

    run_data = run_benchmark_for_provider(args.provider, port=args.port)
    if run_data.get("status") == "completed":
        print_benchmark_table(args.provider, run_data)
        out_file = FIXTURES_DIR / f"benchmark_result_{args.provider}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(run_data, f, indent=2)
        print(f"Benchmark output saved to {out_file}")


if __name__ == "__main__":
    main()
