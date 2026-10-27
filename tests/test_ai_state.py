import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pct.tracker import Tracker


def seat(index, uid, **overrides):
    data = {"seat": index, "uid": uid, "hand": [1, 2, 102], "handKnown": True,
            "put": [4], "peng": [], "peng1": [], "gang0": [], "gang1": [],
            "chi": [], "lie": [], "meldsKnown": True}
    data.update(overrides)
    return data


def live(**overrides):
    data = {"me": 22, "uids": [11, 22], "tState": 3, "curPlayer": 1,
            "tableid": 123, "playNum": 5, "cardNext": 42, "choupai": 20,
            "seats": [seat(0, 11), seat(1, 22)]}
    data.update(overrides)
    return data


def state(snapshot):
    tracker = Tracker()
    tracker.feed(snapshot, [])
    return tracker.ai_state()


def test_snapshot_uses_uid_and_seat_and_excludes_inferred_or_hidden_data():
    own = seat(1, 22, hand=[1, 1, 102], peng=[3], peng1=[4],
               gang0=[5], chi=[2, 7, 10], put=[6])
    opponent = seat(0, 11, peng=[106], peng1=[101], gang0=[102],
                    gang1=[107], chi=[1, 2, 3], put=[9])
    snapshot = state(live(seats=[own, opponent]))
    assert snapshot["mySeat"] == snapshot["curPlayer"] == 1
    assert snapshot["me"]["hand"] == [1, 1, 102]
    assert snapshot["me"]["puts"] == [6]
    assert {m["kind"] for m in snapshot["me"]["melds"]} == {"peng", "wei", "ti", "chi"}
    assert {m["kind"] for m in snapshot["opp"]["visibleMelds"]} == {"peng", "pao", "chi"}
    assert snapshot["opp"]["puts"] == [9]
    assert not {"timeline", "timelineComplete", "bottom", "first", "myPuts"} & snapshot.keys()
    assert all("after" not in m for m in snapshot["opp"]["visibleMelds"])


def test_missing_identity_never_reuses_previous_seat():
    tracker = Tracker()
    tracker.feed(live(), [])
    assert tracker.ai_state()["mySeat"] == 1
    tracker.feed(live(me=None), [])
    result = tracker.ai_state()
    assert result["mySeat"] is None
    assert result["me"]["hand"] is None
    assert result["opp"]["puts"] is None


def test_inconsistent_seat_or_unknown_snapshot_is_not_fabricated():
    wrong = state(live(seats=[seat(0, 11), seat(1, 11)]))
    assert wrong["mySeat"] is None
    missing = state(live(seats=[seat(0, 11), seat(1, 22, handKnown=False, meldsKnown=False)]))
    assert missing["me"]["hand"] is None
    assert missing["me"]["melds"] is None
    assert missing["me"]["handKnown"] is False
    assert missing["me"]["meldsKnown"] is False


def test_invalid_or_incomplete_card_data_is_unknown():
    s = state(live(seats=[seat(0, 11, chi=[1, 2]),
                          seat(1, 22, hand=[1, 11], chi=[1, 2])]))
    assert s["me"]["hand"] is None
    assert s["me"]["melds"] is None
    assert s["opp"]["visibleMelds"] is None
    unknown = state(live(seats=[seat(0, 11), seat(1, 22, chi=[1, 4, 8])]))
    assert unknown["me"]["melds"] is None


def test_missing_put_array_is_unknown_not_empty():
    s = state(live(seats=[seat(0, 11, put=[], putKnown=False),
                          seat(1, 22, put=[], putKnown=True)]))
    assert s["opp"]["puts"] is None
    assert s["me"]["puts"] == []


def test_duplicate_seat_is_ambiguous():
    s = state(live(seats=[seat(0, 11), seat(1, 22), seat(1, 22)]))
    assert s["mySeat"] is None
    assert s["me"]["hand"] is None


def test_failed_own_chi_and_confirmed_opponent_peng():
    tracker = Tracker()
    before = live(seats=[seat(0, 11), seat(1, 22, hand=[1, 2, 3, 4])])
    tracker.feed(before, [])
    # 本人发起吃的请求，但服务端并未确认；对手随后碰到目标牌。
    attempt = {"s": "request", "e": "MJChi", "t": 1,
               "d": {"uid": 22, "mjchi": [1, 2, 3]}}
    peng = {"s": "emit", "e": "MJPeng", "t": 2,
            "d": {"uid": 11, "card": 3, "from": 1}}
    confirmed = live(seats=[seat(0, 11, peng=[3]), seat(1, 22, hand=[1, 2, 3, 4])])
    tracker.feed(confirmed, [attempt, peng])
    result = tracker.ai_state()
    assert result["me"]["hand"] == [1, 2, 3, 4]
    assert result["me"]["melds"] == []
    assert result["opp"]["visibleMelds"] == [{"kind": "peng", "cards": [3, 3, 3]}]
    assert [(m.kind, m.cards) for m in tracker.melds] == [("peng", [3, 3, 3])]


def test_unconfirmed_opponent_event_is_not_a_completed_meld():
    tracker = Tracker()
    before = live()
    tracker.feed(before, [])
    event = {"s": "emit", "e": "MJChi", "t": 1,
             "d": {"uid": 11, "mjchi": [1, 2, 3]}}
    tracker.feed(before, [event])
    assert not tracker.melds
    assert tracker.ai_state()["opp"]["visibleMelds"] == []
    # 若快照随后确认成牌，仍应补记，不依赖事件重发。
    tracker.feed(live(seats=[seat(0, 11, chi=[1, 2, 3]), seat(1, 22)]), [])
    assert [(m.kind, m.cards) for m in tracker.melds] == [("chi", [1, 2, 3])]


def test_public_opponent_melds_survive_unknown_hidden_melds():
    known = {"peng": True, "peng1": False, "gang0": False,
             "gang1": True, "chi": True, "lie": True}
    opponent = seat(0, 11, peng=[3], meldsKnown=False, knownMeldFields=known)
    result = state(live(seats=[opponent, seat(1, 22)]))
    assert result["opp"]["visibleMelds"] == [{"kind": "peng", "cards": [3, 3, 3]}]
    known["peng"] = False
    assert state(live(seats=[opponent, seat(1, 22)]))["opp"]["visibleMelds"] is None


def test_unconfirmed_peng_is_added_only_when_snapshot_confirms():
    tracker = Tracker()
    before = live()
    tracker.feed(before, [])
    event = {"s": "emit", "e": "MJPeng", "t": 1,
             "d": {"uid": 11, "card": 3, "from": 1}}
    tracker.feed(before, [event])
    assert not tracker.melds
    tracker.feed(live(seats=[seat(0, 11, peng=[3]), seat(1, 22)]), [])
    assert [(m.kind, m.cards) for m in tracker.melds] == [("peng", [3, 3, 3])]


if __name__ == "__main__":
    tests = [value for name, value in list(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print("%d AI state tests passed" % len(tests))
