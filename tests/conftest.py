"""Shared pytest fixtures.

isolate_environment (autouse): make every test's result independent of the developer's ambient env
by pinning two things per test:

  1. FILE_STORAGE_DIR -> a fresh tmp_path, so pipeline idempotency markers (_processed/{pid}__{task}
     .json) and hosted files never leak across tests OR across pytest runs (857222/999001 used to
     stay "already processed" forever, so process() skipped before attaching and fake.files was empty).

  2. COPERNIQ_WEBHOOK_SECRET -> UNSET (env deleted + CONFIG.coperniq_webhook_secret=""), so the
     create-bom endpoint's verify_signature deterministically takes its documented no-secret path.
     Without this, a secret loaded from a local .env makes the UNSIGNED create-bom flow tests 401 —
     the suite's result depended on whether the secret was in the environment. A test that SPECIFICALLY
     proves signature enforcement (test_signature) re-sets the secret itself (setenv + reload) AFTER
     this fixture runs, so enforcement stays explicitly covered and is not neutralized.

CONFIG is a frozen dataclass captured at import (`CONFIG = Config.from_env()`), so its fields can't be
setattr'd and monkeypatching only the env var wouldn't change the already-frozen value. We swap the
whole CONFIG object (dataclasses.replace, preserving every other field) into each module that imported
it — INCLUDING app.main, where verify_signature reads CONFIG.coperniq_webhook_secret — and set/unset
the env vars for any in-test importlib.reload.

The tmp dir is created ONCE at fixture setup and NOT cleared between calls within a test — so a test
that fires the pipeline twice (test_idempotent) still shares the marker dir and its second call
returns "skipped". tmp_path is unique per test, so isolation across tests is automatic.
"""
from __future__ import annotations
import dataclasses

import pytest


@pytest.fixture(autouse=True)
def isolate_environment(tmp_path, monkeypatch):
    from app import config as _config
    import app.pipeline as _pipeline
    import app.hosting as _hosting
    import app.comment_trigger as _comment_trigger
    import app.main as _main

    store = tmp_path / "bom_files"
    store.mkdir()
    monkeypatch.setenv("FILE_STORAGE_DIR", str(store))
    # neutralize the webhook secret by default (deterministic no-secret path for the unsigned
    # create-bom flow tests); test_signature re-sets it after this fixture via setenv + reload.
    monkeypatch.delenv("COPERNIQ_WEBHOOK_SECRET", raising=False)
    new_cfg = dataclasses.replace(_config.CONFIG, file_storage_dir=str(store),
                                  coperniq_webhook_secret="")
    # every module that did `from .config import CONFIG` holds its own reference to the SAME frozen
    # object; rebind each so markers/hosted files land in `store` AND verify_signature (app.main) sees
    # an empty secret.
    for mod in (_config, _pipeline, _hosting, _comment_trigger, _main):
        if getattr(mod, "CONFIG", None) is not None:
            monkeypatch.setattr(mod, "CONFIG", new_cfg)
    yield
