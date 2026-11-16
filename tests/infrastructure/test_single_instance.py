"""One bot per machine, and a stop request that only the holder of the secret can make (owner, 2026-11-16)."""

import threading

from infrastructure.single_instance import hold_single_instance, request_stop, serve_stop_requests


def test_a_second_instance_is_refused_and_the_port_frees_on_close() -> None:
    port = 18429
    first = hold_single_instance(port)
    assert first is not None
    assert hold_single_instance(port) is None
    first.close()
    again = hold_single_instance(port)
    assert again is not None
    again.close()


def test_only_the_secret_stops_the_bot(tmp_path) -> None:
    port, token_file = 18430, tmp_path / "token"
    sock = hold_single_instance(port)
    stopped = threading.Event()
    serve_stop_requests(sock, stopped.set, token_file=token_file)
    wrong = tmp_path / "wrong"
    wrong.write_text("not-the-secret", encoding="ascii")
    assert request_stop(port, token_file=wrong) is False and not stopped.is_set()
    assert request_stop(port, token_file=token_file) is True
    assert stopped.wait(2)
    sock.close()
    assert request_stop(port, token_file=token_file) is False         # nobody listening
