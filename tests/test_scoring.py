import pytest

from orb.scoring import RuleScorer, Scorer


@pytest.fixture
def rs(cfg):
    return RuleScorer(cfg.scoring)


def feats(**kw):
    base = {"side": 1.0, "d": 0.1, "rv": 2.0, "r_idx": 0.002, "w": 0.4, "v": 0.02}
    return {**base, **kw}


def test_is_a_scorer(rs):
    assert isinstance(rs, Scorer)


def test_perfect_signal_scores_100(rs):
    e = rs.explain(feats())
    assert e == {
        "s_breakout": 30,
        "s_rel_volume": 30,
        "s_market": 15,
        "s_or_quality": 15,
        "s_volatility": 10,
    }
    assert rs.score(feats()) == 100


@pytest.mark.parametrize("d, pts", [(0.0, 0), (0.05, 15), (0.10, 30), (0.5, 30)])
def test_breakout_strength(rs, d, pts):
    assert rs.explain(feats(d=d))["s_breakout"] == pytest.approx(pts)


@pytest.mark.parametrize("rv, pts", [(0.5, 0), (1.0, 0), (1.5, 15), (2.0, 30), (5.0, 30)])
def test_relative_volume(rs, rv, pts):
    assert rs.explain(feats(rv=rv))["s_rel_volume"] == pytest.approx(pts)


@pytest.mark.parametrize(
    "side, r, pts",
    [
        (1, 0.0011, 15),
        (1, 0.001, 7.5),
        (1, 0.0, 7.5),
        (1, -0.001, 7.5),
        (1, -0.0011, 0),
        (-1, -0.0011, 15),
        (-1, -0.001, 7.5),
        (-1, 0.001, 7.5),
        (-1, 0.0011, 0),
    ],
)
def test_market_alignment_and_short_mirror(rs, side, r, pts):
    assert rs.explain(feats(side=float(side), r_idx=r))["s_market"] == pts


@pytest.mark.parametrize(
    "w, pts",
    [
        (0.05, 0),
        (0.10, 0),
        (0.15, 7.5),
        (0.20, 15),
        (0.40, 15),
        (0.60, 15),
        (0.80, 7.5),
        (1.00, 0),
        (1.2, 0),
    ],
)
def test_or_quality(rs, w, pts):
    assert rs.explain(feats(w=w))["s_or_quality"] == pytest.approx(pts)


@pytest.mark.parametrize(
    "v, pts",
    [(0.005, 0), (0.010, 0), (0.0125, 5), (0.015, 10), (0.04, 10), (0.05, 5), (0.06, 0), (0.08, 0)],
)
def test_volatility(rs, v, pts):
    assert rs.explain(feats(v=v))["s_volatility"] == pytest.approx(pts)


def test_score_bounded(rs):
    assert 0 <= rs.score(feats(d=-5, rv=-1, r_idx=-1, w=9, v=9)) == 0
    assert rs.score(feats(d=99, rv=99)) <= 100
