from scripts import build_nc_dps_public_enrichment_v3 as v3


def test_institutional_names_are_not_heirs():
    assert v3.heir_flag_strict("A & E REAL ESTATES LLC", []) is False
    assert v3.heir_flag_strict("HRS HOLDING CO", []) is False
    assert v3.heir_flag_strict("GF REAL ESTATE HOLDINGS LLC", []) is False


def test_real_heir_abbreviations_remain_heirs():
    assert v3.heir_flag_strict("UNKNOWN HEIRS OF FREDDIE SOWELL", []) is True
    assert v3.heir_flag_strict("PARKER LARRY HRS", []) is True
    assert v3.heir_flag_strict("ESTATE OF JOHN DOE", []) is True


def test_arcgis_epoch_date_is_normalized():
    assert v3._arcgis_date_text(946684800000) == "2000-01-01"
