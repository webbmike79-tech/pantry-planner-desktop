"""
religious_diet.py - Religious Diet Presets ("Faith & tradition") for Pantry Planner.

Defines:
- 6 Religious Diet Presets & Rule Bundles
- Verification / Enforcement logic for meal recipes, swap requests, and generation contexts
- Formatting helpers for AI prompt injection
"""

import re
from typing import Dict, List, Optional, Tuple, Set

PRESETS_INFO = {
    "Halal": {
        "title": "Halal",
        "description": "Excludes pork and pork derivatives, and alcohol in any form.",
        "exclusions_summary": "No pork, bacon, ham, pepperoni, sausage with pork, lard, pork gelatin, or alcohol in any form (including cooking wine/beer in sauces).",
    },
    "Kosher": {
        "title": "Kosher",
        "description": "Excludes pork, shellfish, and mixing meat/poultry with dairy in the same meal.",
        "exclusions_summary": "No pork/pork derivatives, no shellfish (shrimp, crab, lobster, scallops, clams, mussels, oysters), and meat/poultry and dairy (milk, cheese, butter, cream, yogurt) never appear in the SAME meal.",
    },
    "Hindu": {
        "title": "Hindu",
        "description": "Excludes beef and veal in all forms.",
        "exclusions_summary": "No beef or veal in all forms (including beef broth/stock and gelatin from beef where identifiable).",
    },
    "Jain": {
        "title": "Jain",
        "description": "Excludes meat, poultry, fish, eggs, root vegetables, onion, and garlic.",
        "exclusions_summary": "No meat, poultry, fish, seafood, eggs, onion, garlic, or root vegetables (potato, carrot, radish, turnip, beet, sweet potato).",
    },
    "Latter-day Saint": {
        "title": "Latter-day Saint",
        "description": "Excludes alcohol in any form, coffee, and tea from the tea plant.",
        "exclusions_summary": "No alcohol in any form, no coffee (including coffee-flavored items), and no tea from the tea plant (black, green, white, oolong; herbal infusions are allowed).",
    },
    "Lent (Fridays)": {
        "title": "Lent (Fridays)",
        "description": "Excludes land meat on Fridays during plan generation. Seafood is allowed.",
        "exclusions_summary": "On Fridays: no land meat (beef, pork, poultry, lamb). Fish and seafood are allowed. (Applies to Friday meals; toggle off outside the season).",
    },
}

HONESTY_NOTE = "These presets follow ingredient rules. This app can't verify halal or kosher certification — please check product labels for certification."

# --- Keywords for deterministic violation checking ---

PORK_KEYWORDS = {
    "pork", "bacon", "ham", "prosciutto", "pancetta", "pepperoni", "lard", "carnitas",
    "pork sausage", "pork chop", "pork chops", "pork loin", "pork belly", "pork ribs", "pork gelatin"
}

ALCOHOL_KEYWORDS = {
    "wine", "red wine", "white wine", "cooking wine", "beer", "ale", "lager", "rum", "vodka",
    "whiskey", "whisky", "bourbon", "brandy", "cognac", "tequila", "liqueur", "sake", "mirin",
    "marsala", "sherry", "champagne", "cider", "alcohol"
}

SHELLFISH_KEYWORDS = {
    "shrimp", "prawn", "prawns", "crab", "lobster", "scallop", "scallops", "clam", "clams",
    "mussel", "mussels", "oyster", "oysters", "calamari", "squid", "octopus", "crawfish", "crayfish"
}

BEEF_KEYWORDS = {
    "beef", "veal", "steak", "ground beef", "roast beef", "beef broth", "beef stock",
    "beef bouillon", "beef tallow", "beef gelatin"
}

POULTRY_KEYWORDS = {
    "chicken", "turkey", "duck", "goose", "quail", "poultry"
}

LAND_MEAT_KEYWORDS = BEEF_KEYWORDS | PORK_KEYWORDS | POULTRY_KEYWORDS | {
    "lamb", "mutton", "goat", "venison", "bison"
}

FISH_KEYWORDS = {
    "fish", "salmon", "tuna", "cod", "tilapia", "halibut", "trout", "bass", "snapper", "flounder", "sardine", "sardines", "anchovy", "anchovies"
}

DAIRY_KEYWORDS = {
    "milk", "cheese", "butter", "cream", "sour cream", "heavy cream", "yogurt", "ghee", "parmesan", "cheddar", "mozzarella", "ricotta"
}

EGG_KEYWORDS = {
    "egg", "eggs", "egg white", "egg yolk", "mayonnaise", "mayo"
}

ROOT_VEG_KEYWORDS = {
    "potato", "potatoes", "carrot", "carrots", "radish", "radishes", "turnip", "turnips",
    "beet", "beets", "beetroot", "sweet potato", "sweet potatoes", "yam", "yams",
    "onion", "onions", "garlic", "shallot", "shallots", "scallion", "scallions", "leek", "leeks", "chive", "chives"
}

COFFEE_TEA_KEYWORDS = {
    "coffee", "espresso", "cappuccino", "latte", "black tea", "green tea", "white tea", "oolong",
    "oolong tea", "matcha", "earl grey"
}


