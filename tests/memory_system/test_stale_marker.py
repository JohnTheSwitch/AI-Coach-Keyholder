"""Bug fix 2026-11-16: yesterday evening's exchanges came back after a restart and read as today's (\"dancing school tonight\")."""

from datetime import datetime, timedelta, timezone

from memory_system.working_memory import InMemoryWorkingMemory

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)


def test_an_old_exchange_says_how_long_ago_it_was() -> None:
    memory = InMemoryWorkingMemory(max_exchanges_per_subject=10, max_characters_per_subject=8000, clock=lambda: NOW, max_age=timedelta(hours=36))
    memory.commit_exchange(subject_key="u", user_content="I have dancing school tonight", assistant_content="Have fun.", at=NOW - timedelta(hours=17))
    memory.commit_exchange(subject_key="u", user_content="Workout today?", assistant_content="Yes.", at=NOW - timedelta(minutes=5))
    turns = [t.content for t in memory.read(subject_key="u").turns]
    assert turns[0] == "[sent 17 hours ago] I have dancing school tonight" and turns[2] == "Workout today?"
