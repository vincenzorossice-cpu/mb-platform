from unittest.mock import MagicMock

import httpx
import openai
import pytest

from orchestrator.core import openai_compat
from orchestrator.core.openai_compat import create_with_param_fallback


@pytest.fixture(autouse=True)
def _clean_memory():
    openai_compat.reset_known_rejections()
    yield
    openai_compat.reset_known_rejections()


def bad_request(message):
    response = httpx.Response(400, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))
    return openai.BadRequestError(message, response=response, body=None)


KWARGS = dict(model="gpt-6.1-sol", messages=[], temperature=0.1, max_tokens=100)


def test_a_normal_call_is_passed_through_unchanged():
    create = MagicMock(return_value="ok")

    assert create_with_param_fallback(create, KWARGS) == "ok"
    create.assert_called_once_with(**KWARGS)


def test_max_tokens_rejected_is_retried_as_max_completion_tokens():
    create = MagicMock(side_effect=[
        bad_request("Unsupported parameter: 'max_tokens' is not supported with this model. "
                    "Use 'max_completion_tokens' instead."),
        "ok",
    ])

    assert create_with_param_fallback(create, KWARGS) == "ok"
    second = create.call_args_list[1].kwargs
    assert second["max_completion_tokens"] == 100 and "max_tokens" not in second


def test_temperature_rejected_is_dropped():
    create = MagicMock(side_effect=[
        bad_request("Unsupported value: 'temperature' does not support 0.1 with this model."), "ok",
    ])

    assert create_with_param_fallback(create, KWARGS) == "ok"
    assert "temperature" not in create.call_args_list[1].kwargs


def test_the_choice_is_remembered_for_the_model():
    first = MagicMock(side_effect=[bad_request("Unsupported parameter: 'max_tokens'"), "ok"])
    create_with_param_fallback(first, KWARGS)

    second = MagicMock(return_value="ok")
    create_with_param_fallback(second, KWARGS)

    assert second.call_count == 1                       # nessun nuovo errore per arrivarci
    assert "max_completion_tokens" in second.call_args.kwargs


def test_another_model_is_not_affected():
    create_with_param_fallback(MagicMock(side_effect=[bad_request("Unsupported parameter: 'max_tokens'"), "ok"]), KWARGS)

    other = MagicMock(return_value="ok")
    create_with_param_fallback(other, dict(KWARGS, model="gpt-4o-mini"))

    assert "max_tokens" in other.call_args.kwargs


def test_other_bad_requests_are_raised():
    create = MagicMock(side_effect=bad_request("Invalid image"))

    with pytest.raises(openai.BadRequestError):
        create_with_param_fallback(create, KWARGS)
    assert create.call_count == 1


def test_other_errors_are_raised_untouched():
    error = openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))
    with pytest.raises(openai.APIConnectionError):
        create_with_param_fallback(MagicMock(side_effect=error), KWARGS)
