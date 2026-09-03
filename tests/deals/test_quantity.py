from deals.quantity import lot_quantity, unit_price


def test_named_patterns_from_real_titles():
    assert lot_quantity("Lot of (30) Lenovo Thinkpads T460") == (30, "title")
    assert lot_quantity("Lot of 6 - Three Dell Monitors And Three Keyboards") == (6, "title")
    assert lot_quantity("One lot of 4 recliners") == (4, "title")
    assert lot_quantity("Pallet Lot of Approx 16 Flat Screen Televisions") == (16, "title")


def test_no_blind_integer_fallback():
    assert lot_quantity("2009 Ford F550 Regular Cab") == (1, "default")
    assert lot_quantity("UMF Medical 8678 Power Phlebotomy Chair") == (1, "default")
    assert lot_quantity(None) == (1, "default")


def test_description_window_is_second_source():
    assert lot_quantity("HP Servers", "Rack pull. Qty: 12 units, tested.") == (12, "description")


def test_thousands_separator():
    assert lot_quantity("Lot of 2,100 folding tables") == (2100, "title")


def test_unit_price():
    assert unit_price(300.0, 30) == 10.0
    assert unit_price(300.0, 0) == 300.0
    assert unit_price(None, 5) is None
