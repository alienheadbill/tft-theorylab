from pathlib import Path

from tftlab.analytics import carry_commitment_stats
from tftlab.demo import generate_demo_matches
from tftlab.items import completed_item_count
from tftlab.storage import Database


def test_components_do_not_count_as_completed_items():
    assert completed_item_count(["TFT_Item_BFSword", "TFT_Item_BlueBuff"]) == 1


def test_demo_pipeline_builds_commitment_stats(tmp_path: Path):
    db_path = tmp_path / "demo.sqlite3"
    with Database(db_path) as db:
        assert db.ingest_many(generate_demo_matches(30, seed=1)) == 30
        stats = carry_commitment_stats(db, min_samples=3)

    names = {s.name for s in stats}
    assert "Cassiopeia" in names
    assert "Kha'Zix" in names
    assert all(1 <= s.cost <= 3 for s in stats)
    assert all(0 <= s.top4_rate <= 1 for s in stats)
    assert all(0 <= s.opportunity_score <= 100 for s in stats)


def test_ingest_is_idempotent(tmp_path: Path):
    payload = generate_demo_matches(1)[0]
    with Database(tmp_path / "db.sqlite3") as db:
        assert db.ingest_match(payload) is True
        assert db.ingest_match(payload) is False
