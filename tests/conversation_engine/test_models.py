"""tests/conversation_engine/test_models.py"""

from __future__ import annotations

import dataclasses
from types import MappingProxyType

import pytest

from conversation_engine.models import (
    ConversationContextFragment,
    ConversationResponse,
    GenerationPath,
    ResponseCategory,
    ResponseContextSnapshot,
    ResponsePlan,
    SituationalConstraints,
    ToolCallRequest,
    UnsupportedFragmentDataError,
)
from ai.identity_catalog import CommunicationProfile


def _profile(**overrides) -> CommunicationProfile:
    kwargs = dict(warmth=0.5, humor=0.5, teasing=0.5, assertiveness=0.5, formality=0.5, verbosity=0.5)
    kwargs.update(overrides)
    return CommunicationProfile(**kwargs)


class TestConversationContextFragment:
    def test_empty_namespace_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            ConversationContextFragment(namespace="", data={})

    def test_whitespace_only_namespace_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            ConversationContextFragment(namespace="   ", data={})

    def test_top_level_data_is_immutable(self) -> None:
        fragment = ConversationContextFragment(namespace="ns", data={"a": 1})
        assert isinstance(fragment.data, MappingProxyType)
        with pytest.raises(TypeError):
            fragment.data["a"] = 2  # type: ignore[index]

    def test_nested_dict_is_immutable(self) -> None:
        """Point 3's own requirement: a top-level MappingProxyType alone
        leaves nested structures mutable -- this proves the recursive
        freeze actually reaches them."""
        fragment = ConversationContextFragment(namespace="ns", data={"outer": {"inner": 1}})
        assert isinstance(fragment.data["outer"], MappingProxyType)
        with pytest.raises(TypeError):
            fragment.data["outer"]["inner"] = 2  # type: ignore[index]

    def test_nested_list_becomes_an_immutable_tuple(self) -> None:
        fragment = ConversationContextFragment(namespace="ns", data={"items": [1, 2, 3]})
        assert isinstance(fragment.data["items"], tuple)
        with pytest.raises(AttributeError):
            fragment.data["items"].append(4)  # type: ignore[attr-defined]

    def test_deeply_nested_structure_is_fully_frozen(self) -> None:
        fragment = ConversationContextFragment(
            namespace="ns", data={"a": [{"b": [1, 2, {"c": "d"}]}]},
        )
        inner = fragment.data["a"][0]["b"][2]
        assert isinstance(inner, MappingProxyType)
        with pytest.raises(TypeError):
            inner["c"] = "changed"  # type: ignore[index]

    def test_mutating_the_original_input_after_construction_does_not_affect_the_fragment(self) -> None:
        original = {"a": [1, 2, 3]}
        fragment = ConversationContextFragment(namespace="ns", data=original)
        original["a"].append(4)
        original["b"] = "new"
        assert fragment.data["a"] == (1, 2, 3)
        assert "b" not in fragment.data

    def test_set_becomes_frozenset(self) -> None:
        fragment = ConversationContextFragment(namespace="ns", data={"tags": {"a", "b"}})
        assert isinstance(fragment.data["tags"], frozenset)

    def test_scalars_pass_through_unchanged(self) -> None:
        fragment = ConversationContextFragment(
            namespace="ns", data={"s": "text", "i": 1, "f": 1.5, "b": True, "n": None},
        )
        assert fragment.data["s"] == "text"
        assert fragment.data["i"] == 1
        assert fragment.data["f"] == 1.5
        assert fragment.data["b"] is True
        assert fragment.data["n"] is None

    def test_unsupported_type_is_rejected(self) -> None:
        class Arbitrary:
            pass

        with pytest.raises(UnsupportedFragmentDataError):
            ConversationContextFragment(namespace="ns", data={"x": Arbitrary()})


