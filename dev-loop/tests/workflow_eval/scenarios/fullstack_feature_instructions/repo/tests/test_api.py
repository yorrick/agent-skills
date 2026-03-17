def test_health():
    from src.api.routes import app

    # basic smoke test
    assert app is not None
