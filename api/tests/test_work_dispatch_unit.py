from services.work_dispatch_service import estimate_task_minutes


RATES = {"PICKING": 30.0, "PACKING": 40.0}


def test_pack_note_estimates_scale_to_actual_order_count():
    assert estimate_task_minutes(
        {"task_type": "PICKING", "order_count": 50}, RATES
    ) == 30.0
    assert estimate_task_minutes(
        {"task_type": "PACKING", "order_count": 10}, RATES
    ) == 8.0


def test_receiving_estimate_weights_skus_units_and_complexity():
    normal = estimate_task_minutes(
        {
            "task_type": "RECEIVING",
            "sku_count": 10,
            "unit_count": 100,
            "complexity_level": 2,
        },
        RATES,
    )
    difficult = estimate_task_minutes(
        {
            "task_type": "RECEIVING",
            "sku_count": 10,
            "unit_count": 100,
            "complexity_level": 5,
        },
        RATES,
    )
    assert normal == 37.0
    assert difficult == 83.2
    assert difficult > normal * 2