def _tokenize_text(text: str) -> str:
    """Normalize text into lower-case with words separated by single spaces."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _contains_any_phrase(text: str, phrases: Set[str]) -> Tuple[bool, Optional[str]]:
    norm = f" {_tokenize_text(text)} "
    for p in phrases:
        p_norm = f" {_tokenize_text(p)} "
        if p_norm in norm:
            return True, p
    return False, None


def check_meal_violation(
    dish_name: str,
    ingredients: List[str],
    active_presets: List[str],
    day_name: Optional[str] = None,
) -> Tuple[bool, str]:
    """Deterministically checks if a meal violates any active religious diet preset.
    Returns (is_violating, reason).
    """
    if not active_presets:
        return False, ""

    combined_text = f"{dish_name} " + " ".join(ingredients)
    day_lower = (day_name or "").strip().lower()

    # 1. Halal: Excludes pork and pork derivatives, and alcohol in any form.
    if "Halal" in active_presets:
        has_pork, kw = _contains_any_phrase(combined_text, PORK_KEYWORDS)
        if has_pork:
            return True, f"Halal violation: dish contains pork ({kw})."
        has_alc, kw = _contains_any_phrase(combined_text, ALCOHOL_KEYWORDS)
        if has_alc:
            return True, f"Halal violation: dish contains alcohol ({kw})."

    # 2. Kosher: Excludes pork, shellfish; AND meat/poultry + dairy never in same meal.
    if "Kosher" in active_presets:
        has_pork, kw = _contains_any_phrase(combined_text, PORK_KEYWORDS)
        if has_pork:
            return True, f"Kosher violation: dish contains pork ({kw})."
        has_shellfish, kw = _contains_any_phrase(combined_text, SHELLFISH_KEYWORDS)
        if has_shellfish:
            return True, f"Kosher violation: dish contains shellfish ({kw})."

        # Check meat/poultry and dairy mixing in the same meal
        has_meat, m_kw = _contains_any_phrase(combined_text, LAND_MEAT_KEYWORDS)
        has_dairy, d_kw = _contains_any_phrase(combined_text, DAIRY_KEYWORDS)
        if has_meat and has_dairy:
            return True, f"Kosher violation: meat/poultry ({m_kw}) and dairy ({d_kw}) cannot appear in the same meal."

    # 3. Hindu: Excludes beef and veal in all forms.
    if "Hindu" in active_presets:
        has_beef, kw = _contains_any_phrase(combined_text, BEEF_KEYWORDS)
        if has_beef:
            return True, f"Hindu violation: dish contains beef/veal ({kw})."

    # 4. Jain: Excludes all meat, poultry, fish, seafood, eggs; onion, garlic; root vegetables.
    if "Jain" in active_presets:
        has_meat, kw = _contains_any_phrase(combined_text, LAND_MEAT_KEYWORDS | FISH_KEYWORDS | SHELLFISH_KEYWORDS)
        if has_meat:
            return True, f"Jain violation: dish contains meat/fish/seafood ({kw})."
        has_egg, kw = _contains_any_phrase(combined_text, EGG_KEYWORDS)
        if has_egg:
            return True, f"Jain violation: dish contains egg ({kw})."
        has_root, kw = _contains_any_phrase(combined_text, ROOT_VEG_KEYWORDS)
        if has_root:
            return True, f"Jain violation: dish contains root vegetables/alliums ({kw})."

    # 5. Latter-day Saint: Excludes alcohol, coffee, tea from tea plant.
    if "Latter-day Saint" in active_presets:
        has_alc, kw = _contains_any_phrase(combined_text, ALCOHOL_KEYWORDS)
        if has_alc:
            return True, f"Latter-day Saint violation: dish contains alcohol ({kw})."
        has_coffee_tea, kw = _contains_any_phrase(combined_text, COFFEE_TEA_KEYWORDS)
        if has_coffee_tea:
            return True, f"Latter-day Saint violation: dish contains coffee/tea ({kw})."

    # 6. Lent (Fridays): on Friday, excludes land meat (seafood fine).
    if "Lent (Fridays)" in active_presets:
        if day_lower == "friday":
            has_meat, kw = _contains_any_phrase(combined_text, LAND_MEAT_KEYWORDS)
            if has_meat:
                return True, f"Lent (Fridays) violation: land meat ({kw}) is not permitted on Friday."

    return False, ""


def build_religious_diet_prompt_section(active_presets: List[str]) -> str:
    """Builds the AI prompt section explaining active religious diet rules in plain language."""
    if not active_presets:
        return ""

    lines = [
        "FAITH & TRADITION PRESETS (STRICT INGREDIENT ENFORCEMENT):",
        f"Honesty Notice: {HONESTY_NOTE}",
    ]
    for preset in active_presets:
        if preset in PRESETS_INFO:
            lines.append(f"- {preset}: {PRESETS_INFO[preset]['exclusions_summary']}")

    lines.append(
        "ENFORCEMENT RULES FOR PRESETS:\n"
        "1. Obey all preset ingredient exclusions strictly. Never generate or use any prohibited ingredient.\n"
        "2. Kosher rule: Meat or poultry and dairy (milk, cheese, butter, yogurt) must NEVER appear in the same meal. Dairy-only meals or meat-only meals are allowed.\n"
        "3. Lent (Fridays) rule: On Friday meals, do NOT include land meat (beef, pork, poultry, lamb). Fish and seafood are permitted on Friday. Other days may include meat normally."
    )
    return "\n".join(lines)
