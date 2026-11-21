import pytest

from detector import ledger


@pytest.fixture(autouse=True)
def _isolate_private(tmp_path, monkeypatch):
    """No test may touch the real private/ledger.json or the real ntfy topics."""
    monkeypatch.setattr(ledger, "PRIVATE", tmp_path / "private")
    monkeypatch.setattr(ledger, "LEDGER_FILE", tmp_path / "private" / "ledger.json")
    for k in ("NTFY_TOPIC", "NTFY_REPLY_TOPIC", "NTFY_TOKEN", "NTFY_SERVER"):
        monkeypatch.delenv(k, raising=False)
