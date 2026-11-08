from pymodulith import hello


def test_hello() -> None:
    assert hello() == "Hello from pymodulith!"
