"""Pure text logic: no I/O, no model, no clock. Everything here is unit-tested.

    norm(t)                     fold Arabic letter variants + diacritics, lower-case, trim
    fidelity(raw, clean)        how far the cleaner moved away from what was said
    apply_snippets(t, cfg)      whole-utterance trigger -> expansion
    dict_terms(cfg)             the dictionary's names and wrong=>right pairs
    apply_dictionary(t, cfg)    explicit pairs, then conservative fuzzy repair of names
    prompt_for(lang, names)     Whisper's initial prompt for a language (None = auto)
    romanised_arabic_check(..)  did Whisper write Arabic speech in Latin letters?
    profile_for(app, profiles)  the per-app cleanup profile for a window title
    TONES, tone_hint(t)         register hints appended to the cleaner's system prompt
"""
from __future__ import annotations

import difflib
import re

# ---------------------------------------------------------------- normalisation
# Arabic diacritics (tashkeel) and the tatweel stretch: dropped before any comparison
_AR_DIAC = dict.fromkeys(map(ord, "ًٌٍَُِّْـ"), None)
_ARABIC_RANGE = ("؀", "ۿ")


def norm(t: str | None) -> str:
    """Fold the spellings Whisper varies between, so two ways of writing a word compare equal.

    alef forms (أ إ آ) -> ا, alef maqsura ى -> ي, taa marbuta ة -> ه, diacritics removed,
    lower-cased and trimmed. Safe on None.
    """
    t = (t or "").translate(_AR_DIAC)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    return t.lower().strip()


def is_arabic_char(ch: str) -> bool:
    return _ARABIC_RANGE[0] <= ch <= _ARABIC_RANGE[1]


def count_arabic(t: str) -> int:
    """How many characters of `t` are in the Arabic block."""
    return sum(1 for ch in (t or "") if is_arabic_char(ch))


# ---------------------------------------------------------------- fidelity: did the cleaner keep the meaning?
_WORD_RE = re.compile(r"[\w؀-ۿ']+")
FILLERS = {"um", "uh", "erm", "hmm", "mmm", "like", "yeah", "يعني", "امم", "اه", "اها"}


def content_tokens(t: str) -> list[str]:
    """Normalised words with hesitation fillers removed."""
    return [w for w in _WORD_RE.findall(norm(t)) if w not in FILLERS]


def fidelity(raw: str, clean: str) -> dict:
    """Word-level divergence between what was said and what the cleaner wrote.

    Fillers and punctuation are dropped first, so a good cleanup of a messy sentence
    scores green rather than amber — the meter must not punish the cleaner for doing
    its job, only for doing someone else's.

    d     = 1 - ratio over content words
    ins   = content words the cleaner added that were never said (an answer instead of a
            transcript shows here long before it shows as divergence)
    lvl   = same / green / amber / red   ("unknown" if scoring itself failed)
    """
    try:
        a, b = content_tokens(raw), content_tokens(clean)
        if not a or not b:
            return {"d": 0.0, "ins": 0, "lvl": "same"}
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        d = 1.0 - sm.ratio()
        seen = set(a)
        ins = 0
        for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
            if tag in ("insert", "replace"):
                ins += sum(1 for w in b[j1:j2] if w not in seen)
        if d < 0.02 and ins == 0:
            lvl = "same"
        elif d <= 0.20:
            lvl = "green"
        elif d <= 0.45:
            lvl = "amber"
        else:
            lvl = "red"
        return {"d": round(d, 3), "ins": ins, "lvl": lvl}
    except Exception:
        return {"d": None, "ins": None, "lvl": "unknown"}


# ---------------------------------------------------------------- snippets
_SNIPPET_TRIM = " .،؟?!"


def apply_snippets(text: str, cfg: dict) -> str:
    """Whole-utterance trigger -> expansion. Exact match on the normalised text only,
    ignoring surrounding spaces and end punctuation ("SIG." matches "sig")."""
    sn = cfg.get("snippets") or {}
    if not isinstance(sn, dict) or not sn or not text:
        return text
    key = norm(text).strip(_SNIPPET_TRIM)
    for trig, val in sn.items():
        if norm(str(trig)).strip(_SNIPPET_TRIM) == key and str(val).strip():
            return str(val)
    return text


# ---------------------------------------------------------------- dictionary
NAME_MATCH = 0.86       # similarity a word needs before it is "repaired" toward a known name
NAME_MIN_LEN = 4        # shorter words are never fuzzy-repaired: too many false friends
_TOKEN_PUNCT = ".,!?;:()[]\"'،؟"


def dict_terms(cfg: dict) -> tuple[list[str], list[tuple[str, str]]]:
    """(names, pairs) from cfg["dictionary"]. Pairs are written "wrong => right"."""
    d = cfg.get("dictionary") or {}
    names = [str(x).strip() for x in (d.get("names") or []) if str(x).strip()]
    pairs: list[tuple[str, str]] = []
    for raw in (d.get("pairs") or []):
        if "=>" in str(raw):
            a, b = str(raw).split("=>", 1)
            if a.strip() and b.strip():
                pairs.append((a.strip(), b.strip()))
    return names, pairs


