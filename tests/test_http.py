import httpx
import pytest
import respx

from helpers import FakeClock, make_settings
from wbscraper.http import FetchError, HttpClient

URL = "https://api.example.test/data"


def client(tmp_path, clock, **over):
    settings = make_settings(tmp_path, domains={"api.example.test": {"interval": 4.0, "jitter": 0.0}}, **over)
    return HttpClient(settings, clock=clock.time, sleep=clock.sleep)


@respx.mock
def test_requests_to_the_same_domain_are_spaced(tmp_path):
    clock = FakeClock()
    http = client(tmp_path, clock)
    respx.get(URL).mock(return_value=httpx.Response(200, json={"ok": 1}))
    http.get_json(URL)
    t_first = clock.now
    http.get_json(URL)
    assert clock.now - t_first >= 4.0 - 1e-9
    assert http.request_count == 2


@respx.mock
def test_5xx_is_retried_then_succeeds(tmp_path):
    clock = FakeClock()
    http = client(tmp_path, clock)
    route = respx.get(URL).mock(side_effect=[httpx.Response(502), httpx.Response(503), httpx.Response(200, json={"a": 1})])
    assert http.get_json(URL) == {"a": 1}
    assert route.call_count == 3


@respx.mock
def test_gives_up_after_max_retries(tmp_path):
    clock = FakeClock()
    http = client(tmp_path, clock)
    respx.get(URL).mock(return_value=httpx.Response(502))
    with pytest.raises(FetchError) as e:
        http.get_json(URL)
    assert e.value.kind == "server"
    assert http.failed_count == 3  # 1 try + 2 retries


@respx.mock
def test_short_retry_after_is_honoured(tmp_path):
    clock = FakeClock()
    http = client(tmp_path, clock)
    respx.get(URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})])
    http.get_json(URL)
    assert clock.slept >= 7


@respx.mock
def test_long_retry_after_is_not_slept_inline_and_domain_cools_down(tmp_path):
    clock = FakeClock()
    http = client(tmp_path, clock)
    respx.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "900"}))
    with pytest.raises(FetchError) as e:
        http.get_json(URL)
    assert e.value.kind == "rate_limited" and e.value.retry_after == 900
    assert clock.slept < 100
    assert http.cooldown_remaining("api.example.test") > 600


@respx.mock
def test_forbidden_is_not_retried(tmp_path):
    clock = FakeClock()
    http = client(tmp_path, clock)
    route = respx.get(URL).mock(return_value=httpx.Response(403, text="blocked"))
    with pytest.raises(FetchError) as e:
        http.get_json(URL)
    assert e.value.kind == "forbidden" and route.call_count == 1


@respx.mock
def test_client_error_keeps_body_for_callers(tmp_path):
    http = client(tmp_path, FakeClock())
    respx.get(URL).mock(return_value=httpx.Response(422, json={"data": None, "error": "Timeout. Maybe slow down a bit"}))
    with pytest.raises(FetchError) as e:
        http.get_json(URL)
    assert e.value.kind == "client" and "Timeout" in e.value.body


@respx.mock
def test_repeated_failures_trigger_cooldown(tmp_path):
    clock = FakeClock()
    http = client(tmp_path, clock, http={"failure_threshold": 2, "cooldown_seconds": 300, "max_retries": 0})
    respx.get(URL).mock(return_value=httpx.Response(500))
    for _ in range(2):
        with pytest.raises(FetchError):
            http.get_json(URL)
    assert http.cooldown_remaining("api.example.test") > 250


@respx.mock
def test_timeout_is_retried(tmp_path):
    http = client(tmp_path, FakeClock())
    respx.get(URL).mock(side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={"x": 1})])
    assert http.get_json(URL) == {"x": 1}


@respx.mock
def test_invalid_json_raises_bad_json(tmp_path):
    http = client(tmp_path, FakeClock())
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(FetchError) as e:
        http.get_json(URL)
    assert e.value.kind == "bad_json"


@respx.mock
def test_oversized_response_is_rejected(tmp_path):
    http = client(tmp_path, FakeClock(), http={"max_response_bytes": 100, "max_retries": 0})
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"x" * 500))
    with pytest.raises(FetchError) as e:
        http.get(URL)
    assert e.value.kind == "too_large"
