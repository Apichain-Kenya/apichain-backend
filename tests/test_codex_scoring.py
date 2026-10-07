"""The rule-based Codex/KEBS conformance scorer (P3-B, 10 §5).

Pure: no DB, no network, no clock, no model file. This is what replaces v1's
ML authenticity decision, and the properties tested here are the ones that
make it defensible where the ML score was not:

- **Per-parameter, never blended.** `02` §4 is explicit that one 0-to-1 number
  hid which question failed. There is no such number in the output, and a test
  asserts the type has no field to put one in.
- **Missing data is `incomplete`, never `pass`.** v1 turned absence into a
  confident answer.
- **The rule set is versioned and frozen**, because a verdict is anchored: an
  old verdict must stay reproducible after the limits change.

Thresholds are quoted from CODEX STAN 12-1981 (adopted 1981, revisions 1987
and 2001), read from the primary text — not from the superseded FAO page that
`02` [G6] links, whose numbers differ.
"""

from dataclasses import fields
from decimal import Decimal

import pytest

from app.enums import ConformanceVerdict
from app.services import codex_scoring
from app.services.canonical import compute_data_hash

# A panel that passes every parameter, comfortably inside each limit.
_CLEAN = {
    "moisture_pct": Decimal("18.4"),
    "fructose_glucose_g_100g": Decimal("72.10"),
    "sucrose_g_100g": Decimal("2.30"),
    "hmf_mg_kg": Decimal("21.0"),
    "diastase_schade": Decimal("14.2"),
    "free_acidity_meq_kg": Decimal("28.0"),
}

# (parameter, field, limit, comparator) — the six rules of codex-kenya-v1.
_RULES = [
    ("moisture", "moisture_pct", Decimal("20.0"), "<="),
    ("fructose_glucose", "fructose_glucose_g_100g", Decimal("60.0"), ">="),
    ("sucrose", "sucrose_g_100g", Decimal("5.0"), "<="),
    ("hmf", "hmf_mg_kg", Decimal("80.0"), "<="),
    ("diastase", "diastase_schade", Decimal("8.0"), ">="),
    ("free_acidity", "free_acidity_meq_kg", Decimal("50.0"), "<="),
]


def _panel(**overrides):
    return codex_scoring.LabMeasurements(**{**_CLEAN, **overrides})


def _result(report, parameter):
    return next(p for p in report.parameters if p.parameter == parameter)


def test_a_clean_panel_passes_every_parameter():
    report = codex_scoring.evaluate(_panel())

    assert report.verdict is ConformanceVerdict.PASS
    assert len(report.parameters) == 6
    assert all(p.status == "pass" for p in report.parameters)


def test_the_rule_set_version_is_recorded_on_every_report():
    assert codex_scoring.evaluate(_panel()).rule_set_version == "codex-kenya-v1"


@pytest.mark.parametrize(("parameter", "field", "limit", "comparator"), _RULES)
def test_a_value_exactly_at_the_limit_passes(parameter, field, limit, comparator):
    """The standard says 'not more than' / 'not less than' — the limit is inclusive."""
    result = _result(codex_scoring.evaluate(_panel(**{field: limit})), parameter)

    assert result.status == "pass"
    assert result.limit == limit
    assert result.comparator == comparator


@pytest.mark.parametrize(("parameter", "field", "limit", "comparator"), _RULES)
def test_a_value_just_inside_the_limit_passes(parameter, field, limit, comparator):
    inside = limit - Decimal("0.1") if comparator == "<=" else limit + Decimal("0.1")

    assert _result(codex_scoring.evaluate(_panel(**{field: inside})), parameter).status == "pass"


@pytest.mark.parametrize(("parameter", "field", "limit", "comparator"), _RULES)
def test_a_value_just_outside_the_limit_fails_only_that_parameter(
    parameter, field, limit, comparator
):
    outside = limit + Decimal("0.1") if comparator == "<=" else limit - Decimal("0.1")

    report = codex_scoring.evaluate(_panel(**{field: outside}))

    assert report.verdict is ConformanceVerdict.FAIL
    assert _result(report, parameter).status == "fail"
    others = [p for p in report.parameters if p.parameter != parameter]
    assert [p.status for p in others] == ["pass"] * 5, "one bad parameter tainted the others"


