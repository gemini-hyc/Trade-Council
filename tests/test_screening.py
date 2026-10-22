from trade_council.engineering.evidence import EvidenceIndex
from trade_council.orchestration.screening import make_screening_result


def _add(index: EvidenceIndex, field: str, value: float) -> None:
    index.add(
        dataset_id="market.daily",
        path="market/daily",
        primary_key="600519.SH",
        event_date="2026-10-16",
        available_at="2026-10-16 15:00:00",
        field=field,
        value=value,
    )


def test_screening_positive_and_insufficient() -> None:
    index = EvidenceIndex("snap")
    _add(index, "adj_close", 110.0)
    _add(index, "ma20", 105.0)
    _add(index, "ma60", 100.0)
    _add(index, "rsi14", 60.0)
    assert make_screening_result(index).direction_bias == "positive"

    empty = EvidenceIndex("snap")
    result = make_screening_result(empty)
    assert result.direction_bias == "insufficient"
    assert result.trade_parameters_allowed is False

