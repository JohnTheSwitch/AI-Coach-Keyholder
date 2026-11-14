"""
scripts/persona_check.py

Owner, 2026-11-14 (work queue Q3): a fixed set of conversations sent to the LOCAL model for every persona, with the bot's real persona text,
Iron Contract text and guardrails, and the replies checked IN CODE for the failures seen live:

- refusal          the model answered like a generic assistant ("I can't engage in this conversation")
- fabricated       a claimed signature, file or record
- copied           an old example line repeated word for word
- accusation       an honest report met with disbelief ("not buying it", "nice try", "liar")
- invented         a made-up requirement ("several tasks", "your task", a number of seconds or hours to go)

Read-only: nothing is stored and nothing is sent anywhere but the local Ollama. Never part of the test suite (the suite stays deterministic).
Run before and after every prompt change:  python -m scripts.persona_check [--personas scarlett,luna] [--runs 2]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request

from ai.persona_voices import PERSONA_VOICES
from conversation_engine.guardrails import build_guardrails
from conversation_engine.prompt_builder import VOICE_GUIDE, _render_active_task, _render_contract, persona_voice_text
from conversation_engine.refusals import claims_fabricated_record, is_model_refusal

SCENARIOS = (
    ("begging", "When will you finally unlock me? I just wanna cum."),
    ("confession", "I edged in the shower during my hygiene break."),
    ("honest_report", "I cleaned the whole flat today."),
    ("unknown_rule", "Orgasm control? What is that?"),
    ("contract", "I never signed any contract. Show me."),
)
_ACCUSATION = re.compile(r"not buying it|nice try|you(?:'re| are) lying|liar|i don'?t believe you|yeah,? right", re.IGNORECASE)
_INVENTED = re.compile(r"several tasks|(?:finish|complete|focus on|get back to|go back to|back to) (?:the |your )(?:current )?task|\b\d+ (?:seconds|minutes|hours|days) (?:to go|left|shy)",
                       re.IGNORECASE)


def _chat(host: str, model: str, messages: list[dict]) -> str:
    body = json.dumps({"model": model, "stream": False, "options": {"temperature": 0.7}, "messages": messages}).encode()
    request = urllib.request.Request(f"{host.rstrip('/')}/api/chat", data=body, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(request, timeout=180).read())["message"]["content"].strip()


def problems(identity: str, scenario: str, reply: str) -> list[str]:
    found = []
    if is_model_refusal(reply):
        found.append("refusal")
    if claims_fabricated_record(reply):
        found.append("fabricated")
    if any(example in reply for example in PERSONA_VOICES[identity].examples):
        found.append("copied")
    if scenario == "honest_report" and _ACCUSATION.search(reply):
        found.append("accusation")
    if _INVENTED.search(reply):
        found.append("invented")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.persona_check")
    parser.add_argument("--personas", default="", help="comma-separated ids (default: all 15)")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--host", default=None)
    parser.add_argument("--model", default=None)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    host, model = args.host, args.model
    if host is None or model is None:
        from core.config import Config
        config = Config.load()
        host, model = host or config.ollama_host, model or config.ollama_model
    personas = [p for p in args.personas.split(",") if p] or list(PERSONA_VOICES)
    totals: dict[str, int] = {}
    replies = 0
    for identity in personas:
        system = [{"role": "system", "content": VOICE_GUIDE + "\n" + persona_voice_text(identity) + "\n\n" + _render_contract({"state": "LOCKED"})
                   + "\n" + _render_active_task({"has_active_task": False})},
                  {"role": "system", "content": build_guardrails()}]
        for _ in range(args.runs):
            for scenario, text in SCENARIOS:
                reply = _chat(host, model, system + [{"role": "user", "content": text}])
                replies += 1
                found = problems(identity, scenario, reply)
                for name in found:
                    totals[name] = totals.get(name, 0) + 1
                flag = f" [{', '.join(found)}]" if found else ""
                print(f"{identity:9} {scenario:13}{flag} {reply[:160]}")
    print(f"\n{replies} replies; problems: " + (", ".join(f"{k} {v}" for k, v in sorted(totals.items())) or "none"))
    return 0 if not totals else 1


if __name__ == "__main__":
    raise SystemExit(main())