class TestSituationalConstraints:
    def test_all_none_is_valid(self) -> None:
        SituationalConstraints()  # must not raise

    def test_value_in_range_is_valid(self) -> None:
        SituationalConstraints(max_humor=0.0, max_teasing=1.0, max_assertiveness=0.5, max_verbosity=0.3)

    def test_value_above_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="0.0-1.0"):
            SituationalConstraints(max_humor=1.1)

    def test_value_below_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="0.0-1.0"):
            SituationalConstraints(max_teasing=-0.1)

    def test_non_numeric_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="number"):
            SituationalConstraints(max_assertiveness="high")  # type: ignore[arg-type]

    def test_bool_rejected_even_though_bool_is_technically_an_int(self) -> None:
        with pytest.raises(ValueError, match="number"):
            SituationalConstraints(max_verbosity=True)  # type: ignore[arg-type]


class TestResponsePlan:
    def test_default_tool_calls_is_empty(self) -> None:
        plan = ResponsePlan(response_category=ResponseCategory.ERROR_FALLBACK)
        assert plan.tool_calls == ()

    def test_generation_path_defaults_to_deterministic_fallback(self) -> None:
        plan = ResponsePlan(response_category=ResponseCategory.ERROR_FALLBACK)
        assert plan.generation_path == GenerationPath.DETERMINISTIC_FALLBACK

    def test_overlapping_required_and_optional_namespaces_rejected(self) -> None:
        with pytest.raises(ValueError, match="overlap"):
            ResponsePlan(
                response_category=ResponseCategory.INFORMATIONAL_STATUS,
                required_provider_namespaces=frozenset({"a", "b"}),
                optional_provider_namespaces=frozenset({"b", "c"}),
            )

    def test_a_tool_call_can_be_constructed_as_a_sketch_but_is_never_used_by_production_code(self) -> None:
        """Confirms the TYPE exists and is usable (Section 11's own
        sketch requirement) -- production code path is what actually
        enforces it never gets populated (see test_validation.py)."""
        call = ToolCallRequest(tool_name="example", parameters={"x": 1})
        plan = ResponsePlan(response_category=ResponseCategory.ERROR_FALLBACK, tool_calls=(call,))
        assert plan.tool_calls == (call,)  # constructible, not forbidden by the type itself


class TestResponseContextSnapshot:
    def test_is_frozen(self) -> None:
        snapshot = ResponseContextSnapshot(
            response_category=ResponseCategory.ERROR_FALLBACK, current_user_message="hi",
            language="en", identity_profile=_profile(), identity_id="alex", situational_constraints=SituationalConstraints(),
            context_fragments={},
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            snapshot.language = "cs"  # type: ignore[misc]

    def test_context_fragments_is_immutable_mapping(self) -> None:
        fragment = ConversationContextFragment(namespace="ns", data={})
        snapshot = ResponseContextSnapshot(
            response_category=ResponseCategory.ERROR_FALLBACK, current_user_message="hi",
            language="en", identity_profile=_profile(), identity_id="alex", situational_constraints=SituationalConstraints(),
            context_fragments={"ns": fragment},
        )
        assert isinstance(snapshot.context_fragments, MappingProxyType)
        with pytest.raises(TypeError):
            snapshot.context_fragments["other"] = fragment  # type: ignore[index]

    def test_mismatched_fragment_key_is_rejected(self) -> None:
        fragment = ConversationContextFragment(namespace="real_ns", data={})
        with pytest.raises(ValueError, match="does not match"):
            ResponseContextSnapshot(
                response_category=ResponseCategory.ERROR_FALLBACK, current_user_message="hi",
                language="en", identity_profile=_profile(), identity_id="alex", situational_constraints=SituationalConstraints(),
                context_fragments={"wrong_key": fragment},
            )


class TestConversationResponse:
    def test_has_no_field_expressing_a_domain_write(self) -> None:
        """Structural proof, not just a docstring claim -- the field
        set is exactly {text, response_category, tool_outputs}, nothing
        write-shaped. INTENTIONALLY EXTENDED for read-only tool calling:
        `tool_outputs` records what a read-only tool READ (default
        empty); it is not, and cannot express, a write request."""
        field_names = {f.name for f in dataclasses.fields(ConversationResponse)}
        assert field_names == {"text", "response_category", "tool_outputs"}
        assert ConversationResponse(text="x", response_category=ResponseCategory.COACHING_DIALOGUE).tool_outputs == ()
