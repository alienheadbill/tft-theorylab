"""The patch calendar (tftlab.patch_calendar): Riot's published TFT patch
schedule -> conservative, frozen classification windows for the local
collector. Every test uses the saved fixture page; nothing contacts Riot
Support (tests/conftest.py blocks the module's only network call)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import tftlab.patch_calendar as pc
import tftlab.unreal_patch as up
from tftlab.balance_window import BALANCE_WINDOW_REGISTRY, resolve_balance_window
from tftlab.patch import patch_from_game_version
from tftlab.unreal_patch import UNRESOLVED_UNREAL_PATCH, UnrealPatchWindow, resolve_unreal_patch

FIXTURE = Path(__file__).parent / "fixtures" / "riot_tft_patch_schedule_2026-10-17.html"
HTML = FIXTURE.read_text(encoding="utf-8")
MASKED = "TFT Unreal Version ?.?.?.?"
FAKE_KEY = "RGAPI-0123abcd-4567-89ef-0123-456789abcdef"


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp() * 1000)


# The expected windows, written out by hand (UTC):
W18_5 = (_ms("2026-10-22T07:00:00+00:00"), _ms("2026-11-03T01:00:00+00:00"))  # PDT start, PST end
W18_6 = (_ms("2026-11-05T08:00:00+00:00"), _ms("2026-11-10T01:00:00+00:00"))  # ends before 18.6B
W19_1 = (_ms("2026-12-02T08:00:00+00:00"), _ms("2026-12-14T01:00:00+00:00"))
OCT_17 = _ms("2026-10-17T19:00:00+00:00")


def _getter(html: str = HTML, status: int = 200, ctype: str = "text/html; charset=utf-8", calls: list | None = None):
    def get(url: str):
        if calls is not None:
            calls.append(url)
        return status, ctype, html.encode("utf-8"), url
    return get


def _seed(local: Path, at_ms: int = OCT_17, html: str = HTML) -> pc.RefreshResult:
    result = pc.refresh_schedule(local, now_ms=at_ms, force=True, http_get=_getter(html))
    assert result.ok, result.message
    return result


def _move(html: str, label: str, new_date: str) -> str:
    """The fixture with one row's date changed (a schedule edit by Riot)."""
    start = html.index(f"<td>TFT {label}</td><td>") + len(f"<td>TFT {label}</td><td>")
    end = html.index("</td>", start)
    return html[:start] + new_date + html[end:]


def _files(local: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in local.iterdir() if p.is_file()}


# ---------------------------------------------------------------- 1-3: parsing


def test_parses_the_real_schedule_table() -> None:
    events = pc.parse_schedule_html(HTML)
    assert len(events) == 25
    assert [e.label for e in events[-8:]] == ["18.2", "18.3", "18.4", "18.5", "18.6", "18.6B", "19.1", "19.2"]
    by = {e.label: e for e in events}
    assert by["18.5"].scheduled_date == date(2026, 10, 21) and by["18.5"].kind == "patch"
    assert by["18.6"].scheduled_date == date(2026, 11, 4)
    assert by["19.1"].scheduled_date == date(2026, 12, 1) and by["19.2"].scheduled_date == date(2026, 12, 15)
    assert by["16.2"].raw_date == "January 8, 2026 (Thursday)" and not by["16.2"].ambiguous  # a real Thursday
    # The live page says "September 23, 2026 (Thursday)"; that date is a Wednesday. Both readings are kept.
    assert by["18.3"].ambiguous and (by["18.3"].earliest_date, by["18.3"].latest_date) == \
        (date(2026, 9, 23), date(2026, 9, 24))
    assert pc.events_fingerprint(events) == pc.events_fingerprint(pc.parse_schedule_html(HTML))  # deterministic


def test_b_events_are_a_base_patch_plus_a_balance_update() -> None:
    by = {e.label: e for e in pc.parse_schedule_html(HTML)}
    b = by["18.6B"]
    assert (b.base_patch, b.suffix, b.kind, b.scheduled_date) == ("18.6", "B", "balance", date(2026, 11, 11))
    # Generic suffix parsing, not a hard-coded label.
    for raw, label in [("TFT 20.3C", "20.3C"), ("TFT 20.3b", "20.3B"), ("TFT  7.10A", "7.10A")]:
        event = pc.parse_row(raw, "May 5, 2027")
        assert (event.label, event.kind, event.base_patch) == (label, "balance", label[:-1])
    # A balance update must belong to the patch just before it.
    rows = [("TFT 20.1", "January 6, 2027"), ("TFT 20.2", "January 20, 2027"), ("TFT 20.1B", "January 27, 2027")]
    with pytest.raises(pc.ScheduleError, match="does not follow patch 20.1"):
        pc.validate_events([pc.parse_row(*r) for r in rows])
    # Never a window, never a balance cutoff.
    assert "18.6B" not in [w.client_patch for w in pc.derive_windows(pc.parse_schedule_html(HTML))]
    assert [r.client_patch for r in BALANCE_WINDOW_REGISTRY] == ["18.2"]
    assert resolve_balance_window("18.6", W18_6[0] + 1) == "18.6"


