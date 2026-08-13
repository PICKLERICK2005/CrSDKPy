def test_import_and_version() -> None:
    import crsdkpy

    assert crsdkpy.__version__ == "0.0.1"
