"""Shared pytest fixtures.

isolate_file_storage (autouse): give every test its OWN FILE_STORAGE_DIR so pipeline idempotency
markers (_processed/{pid}__{task}.json) and hosted files can never leak across tests OR across
pytest runs — the root cause of the webhook-flow flakiness (857222/999001 stayed "already processed"
forever, so process() skipped before attaching and fake.files was empty).

CONFIG is a frozen dataclass captured at import (`CONFIG = Config.from_env()`), so its fields can't be
setattr'd and monkeypatching only the env var wouldn't change the already-frozen value. We therefore
swap the whole CONFIG object (dataclasses.replace, preserving every other field) into each module that
imported it, and also set the env var for any in-test importlib.reload.

The dir is created ONCE at fixture setup and NOT cleared between calls within a test — so a test that
fires the pipeline twice (test_idempotent) still shares the same marker dir and its second call
returns "skipped". tmp_path is unique per test, so isolation across tests is automatic.
"""
from __future__ import annotations
import dataclasses

import pytest


@pytest.fixture(autouse=True)
def isolate_file_storage(tmp_path, monkeypatch):
    from app import config as _config
    import app.pipeline as _pipeline
    import app.hosting as _hosting
    import app.comment_trigger as _comment_trigger

    store = tmp_path / "bom_files"
    store.mkdir()
    monkeypatch.setenv("FILE_STORAGE_DIR", str(store))
    new_cfg = dataclasses.replace(_config.CONFIG, file_storage_dir=str(store))
    # every module that did `from .config import CONFIG` holds its own reference to the SAME frozen
    # object; rebind each to the per-test config so markers + hosted files land in `store`.
    for mod in (_config, _pipeline, _hosting, _comment_trigger):
        if getattr(mod, "CONFIG", None) is not None:
            monkeypatch.setattr(mod, "CONFIG", new_cfg)
    yield
