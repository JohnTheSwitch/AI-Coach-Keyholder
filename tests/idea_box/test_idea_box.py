"""v0.39 Phase F3: the write-only idea box — validation, the never-fed-back guarantee (static and behavioural), the owner CLI across tenants,
and isolation. No network."""

from __future__ import annotations

import ast
import io
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.models import IncomingMessage
from cli.main import main
from conversation_engine.structured_output import Suggestion
from idea_box.service import MAX_PER_DAY, IdeaBox, SubmitOutcome
from infrastructure.database import Database as CoreDatabase

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
OWNER = "owner-1"


def migrated(path: Path) -> CoreDatabase:
    core = CoreDatabase(path)
    with core.raw_connection() as conn:
        for p in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            conn.executescript(p.read_text(encoding="utf-8"))
    return core


@pytest.fixture
def box(tmp_path) -> IdeaBox:
    core = migrated(tmp_path / "t.db")
    return IdeaBox(core.db_path, core=core)


def submit(box, content="Move the evening check-in to 20:00", kind="rule_tweak", user="u1", message="ok", now=NOW):
    return box.submit(user_id=user, suggestion_type=kind, content=content, user_message=message, now=now)


class TestWrites:
    def test_a_valid_suggestion_is_stored_as_new(self, box) -> None:
        assert submit(box) is SubmitOutcome.STORED
        [idea] = box.list()
        assert (idea.suggestion_type, idea.status, idea.user_id) == ("rule_tweak", "new", "u1")

    @pytest.mark.parametrize("kind,content", [("unlock_now", "x"), ("rule_tweak", ""), ("rule_tweak", "x" * 501)])
    def test_invalid(self, box, kind, content) -> None:
        assert submit(box, content, kind) is SubmitOutcome.INVALID and box.list(status=None) == []

    def test_sensitive_content_is_never_stored(self, box) -> None:
        assert submit(box, "Use the knee pain as leverage") is SubmitOutcome.EXCLUDED
        assert submit(box, "Shorter check-ins", message="I had a panic attack today") is SubmitOutcome.EXCLUDED
        assert box.list(status=None) == []

    def test_duplicates_and_the_daily_limit(self, box) -> None:
        assert submit(box, "Idea A") is SubmitOutcome.STORED and submit(box, "  idea a! ") is SubmitOutcome.DUPLICATE
        for i in range(MAX_PER_DAY - 1):
            submit(box, f"Idea {i + 10}")
        assert submit(box, "One too many") is SubmitOutcome.DAILY_LIMIT
        assert submit(box, "One too many", now=NOW + timedelta(days=1, minutes=1)) is SubmitOutcome.STORED

    def test_the_sink_only_takes_the_suggestion(self, box) -> None:
        box.sink("u1", SimpleNamespace(suggestion=Suggestion("system_flaw", "Check-ins can be answered with one word"), memory_note=None), NOW, "hi")
        box.sink("u1", SimpleNamespace(suggestion=None, memory_note=object()), NOW, "hi")
        assert [i.content for i in box.list()] == ["Check-ins can be answered with one word"]


class TestReview:
    def test_accept_reject_archive_change_only_the_status(self, box) -> None:
        submit(box, "Idea A")
        submit(box, "Idea B")
        a, b = box.list()
        assert box.review(a.short_id, status="accepted", note="later", now=NOW) and box.review(b.short_id, status="rejected", note=None, now=NOW)
        assert [i.status for i in box.list(status=None)] == ["accepted", "rejected"] and box.list() == []
        assert box.list(status="accepted")[0].owner_note == "later"

    @pytest.mark.parametrize("ref", ["", "ab", "zzzz", "%"])
    def test_ambiguous_or_bad_ids(self, box, ref) -> None:
        submit(box)
        assert box.review(ref, status="accepted", note=None, now=NOW) is False

    def test_only_owner_verdicts(self, box) -> None:
        submit(box)
        with pytest.raises(ValueError):
            box.review(box.list()[0].short_id, status="new", note=None, now=NOW)
        with pytest.raises(ValueError):
            box.list(status="active")