def test_hmf_uses_the_tropical_limit_of_80_not_the_general_40():
    """Kenyan honey is tropical (Codex Annex 1.3). 40 would flag honest honey."""
    report = codex_scoring.evaluate(_panel(hmf_mg_kg=Decimal("55.0")))

    assert report.verdict is ConformanceVerdict.PASS
    assert _result(report, "hmf").limit == Decimal("80.0")


def test_a_missing_parameter_is_incomplete_and_never_pass():
    report = codex_scoring.evaluate(_panel(diastase_schade=None))

    assert report.verdict is ConformanceVerdict.INCOMPLETE
    assert _result(report, "diastase").status == "not_measured"
    assert _result(report, "diastase").measured is None


def test_a_failing_parameter_beats_a_missing_one():
    """A panel that both fails and is incomplete is a fail: the honey is out."""
    report = codex_scoring.evaluate(_panel(sucrose_g_100g=Decimal("9.0"), diastase_schade=None))

    assert report.verdict is ConformanceVerdict.FAIL


def test_an_empty_panel_reports_six_unmeasured_parameters():
    report = codex_scoring.evaluate(codex_scoring.LabMeasurements())

    assert report.verdict is ConformanceVerdict.INCOMPLETE
    assert [p.status for p in report.parameters] == ["not_measured"] * 6


def test_every_parameter_declares_its_unit_and_codex_basis():
    """A consumer reading a verdict must not need the rule set to interpret it."""
    report = codex_scoring.evaluate(_panel())
    by_name = {p.parameter: p for p in report.parameters}

    assert by_name["moisture"].unit == "%"
    assert by_name["fructose_glucose"].unit == "g/100g"
    assert by_name["sucrose"].unit == "g/100g"
    assert by_name["hmf"].unit == "mg/kg"
    assert by_name["diastase"].unit == "Schade"
    assert by_name["free_acidity"].unit == "meq/kg"

    # Moisture and the sugars are in the body of the standard; HMF, diastase
    # and free acidity are in an Annex that describes itself as voluntary.
    assert by_name["moisture"].basis == "codex-mandatory"
    assert by_name["fructose_glucose"].basis == "codex-mandatory"
    assert by_name["sucrose"].basis == "codex-mandatory"
    assert by_name["hmf"].basis == "codex-annex"
    assert by_name["diastase"].basis == "codex-annex"
    assert by_name["free_acidity"].basis == "codex-annex"


def test_there_is_no_blended_score_anywhere_in_the_output():
    """02 §4: one number hid which question failed. The type has no room for one."""
    banned = {"score", "authenticity_score", "confidence", "confidence_score", "weight"}

    report_fields = {f.name for f in fields(codex_scoring.ConformanceReport)}
    parameter_fields = {f.name for f in fields(codex_scoring.ParameterResult)}

    assert banned & (report_fields | parameter_fields) == set()


def test_float_and_decimal_inputs_of_the_same_value_agree():
    """A lab form sends JSON numbers; a DB column may hand back Decimal."""
    as_decimal = codex_scoring.evaluate(_panel(moisture_pct=Decimal("19.5")))
    as_float = codex_scoring.evaluate(_panel(moisture_pct=19.5))

    assert as_float == as_decimal


def test_evaluating_under_a_different_rule_set_can_change_the_verdict():
    """The versioning property: same measurements, different registered rules."""
    panel = _panel(hmf_mg_kg=Decimal("55.0"))

    under_v1 = codex_scoring.evaluate(panel)
    under_general = codex_scoring.evaluate(panel, rule_set_version="codex-general-v1")

    assert under_v1.verdict is ConformanceVerdict.PASS
    assert under_general.verdict is ConformanceVerdict.FAIL
    assert under_general.rule_set_version == "codex-general-v1"


def test_an_old_verdict_stays_reproducible_after_a_newer_rule_set_exists():
    """Re-evaluating under the named version reproduces the anchored verdict."""
    panel = _panel(hmf_mg_kg=Decimal("55.0"))
    anchored = codex_scoring.evaluate(panel, rule_set_version="codex-kenya-v1")

    assert codex_scoring.evaluate(panel, rule_set_version="codex-kenya-v1") == anchored


