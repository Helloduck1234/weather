import httpx
import pytest
import respx

from weatherbot.http import Http, HttpError

URL = "https://example.test/data"


def client():
    return Http(sleep=lambda _: None)


@respx.mock
def test_get_returns_json_and_sends_user_agent():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert client().get(URL, {"a": 1}) == {"ok": True}
    request = route.calls.last.request
    assert request.url.params["a"] == "1"
    assert request.headers["user-agent"].startswith("weatherbot/")


@respx.mock
def test_retries_server_errors_then_succeeds():
    route = respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, json=[1])])
    assert client().get(URL) == [1]
    assert route.call_count == 2


@respx.mock
def test_retries_rate_limit_honouring_retry_after():
    waits = []
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json=[1])]
    )
    Http(sleep=waits.append).get(URL)
    assert waits == [7.0]
    assert route.call_count == 2


@respx.mock
def test_gives_up_after_retries():
    route = respx.get(URL).mock(return_value=httpx.Response(500))
    with pytest.raises(HttpError) as err:
        client().get(URL)
    assert err.value.status == 500
    assert route.call_count == 4


@respx.mock
def test_client_error_is_not_retried():
    route = respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(HttpError) as err:
        client().get(URL)
    assert err.value.status == 404
    assert route.call_count == 1


@respx.mock
def test_transport_error_is_retried():
    route = respx.get(URL).mock(side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json=[1])])
    assert client().get(URL) == [1]
    assert route.call_count == 2


@respx.mock
def test_transport_error_after_retries_is_wrapped():
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(HttpError) as err:
        client().get(URL)
    assert err.value.status == 0


@respx.mock
def test_post_sends_json_body():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=[]))
    client().post(URL, [{"token_id": "1"}])
    assert route.calls.last.request.content == b'[{"token_id":"1"}]'


@respx.mock
def test_empty_reply_is_none():
    respx.post(URL).mock(return_value=httpx.Response(200))
    assert client().post(URL, {}) is None


@respx.mock
def test_reply_that_is_not_json_is_an_http_error():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>upstream is down</html>"))
    with pytest.raises(HttpError) as err:
        client().get(URL)
    assert err.value.status == 200
    assert "not JSON" in str(err.value)


@respx.mock
def test_retry_after_given_as_a_date_falls_back_to_backoff():
    waits = []
    respx.get(URL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        httpx.Response(200, json=[1]),
    ])
    Http(sleep=waits.append).get(URL)
    assert waits == [1.0]


@respx.mock
def test_long_retry_after_cannot_hold_up_the_run():
    waits = []
    respx.get(URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "3600"}), httpx.Response(200, json=[1])])
    Http(sleep=waits.append).get(URL)
    assert waits == [60.0]
