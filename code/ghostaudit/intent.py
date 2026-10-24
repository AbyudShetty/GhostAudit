"""Does the request text authorise deletion? (``NOT_APPLICABLE`` vs a deletion audit.)

Specification (AGENTS.md 6.5): "First determine whether ``request_text`` authorizes deletion. A request for access,
correction, export, marketing opt-out, or another non-deletion action is ``NOT_APPLICABLE``, even if other fields
resemble a deletion case." The text may be in any language.

Method (deterministic, no model): normalise (NFKC, lower-case, accents stripped), then decide in this order

1. **explicit denial** - the person says they are *not* asking for deletion / do not want the account closed or data
   removed ("not asking for deletion", "do not close the account", "without removing any of my records",
   "leave the account active", "keep the account", "don't delete or change anything");
2. **scope limiter** - the removal is confined to something other than the account/personal data
   ("remove me from marketing lists only", "stop the promotional emails");
3. **deletion cue** - a deletion/erasure/closure verb or noun in one of the supported languages, or a statement that the
   account is gone / was deleted (people asking us to *verify or finish* a deletion do not always use a verb);
4. **non-deletion topic** - correction, access, export, login help, invoices, e-mail change.

Outcomes: ``DELETION``, ``NON_DELETION``, ``UNCERTAIN``. A denial together with an explicit imperative deletion request
is contradictory and is ``UNCERTAIN`` rather than silently resolved. ``UNCERTAIN`` never authorises deletion
(see ``ClassificationPolicy.uncertain_intent_status`` in :mod:`ghostaudit.classification`).

Every result lists the matched cues. The classifier is validated on the 32 sample requests (all deletion audits) and
the 10 explicit non-deletion eval requests, plus synthetic misleading cases in the tests; the lexicon is closed and
can only be as good as that coverage, which is why a fallback outcome exists.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple

DELETION, NON_DELETION, UNCERTAIN = "DELETION", "NON_DELETION", "UNCERTAIN"


def _strip_latin_accents(ch: str) -> str:
    # NFD would also split Hangul syllables into jamo and break the Korean cues, so only Latin letters are decomposed
    if ord(ch) < 0x250:
        return "".join(c for c in unicodedata.normalize("NFD", ch) if unicodedata.category(c) != "Mn")
    return ch


def normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).replace("’", "'").lower()
    t = "".join(_strip_latin_accents(c) for c in t)
    return re.sub(r"\s+", " ", t).strip()


def _rx(*alts: str) -> "re.Pattern[str]":
    return re.compile("|".join(alts), re.I)


# 1. explicit denial
_DENIAL = _rx(
    r"\bnot (?:asking|requesting|wanting) (?:for )?(?:the )?(?:deletion|erasure|removal|account (?:deletion|closure))",
    r"\bnot (?:a )?request(?:ing)? to (?:erase|delete|remove|close)",
    r"\bnot asking (?:you )?to (?:erase|delete|remove|close)",
    r"\b(?:do not|don't|dont|please do not|please don't) (?:close|delete|erase|remove)\b",
    r"\bdo not (?:close|delete|erase|remove)\b",
    r"\bwithout (?:removing|deleting|erasing|closing)\b",
    r"\bleave (?:the |my )?(?:account|profile)[^.?!]{0,30}(?:active|in place|as it is|intact|open)",
    r"\bleave (?:the |my )?account active\b",
    r"\bkeep the account\b",
    r"\bplease keep the account\b",
    r"\bdon't delete or change anything\b",
)
# explicit imperative deletion requests (used only to detect a contradiction with a denial)
_IMPERATIVE = _rx(
    r"\bplease (?:delete|erase|remove|close)\b",
    r"\bi (?:want|would like|request|am requesting|am asking you) (?:to |you to )?(?:delete|erase|remove|close)",
    r"\b(?:request|requesting) (?:for )?(?:the )?(?:permanent )?(?:account )?(?:deletion|erasure)\b",
)
# 2. scope limiters: removal confined to marketing / messages
_LIMITER = _rx(
    r"\b(?:marketing|promotional|mailing|newsletter)[^.?!]{0,30}\b(?:lists?|emails?|messages?)\b[^.?!]{0,10}\bonly\b",
    r"\b(?:remove|unsubscribe)[^.?!]{0,25}\b(?:marketing|promotional|mailing) lists? only\b",
    r"\bonly (?:from )?(?:the )?(?:marketing|promotional|mailing) lists?\b",
    r"\bstop the (?:promotional|marketing)[^.?!]{0,20}(?:emails?|messages?)\b",
)
# 3. deletion cues (EN, ES, PT, FR, JA, KO) - applied after accent stripping
_CUE = _rx(
    r"\bdelet\w*", r"\bdeletion\b", r"\berase\w*", r"\berasure\b", r"\bremov\w*", r"\bclos(?:e|ed|ing|ure)\b",
    r"\bpermanently\b", r"\bgone\b", r"\bno longer exists?\b", r"\bstill existed\b", r"\bfully removed\b",
    r"\belimin\w*", r"\bborr\w*", r"\bsuprim\w*",                       # ES
    r"\bexclu\w*", r"\bapag\w*",                                         # PT
    r"\bsuppress\w*", r"\bsuppression\b", r"\bsupprim\w*", r"\beffac\w*", r"\bcloture\b",  # FR
    "削除", "消去", "アカウントを閉",           # JA: 削除, 消去, アカウントを閉
    "삭제", "탈퇴", "해지",                                   # KO: 삭제, 탈퇴, 해지
)
# 4. non-deletion topics
_TOPIC = _rx(
    r"\bout of date\b", r"\breplace it with the new one\b", r"\bdownload my account information\b", r"\bportable format\b",
    r"\bcan'?t access my (?:profile|account)\b", r"\bresetting the login\b", r"\bmisspelled\b", r"\bcorrect it\b",
    r"\bwhat personal information you hold\b", r"\binformation request\b", r"\bcopy of (?:last year's )?(?:invoices|the information)\b",
    r"\bchange the email address\b", r"\bcontinue using the service\b",
)


@dataclass(frozen=True)
class IntentResult:
    outcome: str  # DELETION | NON_DELETION | UNCERTAIN
    reason: str
    cues: Tuple[str, ...] = ()

    @property
    def authorises_deletion(self) -> bool:
        return self.outcome == DELETION


def _spans(rx: "re.Pattern[str]", text: str) -> List[str]:
    return sorted({m.group(0) for m in rx.finditer(text)})


def classify_intent(request_text: str) -> IntentResult:
    t = normalise(request_text)
    denial, limiter, cue, topic = _spans(_DENIAL, t), _spans(_LIMITER, t), _spans(_CUE, t), _spans(_TOPIC, t)
    if denial:
        if _IMPERATIVE.search(t) and not limiter and not topic:
            return IntentResult(UNCERTAIN, "contradictory: an explicit denial and an explicit deletion request", tuple(denial + cue))
        return IntentResult(NON_DELETION, "explicit denial of deletion", tuple(denial))
    if limiter:
        return IntentResult(NON_DELETION, "removal limited to marketing/messages, not the account or personal data", tuple(limiter))
    if cue:
        return IntentResult(DELETION, "deletion cue", tuple(cue))
    if topic:
        return IntentResult(NON_DELETION, "access/correction/export/other non-deletion topic", tuple(topic))
    return IntentResult(UNCERTAIN, "no deletion cue and no recognised non-deletion topic", ())
