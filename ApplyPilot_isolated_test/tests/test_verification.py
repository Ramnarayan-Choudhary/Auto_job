from applypilot.apply.verification import parse_result_block, verify_apply_result


def test_parse_result_block():
    parsed = parse_result_block(
        """
        RESULT_STATUS: APPLIED
        RESULT_CONFIDENCE: high
        RESULT_REASON: Submitted successfully
        RESULT_VERIFICATION: Thank you page visible
        RESULT_URL: https://example.com/thanks
        """
    )
    assert parsed["RESULT_STATUS"] == "APPLIED"
    assert parsed["RESULT_CONFIDENCE"] == "high"


def test_verify_apply_result_promotes_success_on_confirmation_text():
    result = verify_apply_result(
        final_text="""
        RESULT_STATUS: APPLIED
        RESULT_CONFIDENCE: medium
        RESULT_REASON: Submitted successfully
        RESULT_VERIFICATION: Thank you for applying page shown
        """,
        final_url="https://example.com/thank-you",
    )
    assert result.status == "applied"
    assert result.confidence == "high"


def test_verify_apply_result_handles_expired():
    result = verify_apply_result(
        final_text="""
        RESULT_STATUS: FAILED
        RESULT_REASON: This job is no longer accepting applications
        RESULT_VERIFICATION: Page says no longer accepting applications
        """,
        final_url="https://example.com/jobs/closed",
    )
    assert result.status == "expired"