def test_an_unknown_rule_set_is_refused_rather_than_silently_defaulted():
    with pytest.raises(KeyError):
        codex_scoring.evaluate(_panel(), rule_set_version="codex-does-not-exist")


def test_the_registered_rule_sets_are_frozen_against_mutation():
    """Thresholds live in code, not a config table, because verdicts are anchored."""
    rules = codex_scoring.RULES["codex-kenya-v1"]

    with pytest.raises((AttributeError, TypeError)):
        rules.parameters[0].limit = Decimal("999")  # type: ignore[misc]


# --- Golden vector ---------------------------------------------------------
# NEVER change these values. They pin the serialized shape of a verdict, and a
# verdict is anchored: if this test fails, every previously anchored verdict
# just became unreproducible. Same discipline as the canonical and Merkle
# golden vectors — fix the code, not the constant.
#
# The expected payload below was written before the implementation existed and
# the code was made to match it. Only `_GOLDEN_HASH` was captured afterwards
# (2026-08-06), because a keccak digest cannot be predicted by hand; the shape
# it commits to is the hand-written dict, which is the part worth pinning.
_GOLDEN_PANEL = {
    "moisture_pct": Decimal("19.20"),
    "fructose_glucose_g_100g": Decimal("68.40"),
    "sucrose_g_100g": Decimal("3.10"),
    "hmf_mg_kg": Decimal("62.50"),
    "diastase_schade": Decimal("9.60"),
    "free_acidity_meq_kg": Decimal("41.30"),
}
_GOLDEN_HASH = "48064291aa601c0b479b4eb6d0b719ebb42404480d4a06cf1f3ea654c1cb5b45"


def test_golden_vector_pins_the_anchored_verdict_payload():
    report = codex_scoring.evaluate(codex_scoring.LabMeasurements(**_GOLDEN_PANEL))
    payload = codex_scoring.as_payload(report)

    assert payload == {
        "rule_set_version": "codex-kenya-v1",
        "verdict": "PASS",
        "parameters": [
            {
                "parameter": "moisture",
                "measured": "19.20",
                "unit": "%",
                "comparator": "<=",
                "limit": "20.00",
                "basis": "codex-mandatory",
                "status": "pass",
            },
            {
                "parameter": "fructose_glucose",
                "measured": "68.40",
                "unit": "g/100g",
                "comparator": ">=",
                "limit": "60.00",
                "basis": "codex-mandatory",
                "status": "pass",
            },
            {
                "parameter": "sucrose",
                "measured": "3.10",
                "unit": "g/100g",
                "comparator": "<=",
                "limit": "5.00",
                "basis": "codex-mandatory",
                "status": "pass",
            },
            {
                "parameter": "hmf",
                "measured": "62.50",
                "unit": "mg/kg",
                "comparator": "<=",
                "limit": "80.00",
                "basis": "codex-annex",
                "status": "pass",
            },
            {
                "parameter": "diastase",
                "measured": "9.60",
                "unit": "Schade",
                "comparator": ">=",
                "limit": "8.00",
                "basis": "codex-annex",
                "status": "pass",
            },
            {
                "parameter": "free_acidity",
                "measured": "41.30",
                "unit": "meq/kg",
                "comparator": "<=",
                "limit": "50.00",
                "basis": "codex-annex",
                "status": "pass",
            },
        ],
    }
    assert compute_data_hash(payload).hex() == _GOLDEN_HASH


def test_the_payload_is_hash_stable_across_float_and_decimal_input():
    """The anchored hash must not depend on how the lab form typed its numbers."""
    from_decimal = codex_scoring.as_payload(
        codex_scoring.evaluate(codex_scoring.LabMeasurements(**_GOLDEN_PANEL))
    )
    from_float = codex_scoring.as_payload(
        codex_scoring.evaluate(
            codex_scoring.LabMeasurements(**{k: float(v) for k, v in _GOLDEN_PANEL.items()})
        )
    )

    assert compute_data_hash(from_float) == compute_data_hash(from_decimal)
