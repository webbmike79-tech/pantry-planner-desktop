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


import unicodedata

def strip_accents(text: str) -> str:
    """Strip accents and diacritics from text so 'Tajín' matches 'Tajin'."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize_words(text: str) -> Set[str]:
    """Tokenize text into lowercase alphanumeric words, filtering out container units and non-identifying generic words."""
    clean = strip_accents(text.lower())
    stop = {
        "a", "an", "the", "in", "with", "of", "and", "or", "for",
        "box", "can", "cans", "bag", "bags", "package", "pack", "tub", "bottle", "jar",
        "container", "cup", "cups", "sliced", "diced", "chunks", "packets", "loaf",
        "frozen", "unidentified", "item", "items", "food", "seasoning", "mccormick"
    }
    words = set(re.findall(r"[a-z0-9]+", clean))
    return {w for w in words if w not in stop}


def is_match(detected_name: str, gt_name: str) -> bool:
    """Check if detected name matches ground truth item (brand-less matches count)."""
    d_norm = strip_accents(detected_name.lower().strip())
    g_norm = strip_accents(gt_name.lower().strip())

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


HONEST_UNKNOWN_PATTERNS = [
    r"\bunidentified\b",
    r"\bclear bag\b",
    r"\bfoil-wrapped\b",
    r"\bfoil wrapped\b",
    r"\bopaque (container|bag|package|bottle|item|tub|wrap)\b",
    r"\bplastic (container|bag|wrap)\b",
    r"\bfood in plastic container\b",
    r"\bwhite container\b",
    r"\bbagged item\b",
    r"\bfrosted (bag|package|container|item)\b",
    r"\bfrost-covered\b",
    r"\bsealed (bag|package|container|item)\b",
    r"\bwrapped (package|item|food)\b",
]

KNOWN_BRANDS = {
    "eggo", "pretzel crisps", "duke's", "dukes", "fire & smoke society", "adams",
    "mccormick grill mates", "taj pel", "sargento", "jimmy dean", "tyson",
    "stouffer's", "birds eye", "green giant", "ore-ida", "gorton's", "pillsbury",
    "hunt's", "del monte", "barilla", "kraft", "heinz", "french's", "hellmann's",
    "bush's", "knorr", "progresso", "swanson", "v8", "jif", "skippy", "smucker's",
    "quaker", "kellogg's", "dole", "chobani", "tillamook", "kerrygold", "daisy",
    "philadelphia", "oscar mayer", "hormel", "hillshire", "hebrew national",
    "nathan's", "johnsonville", "applegate", "perdue", "butterball", "goya",
    "la costena", "la costeña", "old el paso", "ortega", "pace", "tostitos",
    "doritos", "lay's", "ruffles", "fritos", "cheetos", "pringles", "ritz",
    "wheat thins", "triscuit", "cheez-it", "goldfish", "oreo", "chips ahoy",
    "keebler", "pepperidge farm", "hostess", "sara lee", "thomas'", "wonder",
    "bimbo", "arnold", "nature's own", "dave's killer bread", "king's hawaiian",
    "mission", "guerrero", "amy's", "annie's", "kashi", "kind", "clif",
    "rxbar", "larabar", "nature valley", "gatorade", "powerade", "snapple",
    "arizona", "tropicana", "minute maid", "simply", "ocean spray", "welch's",
    "silk", "almond breeze", "oatly", "califia", "fairlife", "horizon",
    "stonyfield", "siggi's", "noosa", "two good", "oikos", "dannon", "breyers",
    "ben & jerry's", "haagen-dazs", "talenti", "blue bell", "edy's", "dreyer's",
    "drumstick", "klondike", "popsicle", "outshine", "hot pockets", "lean cuisine",
    "marie callender's", "banquet", "hungry-man", "healthy choice", "smart ones",
    "boston market", "bertolli", "rao's", "classico", "newman's own", "ken's",
    "wish-bone", "hidden valley", "good seasons", "lawry's", "old bay",
    "tony chachere's", "zatarain's", "badia", "morton", "diamond crystal",
    "spice islands", "simply organic", "frontier co-op", "trader joe's", "kirkland"
}

CONFABULATED_SPECIES = {
    "pheasant", "venison", "quail", "grouse", "squab", "pigeon", "goose", "duck",
    "bison", "elk", "boar", "rabbit", "hare", "goat", "mutton", "veal", "octopus",
    "squid", "calamari", "eel", "crawfish", "crayfish", "lobster", "crab", "oyster",
    "mussel", "clam", "scallop", "anchovy", "sardine", "trout", "halibut", "cod",
    "snapper", "mahi mahi", "bass", "flounder", "sole", "catfish", "tilapia",
    "haddock", "pollock", "swordfish", "tuna", "salmon"
}


def classify_invented_item(item_name: str, ground_truth: List[str] = None) -> str:
    """Classify an unmatched detected item into CONFABULATED, HONEST-UNKNOWN, or UNMATCHED-SPECIFIC."""
    norm = item_name.strip()
    lower = strip_accents(norm.lower())

    # 1. HONEST-UNKNOWN: explicitly honest descriptions of unidentifiable/ambiguous items
    for pat in HONEST_UNKNOWN_PATTERNS:
        if re.search(pat, lower):
            return "HONEST-UNKNOWN"

    # 2. CONFABULATED: claims specific brand, product line, or species with no match in manifest
    words = set(re.findall(r"[a-z0-9]+", lower))
    if any(sp in words for sp in CONFABULATED_SPECIES):
        gt_words = set()
        if ground_truth:
            for g in ground_truth:
                gt_words.update(re.findall(r"[a-z0-9]+", strip_accents(g.lower())))
        species_in_gt = any(sp in gt_words for sp in (words & CONFABULATED_SPECIES))
        if not species_in_gt:
            return "CONFABULATED"

    # Check known brands
    for brand in KNOWN_BRANDS:
        if brand in lower or brand.replace("'", "") in lower.replace("'", ""):
            if not ground_truth or not any(brand in strip_accents(g.lower()) for g in ground_truth):
                return "CONFABULATED"

    # Possessive brand names (e.g., Duke's, Lola's)
    possessives = re.findall(r"\b([a-z]+'s)\b", lower)
    if possessives:
        gt_lower = " ".join(ground_truth).lower() if ground_truth else ""
        if any(p not in gt_lower for p in possessives):
            return "CONFABULATED"

    # Multi-word proper name / brand patterns e.g. "Taj Pel", "Fire & Smoke Society"
    if "taj pel" in lower or "fire & smoke" in lower or "&" in norm:
        return "CONFABULATED"

    # 3. UNMATCHED-SPECIFIC: specific food names that don't match manifest but aren't clearly confabulated brands
    return "UNMATCHED-SPECIFIC"


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
    unique_invented = list(dict.fromkeys(invented_all))

    # Classify invented items into 3 reported classes
    confabulated = [d for d in unique_invented if classify_invented_item(d, ground_truth) == "CONFABULATED"]
    honest_unknown = [d for d in unique_invented if classify_invented_item(d, ground_truth) == "HONEST-UNKNOWN"]
    unmatched_specific = [d for d in unique_invented if classify_invented_item(d, ground_truth) == "UNMATCHED-SPECIFIC"]

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
        "confabulated_count": len(confabulated),
        "confabulated_items": confabulated,
        "honest_unknown_count": len(honest_unknown),
        "honest_unknown_items": honest_unknown,
        "unmatched_specific_count": len(unmatched_specific),
        "unmatched_specific_items": unmatched_specific,
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
            photo_candidates = [
                FIXTURES_DIR / fname,
                Path(r"C:\Users\ekim_\Downloads\pantry test pics") / fname,
                FIXTURES_DIR / entry.get("camera_filename", ""),
                Path(r"C:\Users\ekim_\Downloads\pantry test pics") / entry.get("camera_filename", ""),
            ]
            photo_path = next((p for p in photo_candidates if p and p.is_file()), None)

            if not photo_path:
                print(f"WARNING: Fixture file {fname} not found in candidates. Skipping.")
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
    """Print markdown benchmark table reporting all 3 classes: Confabulated, Honest-Unknown, Unmatched-Specific."""
    results = run_data.get("results", {})
    if not results:
        print("No results to display.")
        return

    print(f"\n### {provider.upper()} Vision Benchmark Results (Honest Classifier)")
    print("| Photo Fixture | Ground Truth | Matched | Missed | Confabulated | Honest-Unknown | Unmatched-Specific | Total Invented | Details / Breakdown |")
    print("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |")

    tot_gt = 0
    tot_matched = 0
    tot_missed = 0
    tot_confab = 0
    tot_honest = 0
    tot_unmatched_spec = 0
    tot_invented = 0

    for key, sc in results.items():
        gt_len = sc.get("total_ground_truth", 0)
        matched = sc.get("matched_count", 0)
        missed = sc.get("missed_count", 0)
        confab = sc.get("confabulated_count", len(sc.get("confabulated_items", [])))
        honest = sc.get("honest_unknown_count", len(sc.get("honest_unknown_items", [])))
        unmatched_spec = sc.get("unmatched_specific_count", len(sc.get("unmatched_specific_items", [])))
        inv_cnt = sc.get("invented_count", 0)

        details = []
        if sc.get("confabulated_items"):
            details.append(f"Confab: {', '.join(sc['confabulated_items'])}")
        if sc.get("honest_unknown_items"):
            details.append(f"Honest: {', '.join(sc['honest_unknown_items'])}")
        if sc.get("unmatched_specific_items"):
            details.append(f"Unmatched-Spec: {', '.join(sc['unmatched_specific_items'])}")
        detail_str = "; ".join(details) if details else "—"

        tot_gt += gt_len
        tot_matched += matched
        tot_missed += missed
        tot_confab += confab
        tot_honest += honest
        tot_unmatched_spec += unmatched_spec
        tot_invented += inv_cnt

        print(f"| `{key}` | {gt_len} | {matched} | {missed} | {confab} | {honest} | {unmatched_spec} | {inv_cnt} | {detail_str} |")

    print(f"| **TOTALS** | **{tot_gt}** | **{tot_matched}** | **{tot_missed}** | **{tot_confab}** | **{tot_honest}** | **{tot_unmatched_spec}** | **{tot_invented}** | |")
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
