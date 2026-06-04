from app.resolvers.address_to_apn import classify_pin


def test_classify_one_match_high():
    apn, conf, reason = classify_pin([{"attributes": {"PIN": "11-54-0340-0151-02-5"}}])
    assert apn == "11-54-0340-0151-02-5" and conf == "high" and "intersect" in reason


def test_classify_no_match_no_guess():
    apn, conf, reason = classify_pin([])
    assert apn is None and conf is None and "no parcel" in reason


def test_classify_ambiguous_no_guess():
    apn, conf, reason = classify_pin([{"attributes": {"PIN": "A"}}, {"attributes": {"PIN": "B"}}])
    assert apn is None and conf is None and "ambiguous" in reason


def test_classify_missing_pin():
    apn, conf, reason = classify_pin([{"attributes": {}}])
    assert apn is None and "no PIN" in reason
