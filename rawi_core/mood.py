"""mood — one emoji per dictation, earned by words actually said.

Not a model. A lexicon in two languages plus punctuation cues, so every tag can be explained
by the words that earned it ("why": matched phrases). No match, no emoji: an emoji invented
for the speaker would be a lie in a pretty font.

    tag(text) -> {"e": "🤍", "m": "tender", "why": ["thank you"]}  or  None
"""
from __future__ import annotations

import re

from .text import norm


def _padded(t: str | None) -> str:
    """Normalised text with single spaces and a space at each end, so " phrase " matches whole."""
    return " " + re.sub(r"\s+", " ", norm(t)) + " "


# mood: (emoji, [(phrase, weight), ...])  -- phrases are matched on the normalised text
MOODS = {
    "tender":   ("\U0001F90D", [("thank you", 3), ("thanks", 2), ("love you", 3), ("حبيبي", 3),
                                ("حبيبتي", 3), ("قلبي", 2), ("عيوني", 2), ("احبك", 3), ("الله يسعدك", 3),
                                ("تسلم", 2), ("يا بعدي", 3), ("حياتي", 2)]),
    "longing":  ("\U0001FAE0", [("miss you", 3), ("i miss", 2), ("اشتقت", 4), ("وحشتني", 3),
                                ("توحشني", 3), ("وينك", 2)]),
    "excited":  ("\U0001F525", [("amazing", 2), ("cant wait", 3), ("can't wait", 3), ("lets go", 2),
                                ("يلا", 1), ("حماس", 2), ("متحمس", 3), ("واخيرا", 2), ("اخيرا", 2)]),
    "amused":   ("\U0001F605", [("haha", 2), ("lol", 2), ("\U0001F602", 3), ("ههه", 2), ("هههه", 2),
                                ("funny", 2), ("تضحك", 2), ("صراحه تضحك", 3), ("\U0001F923", 3)]),
    "tired":    ("\U0001F634", [("tired", 3), ("exhausted", 3), ("sleepy", 3), ("i cant", 1),
                                ("تعبان", 3), ("نعسان", 3), ("موقادر", 3), ("مو قادر", 3), ("ما قدرت", 2)]),
    "frustrated": ("\U0001F32A", [("annoying", 3), ("frustrat", 3), ("stupid", 3), ("wtf", 3),
                                  ("زفت", 3), ("مقرف", 3), ("تعبت من", 3), ("مو زابط", 3), ("ما يزبط", 3), ("مو زابطه", 3), ("not working", 2)]),
    "thinking":  ("\U0001F9E0", [("i think", 2), ("maybe", 1), ("what if", 2), ("احس ان", 2),
                                  ("مدري", 1), ("فكرت", 2), ("يعني", 1), ("اتوقع", 2)]),
    "asking":    ("\U0001F914", [("?", 1), ("شلون", 2), ("كيف", 1), ("ليش", 2), ("وش رايك", 3)]),
    "work":      ("\U0001F4BC", [("meeting", 2), ("deck", 2), ("memo", 2), ("email", 2), ("stakeholder", 3),
                                 ("اجتماع", 3), ("ملف", 1), ("تقرير", 3), ("مشروع", 2), ("عرض", 2)]),
    "idea":      ("\U0001F4A1", [("idea", 2), ("concept", 2), ("design", 1), ("architecture", 2),
                                 ("فكره", 2), ("تصميم", 2), ("ابتكار", 3)]),
    "plan":      ("\U0001F331", [("tomorrow", 2), ("next step", 3), ("plan", 2), ("we should", 2),
                                 ("بكره", 2), ("خطه", 2), ("نخطط", 3), ("بنبدا", 2), ("لازم", 2)]),
    "grateful":  ("\U0001F64F", [("alhamdulillah", 4), ("الحمدلله", 4), ("مشكور", 2), ("جزاك", 3),
                                 ("ممتن", 3), ("thank god", 3)]),
    "prayer":    ("\U0001F56F", [("inshallah", 3), ("ان شاء الله", 3), ("يارب", 3), ("دعاء", 3),
                                 ("الله كريم", 3), ("يا رب", 3), ("زابط", 2)]),
    "code":      ("⌨", [("code", 2), ("bug", 2), ("commit", 2), ("server", 1), ("build", 1),
                               ("port", 1), ("كود", 2), ("سيرفر", 2)]),
}

MIN_SCORE = 2           # below this the evidence is too thin to tag anything


def _hit(hay: str, t: str) -> bool:
    """Does phrase `hay` occur in padded text `t`? Latin words must start at a word boundary
    ("lol" in "lollipop" is fine as a prefix match, "plan" in "airplane" is not)."""
    if re.search(r"[؀-ۿ]", hay):
        return hay in t                      # Arabic attaches prefixes, so substring it is
    if " " in hay:
        return (" " + hay + " ") in t
    if re.match(r"[a-z0-9]", hay):
        return re.search(r"(?<![a-z0-9])" + re.escape(hay), t) is not None
    return hay in t                          # punctuation and emoji


def tag(text: str | None) -> dict | None:
    """The best-scoring mood for `text`, with the phrases that earned it; None below MIN_SCORE."""
    t = _padded(text)
    if not t.strip():
        return None
    best, best_score, best_why = None, 0, []
    for mood, (_emoji, phrases) in MOODS.items():
        score, why = 0, []
        for ph, w in phrases:
            hay = norm(ph)
            if hay and _hit(hay, t):
                score += w
                why.append(ph)
        if score > best_score:
            best, best_score, best_why = mood, score, why
    if best is None or best_score < MIN_SCORE:
        return None
    return {"e": MOODS[best][0], "m": best, "why": best_why[:3]}