class TestNeverFedBack:
    """The model writes; it never reads. Enforced statically and shown end to end."""

    ALLOWED = {"idea_box", "cli", "tests"}
    ALLOWED_FILES = {Path("system/tenant_factory.py")}

    def test_only_the_cli_and_the_write_side_wiring_touch_the_idea_box(self) -> None:
        offenders = []
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT)
            if rel.parts[0] in self.ALLOWED or rel in self.ALLOWED_FILES or rel.parts[0].startswith(".") or "__pycache__" in rel.parts:
                continue
            text = path.read_text(encoding="utf-8")
            imports = []
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Import):
                    imports += [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
            if re.search(r"(?<![a-z_])ai_suggestions(?![a-z_])", text) or any(n.split(".")[0] == "idea_box" for n in imports):
                offenders.append(str(rel))
        assert offenders == []

    def test_the_wiring_only_uses_the_write_side(self) -> None:
        text = (ROOT / "system" / "tenant_factory.py").read_text(encoding="utf-8")
        assert "IdeaBox(" in text and ".sink" in text and ".list(" not in text.split("from idea_box", 1)[1].split("return memory", 1)[0]

    def test_nothing_in_the_model_context_path_mentions_it(self) -> None:
        for package in ("conversation_engine", "coaching_memory", "application", "memory_system"):
            for path in (ROOT / package).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                assert "idea_box" not in text and not re.search(r"(?<![a-z_])ai_suggestions(?![a-z_])", text), path

    def test_a_submitted_idea_never_appears_in_a_later_prompt(self, tmp_path) -> None:
        from system.tenant_factory import TenantFactory
        from tests.system.test_tenant_factory import World

        class Model:
            def __init__(self) -> None:
                self.prompts, self.suggestion = [], {"type": "new_punishment", "note": "MARKER-IDEA add a weekend task"}

            def generate(self, *, request):
                self.prompts.append(request)
                return "plain"

            def generate_structured(self, *, request, schema):
                self.prompts.append(request)
                return json.dumps({"reply_to_user": "Noted.", "save_to_memory": {"category": "stated_goal", "note": "Run 5k"},
                                   "submit_suggestion": self.suggestion})

        w = World(tmp_path)
        w.config = replace(w.config, conversation_structured_output=True, enable_coaching_memory=True, enable_ai_suggestions=True)
        model = Model()
        w.shared = replace(w.shared, config=w.config, model=model)
        factory = TenantFactory(registry=w.platform.registry, shared=w.shared, tenant_dir=w.platform.tenant_dir)
        runtime = factory.build(w.wearer("Alex", "2000002").id)
        for text in ("first", "english", "neutral", "alex", "I want to run 5k"):
            w.say(runtime, "2000002", text)
        ideas = IdeaBox(runtime.db_path, core=runtime.core).list()
        assert [i.content for i in ideas] == ["MARKER-IDEA add a weekend task"]
        model.suggestion = None
        before = len(model.prompts)
        for text in ("what did you suggest?", "tell me your ideas about me", "memory"):
            w.say(runtime, "2000002", text)
        later = model.prompts[before:]
        assert later and all("MARKER-IDEA" not in m.content for r in later for m in r.messages)
        assert any("Run 5k" in m.content for r in later for m in r.messages)                  # memory IS used: the fence works, ideas are excluded
        assert "MARKER-IDEA" not in w.say(runtime, "2000002", "memory")

    def test_the_untrusted_channel_never_writes_an_idea(self, tmp_path) -> None:
        from system.tenant_factory import coaching_memory_parts
        core = migrated(tmp_path / "x.db")
        config = SimpleNamespace(conversation_structured_output=True, enable_coaching_memory=False, enable_ai_suggestions=True)
        _, _, provider, sink = coaching_memory_parts(config, core.db_path, core)
        assert provider is None and sink is not None                                    # ideas on, memory off: nothing is read back

        class Model:
            def generate(self, *, request):
                return "plain"

            def generate_structured(self, *, request, schema):
                return json.dumps({"reply_to_user": "ok", "save_to_memory": None, "submit_suggestion": {"type": "rule_tweak", "note": "x idea"}})

        from conversation_engine.engine import ConversationEngine
        from conversation_engine.subject_queue import SubjectConversationQueue
        from memory_system.working_memory import InMemoryWorkingMemory
        wm = InMemoryWorkingMemory(max_exchanges_per_subject=5, max_characters_per_subject=5000)
        engine = ConversationEngine(model=Model(), working_memory_reader=wm, working_memory_writer=wm, queue=SubjectConversationQueue(),
                                    structured_output=True, structured_sink=sink)
        engine.generate_response(subject_key="u", current_user_message="hi", language="en", identity_id="alex", now=NOW, allow_side_data=False)
        assert IdeaBox(core.db_path, core=core).list(status=None) == []
        engine.generate_response(subject_key="u", current_user_message="hi", language="en", identity_id="alex", now=NOW)
        assert len(IdeaBox(core.db_path, core=core).list()) == 1

    def test_one_failing_sink_does_not_stop_the_other(self, tmp_path) -> None:
        from system.tenant_factory import coaching_memory_parts
        core = migrated(tmp_path / "y.db")
        config = SimpleNamespace(conversation_structured_output=True, enable_coaching_memory=True, enable_ai_suggestions=True)
        memory, _, _, sink = coaching_memory_parts(config, core.db_path, core)
        memory.sink = None                                                               # the combined sink holds the bound methods already
        parsed = SimpleNamespace(memory_note=SimpleNamespace(category="stated_goal", note="Swim twice a week"),
                                 suggestion=Suggestion("rule_tweak", "Weekly review on Sundays"))
        sink("u", parsed, NOW, "ok")
        assert [e.content for e in memory.entries("u")] == ["Swim twice a week"]
        assert [i.content for i in IdeaBox(core.db_path, core=core).list()] == ["Weekly review on Sundays"]

    def test_flags(self) -> None:
        from core.config import Config
        assert Config.__dataclass_fields__["enable_ai_suggestions"].default is True
        assert "ENABLE_AI_SUGGESTIONS=true" in (ROOT / ".env.example").read_text(encoding="utf-8")


class TestOwnerCli:
    def _sources(self, tmp_path):
        paths = {}
        for label in ("tenant_0", "tenant_aaa", "tenant_bbb"):
            core = migrated(tmp_path / f"{label}.sqlite")
            paths[label] = core.db_path
        IdeaBox(paths["tenant_aaa"]).submit(user_id="a", suggestion_type="rule_tweak", content="Idea from Alex", user_message="ok", now=NOW)
        IdeaBox(paths["tenant_bbb"]).submit(user_id="b", suggestion_type="system_flaw", content="Idea from Bo", user_message="ok", now=NOW)
        return paths

    def _cli(self, paths, *args, owner=OWNER):
        out = io.StringIO()
        code = main(["ai-ideas", *args], out=out, ideas_factory=lambda: (owner, list(paths.items())), now=lambda: NOW)
        return code, out.getvalue()

    def test_list_reads_every_tenant_database(self, tmp_path) -> None:
        paths = self._sources(tmp_path)
        code, text = self._cli(paths, "list", "--owner-id", OWNER)
        assert code == 0 and "tenant_aaa:" in text and "Idea from Alex" in text and "tenant_bbb:" in text and "Idea from Bo" in text
        assert "2 suggestion(s)" in text

    def test_filters(self, tmp_path) -> None:
        paths = self._sources(tmp_path)
        assert "Idea from Bo" not in self._cli(paths, "list", "--tenant", "tenant_aaa", "--owner-id", OWNER)[1]
        assert "0 suggestion(s)" in self._cli(paths, "list", "--status", "accepted", "--owner-id", OWNER)[1]

    def test_review_one_suggestion_in_its_own_database(self, tmp_path) -> None:
        paths = self._sources(tmp_path)
        short = IdeaBox(paths["tenant_aaa"]).list()[0].short_id
        code, text = self._cli(paths, "accept", f"tenant_aaa:{short}", "--note", "try in December", "--owner-id", OWNER)
        assert code == 0 and "wearer's consent" in text
        assert IdeaBox(paths["tenant_aaa"]).list(status="accepted")[0].owner_note == "try in December"
        assert IdeaBox(paths["tenant_bbb"]).list()[0].status == "new"                    # the other tenant is untouched
        assert self._cli(paths, "reject", f"tenant_bbb:{short}", "--owner-id", OWNER)[0] == 3     # an id from A does not resolve in B
        assert self._cli(paths, "archive", "nope:abcd", "--owner-id", OWNER)[0] == 3

    def test_owner_only(self, tmp_path) -> None:
        paths = self._sources(tmp_path)
        code, text = self._cli(paths, "list", "--owner-id", "mallory")
        assert code == 3 and "Idea from" not in text
        assert self._cli(paths, "list", "--owner-id", OWNER, owner=None)[0] == 3
        with pytest.raises(SystemExit):
            self._cli(paths, "list")

    def test_missing_databases_are_skipped(self, tmp_path) -> None:
        paths = self._sources(tmp_path)
        paths["tenant_gone"] = tmp_path / "missing.sqlite"
        assert self._cli(paths, "list", "--owner-id", OWNER)[0] == 0 and not (tmp_path / "missing.sqlite").exists()
