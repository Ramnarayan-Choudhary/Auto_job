from autojob.apply import launcher


def test_send_confirmation_email_uses_profile_settings(monkeypatch, tmp_path):
    sent = {}
    recorded = {}

    class FakeEmailService:
        def send_email(self, to, subject, body, attachments=None):
            sent["to"] = to
            sent["subject"] = subject
            sent["body"] = body
            sent["attachments"] = attachments or []
            return "msg-1"

    monkeypatch.setattr(
        launcher.config,
        "load_profile",
        lambda: {
            "personal": {"email": "owner@example.com"},
            "automation": {
                "email_confirmation_enabled": True,
                "confirmation_email": "notify@example.com",
            },
        },
    )
    monkeypatch.setattr(launcher, "GmailAutomationService", FakeEmailService)
    monkeypatch.setattr(
        launcher,
        "_record_confirmation_email",
        lambda url, sent_at=None, error=None: recorded.update(
            {"url": url, "sent_at": sent_at, "error": error}
        ),
    )

    launcher._send_confirmation_email(
        {"url": "https://example.com/jobs/1", "title": "Engineer", "site": "Acme"},
        {"confidence": "high", "verification": "Thank you page visible"},
        tmp_path / "artifact.json",
    )

    assert sent["to"] == "notify@example.com"
    assert "Engineer" in sent["subject"]
    assert recorded["url"] == "https://example.com/jobs/1"
    assert recorded["error"] is None
