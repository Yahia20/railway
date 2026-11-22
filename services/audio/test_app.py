"""calls-audio server: the models live in a child process that idle time ends.
No models are loaded here: the child runs os.getpid, not the pipeline."""
import os
import time

import app


def _pid():
    return os.getpid()


def test_models_never_enter_the_server_process_and_idle_ends_the_child(monkeypatch):
    monkeypatch.setattr(app, "IDLE_UNLOAD_SECONDS", 0.0)
    try:
        child = app._child().submit(_pid).result(timeout=60)
        assert child != os.getpid()                            # the work ran in another process
        assert app.health()["models_loaded"] is True
        with app._one:
            assert app._unload() is True                       # what the idle watch does
        assert app._pool is None and app.health()["models_loaded"] is False
        again = app._child().submit(_pid).result(timeout=60)
        assert again not in (child, os.getpid())               # the next job starts a fresh child
    finally:
        with app._one:
            app._unload()


def test_a_job_resets_the_idle_clock_even_when_it_fails(monkeypatch):
    before = app._last_used
    time.sleep(0.01)
    monkeypatch.setattr(app, "_run_locked", lambda url: {"error": "x"})
    assert app._run("https://x") == {"error": "x"}
    assert app._last_used > before


def test_the_server_module_does_not_import_the_pipeline():
    src = open(app.__file__, encoding="utf-8").read()
    assert "\nimport pipeline" not in src
