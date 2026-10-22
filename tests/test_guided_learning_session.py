from nexfiscal.portal.guided_learning_session import _safe_url


def test_safe_url_removes_credentials_query_and_fragment():
    assert _safe_url("https://user:secret@portal.example/login?token=abc#step") == (
        "https://portal.example/login"
    )


def test_safe_url_rejects_non_http_urls():
    assert _safe_url("file:///tmp/test") == ""
    assert _safe_url("not-a-url") == ""
