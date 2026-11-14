"""Owner, 2026-11-14 ("brat taming"): while hygiene Trust is below 0.70, every hygiene opening on the OWNER's lock ends with a verification
picture; at or above it the bot switches that off again. Never on another wearer's lock. No network."""

from __future__ import annotations

from dataclasses import replace

from chaster.extension_plan import HYGIENE_PICTURE_BELOW, owner_lock_settings, plan_extension_policy
from chaster_execution.extension_guard import ExtensionGuard
from tests.chaster_execution.test_extension_guard import LOCK, NOW, OPENING, Control, Reader, World, _config, _ext

PICTURE = _ext("verification-picture", {"visibility": "keyholder", "peerVerification": {"enabled": True, "punishments": []}}, regularity=86400.0)


def owner_lock(picture_after=False, extra=(PICTURE,)):
    config = _config(penalty=0, extra=extra)
    opening = _ext("temporary-opening", {**OPENING, "penaltyTime": 0, "requireVerificationPictureAfter": picture_after})
    return replace(config, extensions=(opening, *config.extensions[1:]))


def changes(plan):
    return [(c.field, c.after) for c in plan.changes]


def test_the_threshold_is_the_owners_decision() -> None:
    assert HYGIENE_PICTURE_BELOW == 0.70
    assert owner_lock_settings(0.60) == {"temporary-opening": {"requireVerificationPictureAfter": True}}
    assert owner_lock_settings(0.70) == {"temporary-opening": {"requireVerificationPictureAfter": False}}
    assert owner_lock_settings(None) == {}


def test_low_trust_turns_it_on_and_earned_trust_turns_it_off() -> None:
    assert changes(plan_extension_policy(owner_lock(False), owner_lock=True, hygiene_score=0.60)) == [("requireVerificationPictureAfter", "True")]
    assert changes(plan_extension_policy(owner_lock(True), owner_lock=True, hygiene_score=0.71)) == [("requireVerificationPictureAfter", "False")]
    assert not plan_extension_policy(owner_lock(True), owner_lock=True, hygiene_score=0.60).has_changes


def test_never_on_another_lock_without_a_score_or_without_the_picture_extension() -> None:
    assert not plan_extension_policy(owner_lock(False), hygiene_score=0.30).has_changes                 # not the owner's lock
    assert not plan_extension_policy(owner_lock(False), owner_lock=True).has_changes                    # unknown Trust
    assert not plan_extension_policy(owner_lock(False, extra=()), owner_lock=True, hygiene_score=0.30).has_changes   # consent revoked


def _guard(w, owner_lock_id, score):
    return ExtensionGuard(core=w.core, control=Control(), reader=w.reader, transport=w.transport, account_user_id="owner",
                          list_lock_ids=lambda _n: ([LOCK], []), notify=lambda *a: None, owner_lock_id=owner_lock_id, hygiene_score=score)


def test_the_guard_applies_it_to_the_owners_lock(tmp_path) -> None:
    w = World(tmp_path, Reader(owner_lock(False), owner_lock(True)))
    assert _guard(w, lambda _now: LOCK, lambda: 0.60).run(NOW) == []
    [(_lock, body)] = w.transport.calls
    assert next(e for e in body["extensions"] if e["slug"] == "temporary-opening")["config"]["requireVerificationPictureAfter"] is True


def test_unknown_or_other_owner_lock_or_unreadable_trust_changes_nothing(tmp_path) -> None:
    def boom(*_a):
        raise RuntimeError("down")
    for i, (owner, score) in enumerate(((None, lambda: 0.3), (lambda _now: "another-lock", lambda: 0.3), (boom, lambda: 0.3),
                                        (lambda _now: LOCK, boom), (lambda _now: LOCK, lambda: None))):
        (tmp_path / str(i)).mkdir()
        w = World(tmp_path / str(i), Reader(owner_lock(False)))
        assert _guard(w, owner, score).run(NOW) == [] and w.transport.calls == []