def apply_dictionary(text: str, cfg: dict) -> str:
    """Explicit pairs first (every occurrence, case-insensitive), then conservative fuzzy
    repair of known names: single words at >= NAME_MATCH similarity and a similar length,
    then multi-word names as a sliding window."""
    if not text:
        return text
    names, pairs = dict_terms(cfg)
    out = text
    for wrong, right in pairs:
        # substring, not whole-word: Arabic attaches prefixes (ال، و، ب) to the word itself
        out = re.sub(re.escape(wrong), lambda _m, r=right: r, out, flags=re.IGNORECASE)
    if not names:
        return out
    tokens = out.split(" ")
    known = {norm(n) for n in names}
    for i, tok in enumerate(tokens):
        core = tok.strip(_TOKEN_PUNCT)
        if len(core) < NAME_MIN_LEN or norm(core) in known:
            continue
        best, score = None, 0.0
        for n in names:
            if abs(len(n) - len(core)) > 2:
                continue
            r = difflib.SequenceMatcher(None, norm(core), norm(n)).ratio()
            if r > score:
                best, score = n, r
        if best and score >= NAME_MATCH:
            tokens[i] = tok.replace(core, best, 1)
    # multi-word names (e.g. "Ada Lovelace") matched as sliding windows
    multi = [n for n in names if " " in n]
    if multi:
        words = " ".join(tokens).split(" ")
        for n in multi:
            k = len(n.split())
            for j in range(0, len(words) - k + 1):
                window = " ".join(words[j:j + k])
                if norm(window) == norm(n):
                    break
                if difflib.SequenceMatcher(None, norm(window), norm(n)).ratio() >= NAME_MATCH:
                    words[j:j + k] = n.split(" ")
                    break
        tokens = words
    return " ".join(tokens)


# ---------------------------------------------------------------- prompts and language
# a neutral default: ordinary spoken Arabic, in whatever dialect the speaker uses.
# override with `arabic_prompt` in config.json when your audio has a known shape.
AR_PROMPT = ("نص عربي منطوق، بالفصحى أو بلهجة المتحدث، بعلامات ترقيم عربية. "
             "أسماء قد تظهر: ")
EN_PROMPT = "A clear English dictation with punctuation. Names that may appear: "
MIX_PROMPT = ("Mixed Arabic and English dictation. Keep each part in its own script: "
              "Arabic in Arabic letters (عربي), English in Latin letters. "
              "Never romanise Arabic and never translate. Names that may appear: ")

# words that only appear when Arabic has been written in Latin letters
EN_STOP = set("the is are was were this that these those at of and for with you we they he she "
              "it to in on my your his her our their a an i do does did not have has had will "
              "would can could should just so but if then there here what when where who how".split())
ROMAN_AR = ("wallah", "walla", "billah", "yani", "ya3ni", "ya'ni", "habibi", "habibti",
            "inshallah", "insha'allah", "alhamdulillah", "hamdullah", "yalla", "khallas",
            "khalas", "shukran", "mashallah", "ma'shallah", "akhi", "ukhti", "na3am",
            "wallahi", "ya rab", "yareb", "insha allah", "astaghfirullah", "tayeb", "zain")


def prompt_for(lang: str | None, names: list[str], arabic_prompt: str | None = None) -> str | None:
    """Whisper's initial prompt for a language.

    "ar" -> Arabic prompt + Arabic-script names (all names if none are Arabic)
    "en" -> English prompt + names
    None -> the mixed prompt (the speaker uses both in one breath)
    other languages -> None (no prompt: an English prompt on Arabic audio steers it to English)
    """
    if lang == "ar":
        ar_names = [n for n in names if any(is_arabic_char(ch) for ch in n)]
        return (arabic_prompt or AR_PROMPT) + ", ".join(ar_names or names or [])
    if lang == "en":
        return EN_PROMPT + ", ".join(names or [])
    if lang is None:
        return MIX_PROMPT + ", ".join(names or [])
    return None


def romanised_arabic_check(text: str, ar_runner_prob: float) -> tuple[bool, list[str], int]:
    """An English opener ("okay, so—") can make the language probe say "en", and the Arabic
    that follows then comes back romanised in Latin letters. Decide whether to look again.

    Returns (retry_as_arabic, romanised_markers_found, english_stopword_count).
    Retry when the text has no Arabic letters and either carries a romanised-Arabic marker,
    or Arabic was a strong runner-up (>= 0.35) and the text has almost no English stopwords.
    """
    low = (text or "").lower()
    no_arabic = count_arabic(text) == 0
    markers = [w for w in ROMAN_AR if w in low]
    en_stop = sum(1 for w in re.findall(r"[a-z']+", low) if w in EN_STOP)
    retry = no_arabic and bool(markers or (ar_runner_prob >= 0.35 and en_stop <= 2))
    return retry, markers, en_stop


# ---------------------------------------------------------------- cleaner tone and profiles
# Tone is a light prompt modifier, not a rewrite: it changes register, never meaning.
TONES = {
    "default": "",
    "formal": " Use a formal, professional register: complete sentences, no slang, no emoji.",
    "casual": " Use a relaxed, conversational register, the way one writes to a colleague.",
    "veryCasual": " Use a very casual chat register: contractions, short clauses, lower case is fine.",
    "excited": " Keep the energy high: the speaker is enthusiastic; exclamation marks are welcome.",
    "genz": " Use a contemporary, informal online register; do not invent slang that was not spoken.",
}
_TONES_BY_LOWER = {k.lower(): v for k, v in TONES.items()}


def tone_hint(tone: str | None) -> str:
    """The prompt sentence for a tone, matched case-insensitively ("veryCasual" == "verycasual")."""
    return _TONES_BY_LOWER.get(str(tone or "default").lower(), "")


def profile_for(app: str | None, profiles: list[dict] | None) -> dict:
    """The first profile whose app list appears in this window title, or {}."""
    t = (app or "").lower()
    if not t:
        return {}
    for prof in profiles or []:
        for key in prof.get("apps") or []:
            if key and key.lower() in t:
                return prof
    return {}
