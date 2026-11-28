"""D58 correction (owner, 2026-11-28)."""



def test_days_evaluated_while_unlocked_are_taken_back(tmp_path) -> None:
    """Owner, 2026-11-28 ("Take it back"): 26 and 27 Nov counted as locked days while he was unlocked (D58)."""
    from datetime import date, datetime, timezone
    from decimal import Decimal
    from system.daily_evaluation import DailyEvaluationTask
    from tests.lines.test_lines import core as make_core

    core = make_core(tmp_path)
    now = datetime(2026, 11, 28, 9, 0, tzinfo=timezone.utc)
    with core.raw_connection() as conn:
        for d, progress, pool, streak, paused in [("2026-11-25", "0.28", "0.428", 1, 0), ("2026-11-26", "0.28", "0.708", 2, 0),
                                                   ("2026-11-27", "0.28", "0.988", 3, 0)]:
            conn.execute("INSERT INTO daily_evaluations (user_id, local_date, goals_assigned, goals_met, check_ins_due, check_ins_ok, violations, "
                         "trust_used, progress, pool_after, tokens_earned, clean, streak_after, bonus, created_at, paused) "
                         "VALUES ('u', ?, 0, 0, 0, 0, 0, '0', ?, ?, 0, 1, ?, '0', '2026-11-28T00:00:00Z', ?)", (d, progress, pool, streak, paused))
        conn.execute("INSERT INTO token_pool_state (user_id, pool, updated_at) VALUES ('u', '0.988', '2026-11-28T00:00:00Z')")
        conn.execute("INSERT INTO clean_streak_state (user_id, streak, updated_at) VALUES ('u', 3, '2026-11-28T00:00:00Z')")
        conn.commit()
    task = DailyEvaluationTask.__new__(DailyEvaluationTask)
    task._core, task._user = core, "u"
    days = [date(2026, 11, 26), date(2026, 11, 27)]
    assert task.correct_as_paused(days, reason="test", now=now) == ["2026-11-26", "2026-11-27"]
    with core.transaction() as tx:
        assert Decimal(tx.fetch_one("SELECT pool FROM token_pool_state")["pool"]) == Decimal("0.428")
        assert tx.fetch_one("SELECT streak FROM clean_streak_state")["streak"] == 1
        assert [r["paused"] for r in tx.fetch_all("SELECT paused FROM daily_evaluations ORDER BY local_date")] == [0, 1, 1]
        assert tx.fetch_one("SELECT 1 FROM domain_events WHERE event_type = 'daily_evaluation.corrected'") is not None
    assert task.correct_as_paused(days, reason="test", now=now) == []             # once only
    assert task.correct_as_paused([date(2026, 11, 25)], reason="test", now=now) == []   # not the newest days: nothing
