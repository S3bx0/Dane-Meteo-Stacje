import http.client
import json
import threading

import pytest

from dane_meteo_stacje.gui_bootstrap import (
    HTML_PAGE,
    MAX_REQUEST_BODY_BYTES,
    AppHandler,
    InvalidRequestBody,
    RequestBodyTooLarge,
    ThreadingHTTPServer,
    _parse_content_length,
    _validate_remote_url,
)


@pytest.fixture
def gui_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), AppHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_results_are_rendered_as_text_instead_of_html():
    assert "cell.textContent = value" in HTML_PAGE
    assert "tr.innerHTML" not in HTML_PAGE


def test_content_length_accepts_body_within_limit():
    assert _parse_content_length(str(MAX_REQUEST_BODY_BYTES)) == MAX_REQUEST_BODY_BYTES


def test_content_length_rejects_oversized_body():
    with pytest.raises(RequestBodyTooLarge):
        _parse_content_length(str(MAX_REQUEST_BODY_BYTES + 1))


@pytest.mark.parametrize("value", ["invalid", "-1"])
def test_content_length_rejects_invalid_values(value):
    with pytest.raises(InvalidRequestBody):
        _parse_content_length(value)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.ncei.noaa.gov/cdo-web/api/v2/stations",
        "https://ncei.noaa.gov/cdo-web/api/v2/stations",
    ],
)
def test_remote_url_accepts_noaa_https(url):
    assert _validate_remote_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://www.ncei.noaa.gov/cdo-web/api/v2/stations",
        "https://example.com/stations",
        "https://127.0.0.1/stations",
        "https://user:password@www.ncei.noaa.gov/stations",
    ],
)
def test_remote_url_rejects_unsafe_sources(url):
    with pytest.raises(ValueError):
        _validate_remote_url(url)


def test_search_endpoint_rejects_non_noaa_remote_url(gui_server):
    host, port = gui_server
    body = json.dumps({"remote_url": "https://example.com/stations"})
    connection = http.client.HTTPConnection(host, port)
    connection.request("POST", "/api/search", body=body, headers={"Content-Type": "application/json"})

    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()

    assert response.status == 400
    assert payload["code"] == "BAD_REQUEST"


def test_search_endpoint_rejects_oversized_body(gui_server):
    host, port = gui_server
    connection = http.client.HTTPConnection(host, port)
    connection.putrequest("POST", "/api/search")
    connection.putheader("Content-Type", "application/json")
    connection.putheader("Content-Length", str(MAX_REQUEST_BODY_BYTES + 1))
    connection.endheaders()

    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()

    assert response.status == 413
    assert payload["code"] == "PAYLOAD_TOO_LARGE"