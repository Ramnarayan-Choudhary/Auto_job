from autojob.email import GmailAutomationService


def test_extract_verification_code(monkeypatch):
    service = GmailAutomationService()

    monkeypatch.setattr(
        service,
        "get_recent_emails",
        lambda keyword="", max_results=3, newer_than="10m": [
            {"body": "Your verification code is 482913. It expires soon."}
        ],
    )

    assert service.extract_verification_code("verification") == "482913"
