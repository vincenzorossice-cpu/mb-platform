from unittest.mock import MagicMock

import httpx
import openai
import pytest

from orchestrator.core.openai_retry import call_with_rate_limit_retry, suggested_wait_seconds


def rate_limit(message="Rate limit reached. Please try again in 371ms."):
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))
    return openai.RateLimitError(message, response=response, body=None)


class TestSuggestedWait:
    def test_milliseconds_seconds_and_minutes_are_understood(self):
        assert suggested_wait_seconds("Please try again in 371ms.") == pytest.approx(0.871)
        assert suggested_wait_seconds("Please try again in 1.2s.") == pytest.approx(1.7)
        assert suggested_wait_seconds("Please try again in 6m0s.") == 20.0   # tetto

    def test_without_a_hint_a_default_is_used(self):
        assert suggested_wait_seconds("boom") == 2.0


class TestCallWithRateLimitRetry:
    def test_a_temporary_limit_is_waited_out(self):
        call = MagicMock(side_effect=[rate_limit(), "ok"])
        sleep = MagicMock()

        assert call_with_rate_limit_retry(call, sleep=sleep) == "ok"
        sleep.assert_called_once()
        assert sleep.call_args.args[0] == 5.0    # la suggerita (0,87 s) non basta con la finestra piena

    def test_waits_grow_with_each_attempt_up_to_the_cap(self):
        call = MagicMock(side_effect=rate_limit())
        sleep = MagicMock()

        with pytest.raises(openai.RateLimitError):
            call_with_rate_limit_retry(call, sleep=sleep, max_retries=5)

        assert [c.args[0] for c in sleep.call_args_list] == [5.0, 10.0, 15.0, 20.0, 20.0]

    def test_a_persistent_limit_is_raised_after_the_retries(self):
        call = MagicMock(side_effect=rate_limit())
        sleep = MagicMock()

        with pytest.raises(openai.RateLimitError):
            call_with_rate_limit_retry(call, sleep=sleep, max_retries=3)
        assert sleep.call_count == 3
        assert call.call_count == 4

    def test_exhausted_credit_is_never_retried(self):
        call = MagicMock(side_effect=rate_limit("You exceeded your current quota (insufficient_quota)."))
        sleep = MagicMock()

        with pytest.raises(openai.RateLimitError):
            call_with_rate_limit_retry(call, sleep=sleep)
        sleep.assert_not_called()
        assert call.call_count == 1

    def test_other_errors_are_not_retried(self):
        error = openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))
        call = MagicMock(side_effect=error)
        sleep = MagicMock()

        with pytest.raises(openai.APIConnectionError):
            call_with_rate_limit_retry(call, sleep=sleep)
        sleep.assert_not_called()
