"""
v0.39: the coarse release enum is the ONLY release datum a model ever sees, the prompt renders it without any
timing, and guardrail 6 forbids hinting at timing. _SYSTEM_BOUNDARIES stays byte-identical.
"""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conversation_engine.context_providers.release_phase_provider import (
    ALLOWED_RELEASE_PHASES,
    ReleasePhaseContextProvider,
)
from conversation_engine.guardrails import CONVERSATION_GUARDRAILS, build_guardrails
from conversation_engine.prompt_builder import build_generation_request
from tests.conversation_engine.test_prompt_domain_context import _lock_fragment, _plan, _snapshot

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
ROOT = Path(__file__).parent.parent.parent


def system_text(fragments) -> str:
    request = build_generation_request(
        snapshot=_snapshot(context_fragments=fragments), plan=_plan(), working_memory_turns=(), max_output_characters=1800,
    )
    return request.messages[0].content


class TestProvider:
    @pytest.mark.parametrize("phase", sorted(ALLOWED_RELEASE_PHASES))
    def test_only_the_two_coarse_phases_pass(self, phase) -> None:
        frag = ReleasePhaseContextProvider(get_phase=lambda subject: phase).provide_context(subject_key="u", now=NOW)
        assert frag.namespace == "release" and dict(frag.data) == {"release_phase": phase}

    @pytest.mark.parametrize("phase", [None, "", "released", "REQUIREMENTS_MET_PENDING", "2026-01-01T18:00:00Z", "18:30", "soon", 5])
    def test_anything_else_yields_no_fragment(self, phase) -> None:
        assert ReleasePhaseContextProvider(get_phase=lambda subject: phase).provide_context(subject_key="u", now=NOW) is None

    def test_the_subject_key_is_passed_through(self) -> None:
        seen = []
        ReleasePhaseContextProvider(get_phase=lambda s: seen.append(s)).provide_context(subject_key="user-9", now=NOW)
        assert seen == ["user-9"]

    def test_allowed_values_are_exactly_the_two_enums(self) -> None:
        assert ALLOWED_RELEASE_PHASES == {"requirements_met_pending", "requirements_not_met"}


class TestPromptRendering:
    def test_met_pending_is_rendered_without_any_timing(self) -> None:
        from conversation_engine.models import ConversationContextFragment
        text = system_text({"release": ConversationContextFragment(namespace="release",
                                                                   data={"release_phase": "requirements_met_pending"})})
        line = next(l for l in text.splitlines() if l.startswith("Release:"))
        assert "requirements are met" in line and "Never say or hint when" in line
        assert not re.search(r"\d", line)

    def test_not_met_is_rendered(self) -> None:
        from conversation_engine.models import ConversationContextFragment
        text = system_text({"release": ConversationContextFragment(namespace="release",
                                                                   data={"release_phase": "requirements_not_met"})})
        assert "Release: the user's requirements are not met yet." in text

    def test_release_comes_after_the_lock_state_and_is_part_of_the_authoritative_section(self) -> None:
        from conversation_engine.models import ConversationContextFragment
        text = system_text({
            "release": ConversationContextFragment(namespace="release", data={"release_phase": "requirements_not_met"}),
            "lock_state": _lock_fragment(),
        })
        assert text.index("Lock:") < text.index("Release:") and "AUTHORITATIVE APPLICATION STATE" in text

    def test_no_release_fragment_means_no_release_line(self) -> None:
        assert "Release:" not in system_text({"lock_state": _lock_fragment()})


class TestGuardrailRule6:
    def test_rule_is_present_and_in_the_final_guardrail_message(self) -> None:
        g = build_guardrails()
        assert "6. RELEASE AND SUSPENSE" in CONVERSATION_GUARDRAILS and "RELEASE AND SUSPENSE" in g

    def test_it_forbids_timing_and_counts_and_promises(self) -> None:
        rule = CONVERSATION_GUARDRAILS.split("6. RELEASE AND SUSPENSE")[1]
        for phrase in ("Never state, estimate, hint at or confirm when", "no times, dates, durations", "Never say\n   how many requirements",
                       "Never promise a release", "keep that to yourself"):
            assert phrase in rule

    def test_it_uses_the_approved_suspense_phrase(self) -> None:
        assert "the\n   timing of your release is up to me, within safety limits" in CONVERSATION_GUARDRAILS

    def test_the_safety_exception_still_wins(self) -> None:
        assert "The safety\n   exception in rule 3 always wins" in CONVERSATION_GUARDRAILS and "SAFETY EXCEPTION" in CONVERSATION_GUARDRAILS

    def test_the_example_line_contains_no_time(self) -> None:
        example = re.search(r'"Your requirements are met.*?"', CONVERSATION_GUARDRAILS, re.S).group(0)
        assert not re.search(r"\d|tonight|soon|later", example)


class TestSystemBoundariesAreUntouched:
    @staticmethod
    def _block(text: str) -> str:
        m = re.search(r'_SYSTEM_BOUNDARIES\s*=\s*(?:\(|""")(.*?)(?:\n\)|""")\s*\n', text, re.S)
        return hashlib.md5(m.group(0).encode()).hexdigest()

    def test_md5_matches_the_protected_value(self) -> None:
        current = (ROOT / "conversation_engine" / "prompt_builder.py").read_text(encoding="utf-8")
        assert self._block(current) == "846faf9aa5b8c4288c2c49656d5aa34a"


class TestStructuralIsolation:
    def test_conversation_engine_never_imports_the_release_machinery(self) -> None:
        forbidden = ("release_engine", "schedule_windows", "supervision", "chaster_execution", "rules_engine", "system")
        for path in (ROOT / "conversation_engine").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
                assert not [n for n in names if n.split(".")[0] in forbidden], (path.name, names)

    def test_the_release_provider_reads_no_database(self) -> None:
        text = (ROOT / "conversation_engine" / "context_providers" / "release_phase_provider.py").read_text(encoding="utf-8")
        assert "sqlite" not in text and "transaction" not in text
