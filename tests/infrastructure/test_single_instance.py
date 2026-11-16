"""One bot per machine (owner, 2026-11-16)."""

from infrastructure.single_instance import hold_single_instance


def test_a_second_instance_is_refused_and_the_port_frees_on_close() -> None:
    port = 18429
    first = hold_single_instance(port)
    assert first is not None
    assert hold_single_instance(port) is None
    first.close()
    again = hold_single_instance(port)
    assert again is not None
    again.close()