MALFORMED = {
    "no table": "<html><body><p>Patch schedule coming soon</p></body></html>",
    "two tables": HTML.replace("</table>", "</table>" + HTML[HTML.index("<table"):HTML.index("</table>") + 8]),
    "another time zone": HTML.replace("Scheduled Date (Pacific Time)", "Scheduled Date (UTC)"),
    "a TBD date": _move(HTML, "19.1", "TBD"),
    "a delayed note": _move(HTML, "18.5", "October 21, 2026 (Delayed)"),
    "an extra column": HTML.replace("<td>TFT 18.5</td>", "<td>TFT 18.5</td><td>x</td>"),
    "dates out of order": _move(HTML, "18.6", "October 1, 2026"),
    "an impossible date": _move(HTML, "18.6", "February 30, 2026"),
    "a duplicate label": HTML.replace("<td>TFT 18.5</td>", "<td>TFT 18.4</td>"),
    "an unknown label": HTML.replace("<td>TFT 18.5</td>", "<td>Set 18 patch 5</td>"),
    "too few rows": "<table><tr><th>Patch</th><th>Scheduled Date (Pacific Time)</th></tr>"
                    "<tr><td>TFT 18.5</td><td>October 21, 2026</td></tr></table>",
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_unexpected_structure_is_rejected_not_guessed(case: str) -> None:
    with pytest.raises(pc.ScheduleError):
        pc.parse_schedule_html(MALFORMED[case])


def test_a_bad_page_keeps_the_old_cache_and_leaves_a_diagnostic(tmp_path: Path) -> None:
    _seed(tmp_path)
    cache_before = (tmp_path / pc.CACHE_NAME).read_bytes()
    result = pc.refresh_schedule(tmp_path, now_ms=OCT_17 + 7_200_000, http_get=_getter(MALFORMED["a TBD date"]))
    assert result.attempted and not result.ok and "TBD" in result.message
    assert (tmp_path / pc.CACHE_NAME).read_bytes() == cache_before
    check = json.loads((tmp_path / pc.CHECK_NAME).read_text())
    assert check["ok"] is False and check["kind"] == "unexpected_structure"
    assert (tmp_path / pc.REJECTED_NAME).exists()  # the page that failed, for diagnosis
    for status, ctype, url in [(500, "text/html", pc.SOURCE_URL), (200, "application/json", pc.SOURCE_URL),
                               (200, "text/html", "https://example.com/x")]:
        res = pc.refresh_schedule(tmp_path, now_ms=OCT_17 + 9e6, force=True,
                                  http_get=lambda u, s=status, c=ctype, f=url: (s, c, HTML.encode(), f))
        assert not res.ok
        assert (tmp_path / pc.CACHE_NAME).read_bytes() == cache_before
    # A good page clears the diagnostic.
    _seed(tmp_path, OCT_17 + 10_800_000)
    assert not (tmp_path / pc.REJECTED_NAME).exists()


def test_a_tampered_cache_is_refused(tmp_path: Path) -> None:
    _seed(tmp_path)
    data = json.loads((tmp_path / pc.CACHE_NAME).read_text())
    data["events"][-4]["scheduled_date"] = "2026-11-20"
    (tmp_path / pc.CACHE_NAME).write_text(json.dumps(data))
    with pytest.raises(pc.ScheduleError):
        pc.load_cache(tmp_path)
    st = pc.calendar_state(tmp_path, now_ms=W18_5[0] + 1)
    assert not st.ready and st.code == "no_schedule" and st.schedule_error


# ---------------------------------------------------------------- 4-7: the rule


def test_pacific_time_and_daylight_saving() -> None:
    # Midnight / 17:00 America/Los_Angeles, PDT (UTC-7) in summer, PST (UTC-8) in winter.
    assert pc.pacific_instant_ms(date(2026, 10, 22), 0) == _ms("2026-10-22T07:00:00+00:00")
    assert pc.pacific_instant_ms(date(2026, 10, 31), 17) == _ms("2026-11-01T00:00:00+00:00")
    assert pc.pacific_instant_ms(date(2026, 11, 1), 0) == _ms("2026-11-01T07:00:00+00:00")  # DST ends 02:00 that day
    assert pc.pacific_instant_ms(date(2026, 11, 2), 0) == _ms("2026-11-02T08:00:00+00:00")
    assert pc.pacific_instant_ms(date(2027, 3, 14), 0) == _ms("2027-03-14T08:00:00+00:00")  # DST starts 02:00
    assert pc.pacific_instant_ms(date(2027, 3, 14), 17) == _ms("2027-03-15T00:00:00+00:00")
    # 18.5 spans the November DST change: its length is not a whole number of days.
    start, end = pc.window_for(date(2026, 10, 21), date(2026, 11, 4))
    assert (start, end) == W18_5
    assert (end - start) % 86_400_000 == 3_600_000 * 18


def test_the_rule_reproduces_the_existing_windows() -> None:
    by = {w.client_patch: w for w in up.UNREAL_PATCH_REGISTRY}
    # Riot's dates for 18.2 / 18.3 / 18.4 / 18.5: Sep 10, Sep 23, Oct 7, Oct 21.
    assert pc.window_for(date(2026, 9, 23), date(2026, 10, 7)) == (by["18.3"].starts_at, by["18.3"].ends_at)
    assert pc.window_for(date(2026, 10, 7), date(2026, 10, 21)) == (by["18.4"].starts_at, by["18.4"].ends_at)
    start_18_2, end_18_2 = pc.window_for(date(2026, 9, 10), date(2026, 9, 23))
    assert end_18_2 == by["18.2"].ends_at
    assert start_18_2 - by["18.2"].starts_at == 7 * 3_600_000  # 18.2 started earlier: a fixed historical exception


def test_history_is_unchanged_and_never_regenerated(tmp_path: Path) -> None:
    assert [(w.client_patch, w.starts_at, w.ends_at, w.verified) for w in up.UNREAL_PATCH_REGISTRY] == [
        ("18.2", 1_789_084_800_000, 1_790_035_200_000, True),
        ("18.3", 1_790_233_200_000, 1_791_244_800_000, True),
        ("18.4", 1_791_442_800_000, 1_792_454_400_000, True),
    ]
    _seed(tmp_path)
    st = pc.calendar_state(tmp_path, now_ms=OCT_17)
    assert [w.client_patch for w in st.planned] == ["18.5", "18.6", "19.1"]  # nothing at or before 18.4
    assert st.ready and st.current_origin == "built-in" and st.current.client_patch == "18.4"
    # Nothing may re-define or overlap a fixed window.
    with pytest.raises(ValueError):
        up.install_frozen_windows([UnrealPatchWindow("18.4", W18_5[0], W18_5[1], True, pc.SOURCE_URL)])
    with pytest.raises(ValueError):
        up.install_frozen_windows([UnrealPatchWindow("18.5", 1_792_000_000_000, W18_5[1], True, pc.SOURCE_URL)])
    with pytest.raises(ValueError):
        up.install_frozen_windows([UnrealPatchWindow("18.5", W18_5[0], W18_5[1], False, pc.SOURCE_URL)])
    assert up.installed_windows() == ()
    for ts in (1_789_084_800_000, 1_790_233_200_000, 1_791_442_800_000, 1_792_454_399_999):
        assert resolve_unreal_patch(ts) == resolve_unreal_patch(ts, registry=up.UNREAL_PATCH_REGISTRY)


def test_windows_never_overlap_and_always_leave_a_gap() -> None:
    windows = pc.derive_windows(pc.parse_schedule_html(HTML))
    for before, after in zip(windows, windows[1:]):
        assert before.ends_at < after.starts_at
        assert after.starts_at - before.ends_at >= 54 * 3_600_000  # ~55 h; 54 h or 56 h across a DST change
    for w in windows:
        assert w.ends_at - w.starts_at >= pc.MIN_WINDOW_MS
    planned = {w.client_patch: (w.starts_at, w.ends_at) for w in windows}
    assert planned["18.5"] == W18_5 and planned["18.6"] == W18_6 and planned["19.1"] == W19_1
    assert "19.2" not in planned  # no successor listed: no end, so no window


def test_gaps_and_planned_windows_classify_nothing(tmp_path: Path) -> None:
    _seed(tmp_path, W18_5[0])
    inside_18_5 = W18_5[0] + 3_600_000
    # Planned (not yet used) windows are never used for classification.
    assert pc.calendar_state(tmp_path, now_ms=inside_18_5).current_origin == "planned"
    assert resolve_unreal_patch(inside_18_5) == UNRESOLVED_UNREAL_PATCH
    window, _st = pc.collection_window(tmp_path, now_ms=inside_18_5)
    assert window.client_patch == "18.5"
    assert resolve_unreal_patch(inside_18_5) == "18.5"
    for gap in (W18_5[0] - 1, _ms("2026-10-21T12:00:00+00:00"), 1_792_454_400_000, W18_5[1], W18_6[0] - 1,
                W18_6[0] + 1, _ms("2031-01-01T00:00:00+00:00")):
        assert resolve_unreal_patch(gap) == UNRESOLVED_UNREAL_PATCH  # 18.6 is planned, not frozen


# ---------------------------------------------------------------- 8-9: changes before and after freezing


def test_a_date_change_before_freezing_moves_the_planned_window(tmp_path: Path) -> None:
    _seed(tmp_path)
    assert pc.calendar_state(tmp_path, now_ms=OCT_17).planned[0].ends_at == W18_5[1]
    moved = _move(HTML, "18.6", "November 5, 2026 (Thursday)")
    result = pc.refresh_schedule(tmp_path, now_ms=OCT_17 + 7_200_000, http_get=_getter(moved))
    assert result.ok and result.changed
    st = pc.calendar_state(tmp_path, now_ms=OCT_17 + 7_200_000)
    assert st.planned[0].client_patch == "18.5"
    assert st.planned[0].ends_at == _ms("2026-11-04T01:00:00+00:00")  # 17:00 PST Nov 3
    assert not (tmp_path / pc.FROZEN_NAME).exists()


def test_a_frozen_window_never_moves(tmp_path: Path) -> None:
    _seed(tmp_path, W18_5[0] + 60_000)
    used_at = W18_5[0] + 3_600_000
    window, _st = pc.collection_window(tmp_path, now_ms=used_at)
    frozen = (tmp_path / pc.FROZEN_NAME).read_bytes()
    assert (window.starts_at, window.ends_at) == W18_5
    record = json.loads(frozen)["windows"][0]
    assert record["policy_version"] == pc.POLICY_VERSION and record["frozen_at"] == used_at
    assert record["until_label"] == "18.6" and record["events_sha256"].startswith("sha256:")
    # Riot delays 18.6 by two days: 18.5 keeps its frozen end; 18.6 starts later. The gap just grows.
    later = _move(HTML, "18.6", "November 6, 2026 (Friday)")
    _seed(tmp_path, used_at + 86_400_000, later)
    st = pc.calendar_state(tmp_path, now_ms=used_at + 86_400_000)
    assert st.ready and st.current_origin == "frozen" and (st.current.starts_at, st.current.ends_at) == W18_5
    assert any("frozen window is kept unchanged" in d for d in st.diagnostics)
    assert [w.client_patch for w in st.planned][0] == "18.6" and st.planned[0].starts_at == \
        _ms("2026-11-07T08:00:00+00:00")
    # Collecting again does not rewrite the record.
    pc.collection_window(tmp_path, now_ms=used_at + 86_400_000)
    assert (tmp_path / pc.FROZEN_NAME).read_bytes() == frozen
    # Riot moves 18.6 EARLIER than the frozen end allows: pause, never move history.
    earlier = _move(HTML, "18.6", "November 3, 2026 (Tuesday)")
    _seed(tmp_path, used_at + 2 * 86_400_000, earlier)
    st = pc.calendar_state(tmp_path, now_ms=used_at + 2 * 86_400_000)
    assert not st.ready and st.code == "schedule_conflict"
    assert pc.FOUND_NEXT_BUT_UNSAFE in st.message and "Collection is paused rather than mixing patches" in st.message
    with pytest.raises(pc.CalendarNotReady):
        pc.collection_window(tmp_path, now_ms=used_at + 2 * 86_400_000)
    assert (tmp_path / pc.FROZEN_NAME).read_bytes() == frozen
    assert resolve_unreal_patch(used_at) == "18.5"  # stored history keeps its classification


def _freeze_18_5(local: Path) -> bytes:
    _seed(local, W18_5[0] + 60_000)
    pc.collection_window(local, now_ms=W18_5[0] + 3_600_000)
    return (local / pc.FROZEN_NAME).read_bytes()


def test_freezing_writes_a_versioned_copy(tmp_path: Path) -> None:
    primary = _freeze_18_5(tmp_path)
    copies = pc.frozen_backups(tmp_path)
    assert len(copies) == 1 and copies[0].parent == tmp_path / "backups" / "patch-windows"
    assert copies[0].read_bytes() == primary
    # Collecting again in the same window adds no copy; a new frozen window adds one.
    pc.collection_window(tmp_path, now_ms=W18_5[0] + 7_200_000)
    assert len(pc.frozen_backups(tmp_path)) == 1
    _seed(tmp_path, W18_6[0] + 60_000)
    pc.collection_window(tmp_path, now_ms=W18_6[0] + 3_600_000)
    copies = pc.frozen_backups(tmp_path)
    assert len(copies) == 2 and copies[0].read_bytes() == (tmp_path / pc.FROZEN_NAME).read_bytes()
    assert [w["client_patch"] for w in json.loads(copies[0].read_text())["windows"]] == ["18.5", "18.6"]


@pytest.mark.parametrize("damage", ["deleted", "corrupt", "tampered"])
def test_a_lost_or_damaged_record_is_restored_without_rederiving_history(tmp_path: Path, damage: str) -> None:
    primary = _freeze_18_5(tmp_path)
    path = tmp_path / pc.FROZEN_NAME
    if damage == "deleted":
        path.unlink()
    elif damage == "corrupt":
        path.write_text("{not json")
    else:  # someone "fixed" the window by hand so that it overlaps 18.4
        data = json.loads(primary)
        data["windows"][0]["starts_at"] = 1_792_000_000_000
        path.write_text(json.dumps(data))
    # Riot then moves 18.6: re-deriving 18.5 now would give a different end.
    later = W18_5[0] + 86_400_000
    _seed(tmp_path, later, _move(HTML, "18.6", "November 6, 2026 (Friday)"))
    before = _files(tmp_path)
    st = pc.calendar_state(tmp_path, now_ms=later)
    assert _files(tmp_path) == before  # reading never writes
    assert st.ready and st.current_origin == "frozen" and (st.current.starts_at, st.current.ends_at) == W18_5
    assert any("using the copy" in d for d in st.diagnostics)
    window, st = pc.collection_window(tmp_path, now_ms=later)
    assert (window.starts_at, window.ends_at) == W18_5
    assert path.read_bytes() == primary  # restored exactly, not re-derived
    damaged = list(tmp_path.glob(pc.FROZEN_NAME + ".damaged-*"))
    assert len(damaged) == (0 if damage == "deleted" else 1)  # a damaged file is kept aside, never discarded


def test_a_valid_but_edited_record_is_not_blessed_as_new_history(tmp_path: Path) -> None:
    primary = _freeze_18_5(tmp_path)
    path = tmp_path / pc.FROZEN_NAME
    # A structurally valid, non-overlapping boundary edit: 18.5 "ends" an hour earlier.
    edited = json.loads(primary)
    edited["windows"][0]["ends_at"] = W18_5[1] - 3_600_000
    path.write_text(json.dumps(edited))
    pc._parse_frozen(path)  # it really is valid on its own
    # Riot then moves 18.6, so re-deriving 18.5 would also give something else.
    later = W18_5[0] + 86_400_000
    _seed(tmp_path, later, _move(HTML, "18.6", "November 6, 2026 (Friday)"))
    st = pc.calendar_state(tmp_path, now_ms=later)
    assert (st.current.starts_at, st.current.ends_at) == W18_5 and st.current_origin == "frozen"
    assert any("differs from its newest copy" in d for d in st.diagnostics)
    window, _st = pc.collection_window(tmp_path, now_ms=later)
    assert (window.starts_at, window.ends_at) == W18_5
    assert path.read_bytes() == primary  # the exact original, not re-backed-up and not re-derived
    copies = pc.frozen_backups(tmp_path)
    assert len(copies) == 1 and copies[0].read_bytes() == primary  # the edit never became a copy
    aside = list(tmp_path.glob(pc.FROZEN_NAME + ".damaged-*"))
    assert len(aside) == 1 and json.loads(aside[0].read_text()) == edited  # kept for inspection


def test_an_interrupted_freeze_is_completed_from_the_copy(tmp_path: Path) -> None:
    _freeze_18_5(tmp_path)
    only_18_5 = (tmp_path / pc.FROZEN_NAME).read_bytes()
    _seed(tmp_path, W18_6[0] + 60_000)
    pc.collection_window(tmp_path, now_ms=W18_6[0] + 3_600_000)  # freezes 18.6: copy first, then primary
    both = (tmp_path / pc.FROZEN_NAME).read_bytes()
    (tmp_path / pc.FROZEN_NAME).write_bytes(only_18_5)  # as if the primary write never happened
    st = pc.calendar_state(tmp_path, now_ms=W18_6[0] + 7_200_000)
    assert [f.client_patch for f in st.frozen] == ["18.5", "18.6"]
    pc.collection_window(tmp_path, now_ms=W18_6[0] + 7_200_000)
    assert (tmp_path / pc.FROZEN_NAME).read_bytes() == both
    assert len(pc.frozen_backups(tmp_path)) == 2


def test_a_valid_record_whose_copies_are_all_damaged_gets_a_fresh_copy(tmp_path: Path) -> None:
    primary = _freeze_18_5(tmp_path)
    for copy in pc.frozen_backups(tmp_path):
        copy.write_text("damaged")
    st = pc.calendar_state(tmp_path, now_ms=W18_5[0] + 7_200_000)
    assert st.ready and st.current_origin == "frozen"
    pc.collection_window(tmp_path, now_ms=W18_5[0] + 7_200_000)
    newest = pc.frozen_backups(tmp_path)[0]
    assert newest.read_bytes() == primary and (tmp_path / pc.FROZEN_NAME).read_bytes() == primary


def test_when_no_valid_copy_is_left_collection_pauses(tmp_path: Path) -> None:
    _freeze_18_5(tmp_path)
    (tmp_path / pc.FROZEN_NAME).write_text("{not json")
    for copy in pc.frozen_backups(tmp_path):
        copy.write_text("also damaged")
    st = pc.calendar_state(tmp_path, now_ms=W18_5[0] + 7_200_000)
    assert not st.ready and st.code == "frozen_unreadable" and "no valid copy" in st.message
    with pytest.raises(pc.CalendarNotReady):
        pc.collection_window(tmp_path, now_ms=W18_5[0] + 7_200_000)
    assert (tmp_path / pc.FROZEN_NAME).read_text() == "{not json"  # never overwritten


def test_stored_matches_from_a_lost_window_pause_collection(tmp_path: Path) -> None:
    _freeze_18_5(tmp_path)
    (tmp_path / pc.FROZEN_NAME).unlink()
    for copy in pc.frozen_backups(tmp_path):
        copy.unlink()
    st = pc.calendar_state(tmp_path, now_ms=W18_5[0] + 7_200_000)
    assert st.ready and st.current_origin == "planned"  # without evidence it would simply be planned again...
    st = pc.check_stored_patches(st, {"18.4", "18.5", UNRESOLVED_UNREAL_PATCH, MASKED})
    assert not st.ready and st.code == "frozen_missing" and "patch 18.5" in st.message  # ...but matches say otherwise
    with pytest.raises(pc.CalendarNotReady):
        pc.collection_window(tmp_path, now_ms=W18_5[0] + 7_200_000, stored_patches={"18.5"})
    assert not (tmp_path / pc.FROZEN_NAME).exists()
    # Fixed history, the unresolved sentinel and legacy masked strings are never "missing".
    ok = pc.check_stored_patches(pc.calendar_state(tmp_path, now_ms=OCT_17),
                                 {"18.2", "18.3", "18.4", UNRESOLVED_UNREAL_PATCH, MASKED})
    assert ok.ready


# ---------------------------------------------------------------- 10-13: cache, failures, special events


def test_a_failed_refresh_falls_back_to_the_validated_cache(tmp_path: Path) -> None:
    _seed(tmp_path)
    before = (tmp_path / pc.CACHE_NAME).read_bytes()
    result = pc.refresh_schedule(tmp_path, now_ms=OCT_17 + 2 * 3_600_000)  # the real getter: blocked in tests
    assert result.attempted and not result.ok and "could not reach Riot Support" in result.message
    assert (tmp_path / pc.CACHE_NAME).read_bytes() == before
    fresh = pc.calendar_state(tmp_path, now_ms=OCT_17 + 3 * 3_600_000).freshness()
    assert fresh["state"] == "cached_refresh_failed" and fresh["label"].startswith("cached — refresh failed")
    st = pc.calendar_state(tmp_path, now_ms=OCT_17 + 3 * 3_600_000)
    assert st.ready and st.current.client_patch == "18.4"


def test_no_cache_and_no_refresh_is_not_ready_once_a_new_patch_is_needed(tmp_path: Path) -> None:
    assert not pc.refresh_schedule(tmp_path, now_ms=W18_5[0] + 1).ok
    st = pc.calendar_state(tmp_path, now_ms=W18_5[0] + 1)
    assert not st.ready and st.code == "no_schedule"
    assert "could not read Riot's official patch schedule" in st.message
    assert "Collection is paused rather than mixing patches" in st.message
    assert st.freshness()["state"] == "failed"
    with pytest.raises(pc.CalendarNotReady):
        pc.collection_window(tmp_path, now_ms=W18_5[0] + 1)
    # The fixed 18.4 window never needed the schedule.
    st = pc.calendar_state(tmp_path, now_ms=OCT_17)
    assert st.ready and st.current.client_patch == "18.4"


def test_a_stale_cache_keeps_a_frozen_window_but_never_starts_a_new_one(tmp_path: Path) -> None:
    _seed(tmp_path, OCT_17)
    inside = W18_5[0] + 3 * 86_400_000  # 18.5, with a schedule last checked 8 days earlier
    st = pc.calendar_state(tmp_path, now_ms=inside)
    assert not st.ready and st.code == "schedule_too_old" and pc.FOUND_NEXT_BUT_UNSAFE in st.message
    assert st.freshness()["state"] == "old"
    with pytest.raises(pc.CalendarNotReady):
        pc.collection_window(tmp_path, now_ms=inside)
    assert not (tmp_path / pc.FROZEN_NAME).exists()
    # Fresh within 48 h of first use: frozen, and from then on the cache's age no longer matters.
    _seed(tmp_path, W18_5[0] + 60_000)
    pc.collection_window(tmp_path, now_ms=W18_5[0] + 3_600_000)
    st = pc.calendar_state(tmp_path, now_ms=inside)
    assert st.ready and st.current_origin == "frozen" and st.current.client_patch == "18.5"
    # ...but the next patch is not started from that stale copy.
    st = pc.calendar_state(tmp_path, now_ms=W18_6[0] + 1)
    assert not st.ready and st.code == "schedule_too_old"


def test_a_balance_update_pauses_collection_across_it(tmp_path: Path) -> None:
    _seed(tmp_path, W18_6[0] + 60_000)
    st = pc.calendar_state(tmp_path, now_ms=W18_6[0] + 3_600_000)
    assert st.ready and st.current.client_patch == "18.6"
    view = st.public()
    assert view["next"]["label"] == "18.6B" and view["next"]["kind"] == "balance"
    assert view["upcoming_balance"] == {"label": "18.6B", "base_patch": "18.6", "scheduled_date": "2026-11-11"}
    # 18.6 ends two days before the balance update...
    assert st.current.ends_at == W18_6[1] < pc.pacific_instant_ms(date(2026, 11, 11), 0)
    # ...and nothing is trusted again until 19.1, whatever the date.
    for when in (W18_6[1], _ms("2026-11-11T20:00:00+00:00"), _ms("2026-11-25T00:00:00+00:00"), W19_1[0] - 1):
        _seed(tmp_path, when)
        st = pc.calendar_state(tmp_path, now_ms=when)
        assert not st.ready and st.code == "balance_update", when
        assert "18.6B" in st.message and "Dec 2, 2026 00:00 Pacific" in st.message
        with pytest.raises(pc.CalendarNotReady):
            pc.collection_window(tmp_path, now_ms=when)
    _seed(tmp_path, W19_1[0])
    assert pc.collection_window(tmp_path, now_ms=W19_1[0])[0].client_patch == "19.1"


def test_the_18_4_to_18_5_transition(tmp_path: Path) -> None:
    _seed(tmp_path, OCT_17)
    st = pc.calendar_state(tmp_path, now_ms=OCT_17)
    view = st.public()
    assert view["current"]["patch"] == "18.4" and view["next"]["label"] == "18.5"
    assert view["next"]["scheduled_date"] == "2026-10-21" and view["next_window"]["client_patch"] == "18.5"
    for when in (1_792_454_400_000, _ms("2026-10-21T18:00:00+00:00"), W18_5[0] - 1):
        _seed(tmp_path, when)
        st = pc.calendar_state(tmp_path, now_ms=when)
        assert not st.ready and st.code == "between_patches"
        assert "starts trusting patch 18.5 at Oct 22, 2026 00:00 Pacific, automatically" in st.message
    _seed(tmp_path, W18_5[0])
    st = pc.calendar_state(tmp_path, now_ms=W18_5[0])
    assert st.ready and st.current.client_patch == "18.5" and st.current_origin == "planned"
    window, st = pc.collection_window(tmp_path, now_ms=W18_5[0])
    assert (window.starts_at, window.ends_at) == W18_5 and st.current_origin == "frozen"


def test_the_last_listed_patch_has_no_window(tmp_path: Path) -> None:
    when = _ms("2026-12-20T00:00:00+00:00")
    _seed(tmp_path, when)
    st = pc.calendar_state(tmp_path, now_ms=when)
    assert not st.ready and st.code == "no_successor" and "19.2" in st.message


# ---------------------------------------------------------------- 14: parseable versions


def test_parseable_versions_never_use_the_calendar(tmp_path: Path) -> None:
    _seed(tmp_path, W18_5[0])
    pc.collection_window(tmp_path, now_ms=W18_5[0] + 1)
    in_gap = _ms("2026-10-21T12:00:00+00:00")
    assert patch_from_game_version("Version 18.5.712.4410 (Oct 20 2026/14:00:00) [PUBLIC] <Releases/18.5>",
                                   in_gap) == "18.5"
    assert patch_from_game_version("TFT Unreal Version 18.6.1234", W18_5[0] + 1) == "18.6"
    assert patch_from_game_version(MASKED, W18_5[0] + 1) == "18.5"
    assert patch_from_game_version(MASKED, in_gap) == UNRESOLVED_UNREAL_PATCH


# ---------------------------------------------------------------- 16-17: network discipline, no secrets


def test_refreshes_are_throttled_and_status_reads_never_fetch(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(pc, "_http_get", _getter(calls=calls))
    assert pc.refresh_schedule(tmp_path, now_ms=OCT_17).ok
    assert not pc.refresh_schedule(tmp_path, now_ms=OCT_17 + 59 * 60_000).attempted
    for i in range(20):
        pc.calendar_state(tmp_path, now_ms=OCT_17 + i * 2000).public()
    assert calls == [pc.SOURCE_URL]
    assert pc.refresh_schedule(tmp_path, now_ms=OCT_17 + 61 * 60_000).attempted
    assert len(calls) == 2


class _FakeHttpx:
    """Stands in for httpx.Client: answers each requested URL from `routes`."""

    def __init__(self, routes: dict, requested: list, sent: dict):
        self.routes, self.requested, self.sent = routes, requested, sent

    def __call__(self, **kwargs):
        self.sent.update(kwargs)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def stream(self, method, url):
        self.requested.append((method, url))
        status, headers, body = self.routes[url]

        class Response:
            status_code = status

            def __init__(self):
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def iter_bytes(self):
                yield body
        return Response()


def _fake_httpx(monkeypatch, routes: dict) -> tuple[list, dict]:
    import httpx

    requested: list = []
    sent: dict = {}
    monkeypatch.setattr(httpx, "Client", _FakeHttpx(routes, requested, sent))
    return requested, sent


PAGE_OK = (200, {"content-type": "text/html"}, HTML.encode())


def test_redirects_are_checked_before_every_hop(monkeypatch, tmp_path: Path) -> None:
    moved = "https://support.riotgames.com/en-us/tft/events/patch-schedule/"
    requested, sent = _fake_httpx(monkeypatch, {pc.SOURCE_URL: (301, {"location": "/en-us/tft/events/patch-schedule/"}, b""),
                                                moved: PAGE_OK})
    status, _ctype, _body, final = pc.fetch_schedule_page(pc.SOURCE_URL)
    assert status == 200 and final == moved and sent["follow_redirects"] is False
    assert [u for _m, u in requested] == [pc.SOURCE_URL, moved]
    # An intermediate hop off Riot Support is refused before it is requested, even if it would come back.
    for away in ("https://example.com/bounce?to=riot", "http://support.riotgames.com/x",
                 "https://support.riotgames.com.evil.example/x", "https://user@support.riotgames.com/x",
                 "https://support.riotgames.com:8443/x"):
        requested, _sent = _fake_httpx(monkeypatch, {pc.SOURCE_URL: (302, {"location": away}, b""), away: PAGE_OK})
        with pytest.raises(pc.ScheduleError, match="redirected away"):
            pc.fetch_schedule_page(pc.SOURCE_URL)
        assert [u for _m, u in requested] == [pc.SOURCE_URL], away
    # A redirect loop stops.
    requested, _sent = _fake_httpx(monkeypatch, {pc.SOURCE_URL: (307, {"location": pc.SOURCE_URL}, b"")})
    with pytest.raises(pc.ScheduleError, match="too many"):
        pc.fetch_schedule_page(pc.SOURCE_URL)
    assert len(requested) == pc.MAX_REDIRECTS + 1
    # Through refresh: the old cache is kept and the reason recorded.
    _seed(tmp_path)
    cache = (tmp_path / pc.CACHE_NAME).read_bytes()
    _fake_httpx(monkeypatch, {pc.SOURCE_URL: (302, {"location": "https://example.com/"}, b"")})
    result = pc.refresh_schedule(tmp_path, now_ms=OCT_17 + 7_200_000, http_get=pc.fetch_schedule_page)
    assert not result.ok and "redirected away" in result.message
    assert (tmp_path / pc.CACHE_NAME).read_bytes() == cache


def test_the_request_carries_no_key_and_the_files_hold_no_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RIOT_API_KEY", FAKE_KEY)
    requested, sent = _fake_httpx(monkeypatch, {pc.SOURCE_URL: PAGE_OK})
    result = pc.refresh_schedule(tmp_path, now_ms=OCT_17, http_get=pc.fetch_schedule_page)  # the real getter
    assert result.ok
    assert requested == [("GET", pc.SOURCE_URL)] and sent["timeout"] == pc.HTTP_TIMEOUT_S
    assert sent["headers"]["User-Agent"].startswith("TheoryLabs-Collector/")
    assert FAKE_KEY not in json.dumps(sent["headers"]) and "X-Riot-Token" not in sent["headers"]
    _seed(tmp_path, W18_5[0])
    pc.collection_window(tmp_path, now_ms=W18_5[0] + 1)
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    blob = b"".join(p.read_bytes() for p in files) + json.dumps(
        pc.calendar_state(tmp_path, now_ms=W18_5[0] + 2).public()).encode()
    assert FAKE_KEY.encode() not in blob and b"RGAPI" not in blob and b"puuid" not in blob.lower()


def test_a_throttled_refresh_reports_the_last_outcome(tmp_path: Path) -> None:
    assert not pc.refresh_schedule(tmp_path, now_ms=OCT_17).ok  # blocked network: a failed check
    again = pc.refresh_schedule(tmp_path, now_ms=OCT_17 + 60_000)
    assert not again.attempted and not again.ok and "could not reach Riot Support" in again.message
    _seed(tmp_path, OCT_17 + 120_000)
    again = pc.refresh_schedule(tmp_path, now_ms=OCT_17 + 180_000)
    assert not again.attempted and again.ok and again.message == "checked recently"


def test_without_time_zone_data_no_new_window_is_created(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path, W18_5[0])
    pc.collection_window(tmp_path, now_ms=W18_5[0] + 1)  # 18.5 frozen while time-zone data was available

    def missing():
        raise LookupError("No time zone found with key America/Los_Angeles")

    monkeypatch.setattr(pc, "_pacific", missing)
    # Stored instants (fixed history, frozen windows) need no time-zone arithmetic...
    assert pc.calendar_state(tmp_path, now_ms=OCT_17).ready
    st = pc.calendar_state(tmp_path, now_ms=W18_5[0] + 3_600_000)
    assert st.ready and st.current_origin == "frozen" and "UTC" in st.message
    # ...a new window does, so none is created.
    st = pc.calendar_state(tmp_path, now_ms=W18_6[0] + 1)
    assert not st.ready and st.code == "no_timezone" and "pip install -e ." in st.message and not st.planned
    with pytest.raises(pc.CalendarNotReady):
        pc.collection_window(tmp_path, now_ms=W18_6[0] + 1)
