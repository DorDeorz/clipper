from clipper.render.tr_text import tr_casefold, tr_lower, tr_upper


def test_tr_upper() -> None:
    assert tr_upper("istanbul ılık") == "İSTANBUL ILIK"
    assert tr_upper("çğöşü") == "ÇĞÖŞÜ"


def test_tr_lower() -> None:
    assert tr_lower("İSTANBUL ILIK") == "istanbul ılık"


def test_casefold_mixes_i() -> None:
    assert tr_casefold("IYI") == tr_casefold("iyi") == tr_casefold("İyİ") == "iyi"
