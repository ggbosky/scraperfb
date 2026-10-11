"""Změří přesnost předfiltru a LLM klasifikátoru na ručně označených příspěvcích.

  python evaluate.py              # předfiltr + LLM (spotřebuje ~20 LLM volání)
  python evaluate.py --no-llm     # jen předfiltr
  python evaluate.py --file moje_vzorky.jsonl

Formát JSONL: {"text": "...", "intent": "poptavajici|nabizejici|diskuse|spam|jine", "lead": true|false}
Doplňuj sem reálné příspěvky, na kterých se klasifikátor spletl – nejrychlejší cesta k vyšší přesnosti.
"""
from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

from analyzer import KeywordPrefilter, LLMClassifier, LLMError, decide
from config import DEFAULT_DEMAND_KEYWORDS, DEFAULT_NEGATIVE_KEYWORDS, DEFAULT_TOPIC_KEYWORDS, load_settings
from database import utcnow

DEFAULT_FILE = Path(__file__).parent / "tests" / "fixtures" / "labeled_posts.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=str(DEFAULT_FILE))
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    samples = [json.loads(l) for l in Path(args.file).read_text(encoding="utf-8").splitlines() if l.strip()]
    pre = KeywordPrefilter(DEFAULT_TOPIC_KEYWORDS, DEFAULT_DEMAND_KEYWORDS, DEFAULT_NEGATIVE_KEYWORDS)
    clf = None if args.no_llm else LLMClassifier(settings)
    posted_at = utcnow() - timedelta(hours=1)

    tp = fp = fn = tn = intent_ok = intent_total = 0
    for s in samples:
        p = pre.check(s["text"])
        predicted, intent, score, why = False, "-", "-", f"prefilter:{p.reason}"
        if p.passed and clf:
            try:
                a = clf.classify(s["text"], posted_at=posted_at)
                predicted, reason = decide(a, posted_at, settings)
                intent, score, why = a["intent"], a["relevance_score"], reason or "LEAD"
                intent_total += 1
                intent_ok += intent == s["intent"]
            except LLMError as exc:
                why = f"llm_error: {exc}"
        elif p.passed:
            predicted, why = True, "prefilter:ok"

        expected = s["lead"]
        tp += predicted and expected
        fp += predicted and not expected
        fn += (not predicted) and expected
        tn += (not predicted) and not expected
        mark = "✅" if predicted == expected else "❌"
        print(f"{mark} oček.={'LEAD' if expected else 'ne  '} intent={intent:<11} skóre={score!s:<3} {why:<28} | {s['text'][:70]}")

    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    print(f"\nPřesnost (precision): {precision:.0%}  – kolik nahlášených leadů je opravdu lead")
    print(f"Záchyt (recall):      {recall:.0%}  – kolik skutečných leadů jsme našli")
    print(f"TP={tp} FP={fp} FN={fn} TN={tn}")
    if intent_total:
        print(f"Správný záměr (u příspěvků, které prošly do LLM): {intent_ok}/{intent_total}")
    if clf:
        clf.close()


if __name__ == "__main__":
    main()
