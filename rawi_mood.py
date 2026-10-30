"""rawi_mood — one emoji per dictation, earned by words he actually said.

Not a model. A lexicon in his two languages plus punctuation cues, so every
tag can be explained by the words that earned it ("why": matched tokens).
No match, no emoji: an emoji invented for him would be a lie in a pretty font.

tag(text) -> {"e": "🤍", "m": "tender", "why": ["thank you"]}  or  None
"""
import re
import unicodedata

_AR_DIAC = dict.fromkeys(map(ord, "\u064B\u064C\u064D\u064E\u064F\u0650\u0651\u0652\u0640"), None)


def _norm(t):
    t = (t or "").translate(_AR_DIAC)
    t = t.replace("\u0623", "\u0627").replace("\u0625", "\u0627").replace("\u0622", "\u0627")
    t = t.replace("\u0649", "\u064A").replace("\u0629", "\u0647")
    return " " + re.sub(r"\s+", " ", t.lower()).strip() + " "


# mood: (emoji, [(phrase, weight), ...])  -- phrases are matched on the normalized text
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
    "code":      ("\u2328", [("code", 2), ("bug", 2), ("commit", 2), ("server", 1), ("build", 1),
                               ("port", 1), ("كود", 2), ("سيرفر", 2)]),
}

MIN_SCORE = 2


def tag(text):
    t = _norm(text)
    if not t.strip():
        return None
    best, best_score, best_why = None, 0, []
    for mood, (emoji, phrases) in MOODS.items():
        score, why = 0, []
        for ph, w in phrases:
            hay = _norm(ph).strip()
            if not hay:
                continue
            if re.search(r"[\u0600-\u06FF]", hay):
                hit = hay in t
            elif " " in hay:
                hit = (" " + hay + " ") in t
            elif re.match(r"[a-z0-9]", hay):
                hit = re.search(r"(?<![a-z0-9])" + re.escape(hay), t) is not None
            else:
                hit = hay in t
            if hit:
                score += w
                why.append(ph)
        if score > best_score:
            best, best_score, best_why = mood, score, why
    if best is None or best_score < MIN_SCORE:
        return None
    return {"e": MOODS[best][0], "m": best, "why": best_why[:3]}
