from common.endpoints import get_endpoint_url


def test_get_endpoint_url():
    app_name = "test_app"
    app_port = 8080
    interface = "/api/v1/resource/{id}"
    param = {"id": "123"}
    expected_url = "http://test_app:8080/api/v1/resource/123"
    assert get_endpoint_url(app_name, app_port, interface, param) == expected_url


def test_get_endpoint_url_with_multiple_params():
    app_name = "test_app"
    app_port = 8080
    interface = "/api/v1/resource/{id}/detail/{detail_id}"
    param = {"id": "123", "detail_id": "456"}
    expected_url = "http://test_app:8080/api/v1/resource/123/detail/456"
    assert get_endpoint_url(app_name, app_port, interface, param) == expected_url


def test_get_endpoint_url_with_no_params():
    app_name = "test_app"
    app_port = 8080
    interface = "/api/v1/resource"
    param = {}
    expected_url = "http://test_app:8080/api/v1/resource"
    assert get_endpoint_url(app_name, app_port, interface, param) == expected_url


def test_get_endpoint_url_with_special_characters():
    app_name = "test_app"
    app_port = 8080
    interface = "/api/v1/resource/{id}"
    param = {"id": "123&456"}
    expected_url = "http://test_app:8080/api/v1/resource/123&456"
    assert get_endpoint_url(app_name, app_port, interface, param) == expected_url


def test_get_endpoint_url_with_empty_interface():
    app_name = "test_app"
    app_port = 8080
    interface = ""
    param = {}
    expected_url = "http://test_app:8080"
    assert get_endpoint_url(app_name, app_port, interface, param) == expected_url
