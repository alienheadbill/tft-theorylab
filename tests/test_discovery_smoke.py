from dataclasses import dataclass
from pathlib import Path

from tftlab.cli import _LOW_SAMPLE_COMMITMENT_GAMES, _is_low_sample
from tftlab.analytics import discover_candidates
from tftlab.storage import Database

from _helpers import make_match, make_unit


@dataclass
class _FakeCandidate:
    commitment_games: int


def test_is_low_sample_threshold() -> None:
    assert _is_low_sample(_FakeCandidate(commitment_games=1)) is True
    assert _is_low_sample(_FakeCandidate(commitment_games=_LOW_SAMPLE_COMMITMENT_GAMES - 1)) is True
    assert _is_low_sample(_FakeCandidate(commitment_games=_LOW_SAMPLE_COMMITMENT_GAMES)) is False
    assert _is_low_sample(_FakeCandidate(commitment_games=1000)) is False


def test_discovery_smoke_handles_tiny_dataset_without_crashing(tmp_path: Path) -> None:
    """A carry with only a couple of commitment games (and therefore no
    partner/item/trait evidence at all) must not crash discover_candidates
    or leave any field in a broken state -- it should just come back
    low-sample with empty evidence lists."""
    with Database(tmp_path / "tiny.sqlite3") as db:
        db.ingest_match(make_match("ONLY_ONE", units=[make_unit("TFT14_Rare", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])]))
        db.ingest_match(make_match("ONLY_TWO", placement=8, units=[make_unit("TFT14_Rare", tier=2, items=["TFT_Item_BlueBuff", "TFT_Item_Deathcap"])]))

        candidates = discover_candidates(db, min_cost=1, max_cost=3, min_samples=1)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert _is_low_sample(candidate) is True
    assert candidate.commitment_games == 2
    # No partners/items/traits ever co-occurred; evidence lists are simply
    # empty, not None or an error.
    assert candidate.best_partners == []
    assert candidate.best_trait_breakpoints == []
    assert isinstance(candidate.opportunity_score, float)
    assert 0.0 <= candidate.opportunity_score <= 100.0
