"""Rule-based Codex/KEBS conformance scoring (P3-B, 02 §4 and §7, 04 §5.9).

This replaces v1's ML authenticity decision. It is a transparent rule check
against measured lab values: no model, no training data, no blended score. Pure
— no DB, no network, no clock — so it cannot fail partway through a request and
can be tested exhaustively without fixtures.

**Why rules and not a model.** `02` found v1's regressors scored a negative
cross-validated R-squared on every target, meaning they predicted worse than
the training mean, and that the headline AUC came from a circular construction
on synthetic data. The Codex standard answers the quality-and-safety question
directly, needs no data the labs are not already producing, and can be shown to
a regulator. Origin authenticity is a different question and gets a different
module (`origin_verification`) when the real-data programme lands.

**Never one number.** `02` §4: "Do not let one blended 0-to-1 'authenticity'
number hide which question failed." The output is a list of per-parameter
results, each carrying its own measured value, limit, unit and outcome. There
is deliberately nowhere to put a score.

**Missing is not passing.** A parameter the lab did not report is
`not_measured` and drags the overall verdict to `INCOMPLETE`. Only a panel
where every parameter is measured and within limit is a `PASS`.

**Versioned and frozen, because verdicts are anchored.** The thresholds live
here as immutable module-level rule sets keyed by version, not in a config
table: a config row could be edited later and silently change the meaning of a
verdict already committed to the audit chain and anchored to Bitcoin. The
version string travels in the anchored payload, so any past verdict can be
reproduced by evaluating against the rule set it names.

**Source of the numbers.** CODEX STAN 12-1981, "Codex Standard for Honey",
adopted 1981, revisions 1987 and 2001, read from the primary text. Note that
the FAO page `02` [G6] links serves the *superseded* revision, whose limits
differ: it gives reducing sugars >=65% where the current standard specifies
fructose + glucose >=60 g/100g, free acidity <=40 where the current text says
<=50, and renders diastase as "not more than 3", which is inverted. Three of
the six limits below would have been wrong if taken from there.
"""

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal

from app.enums import ConformanceVerdict

Comparator = Literal["<=", ">="]
Basis = Literal["codex-mandatory", "codex-annex"]
Status = Literal["pass", "fail", "not_measured"]

# Measured values and limits are rendered to a fixed 2 decimal places in the
# anchored payload, so the hash does not depend on whether the lab form sent
# 19.2, 19.20 or Decimal("19.2"). Same purpose as v1's `_q4`.
_QUANTUM = Decimal("0.01")


def _q2(value: Decimal | float | int | None) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(_QUANTUM))


@dataclass(frozen=True, slots=True)
class Rule:
    """One Codex parameter and the limit it must satisfy."""

    parameter: str
    field: str
    comparator: Comparator
    limit: Decimal
    unit: str
    basis: Basis


@dataclass(frozen=True, slots=True)
class RuleSet:
    version: str
    parameters: tuple[Rule, ...]


@dataclass(frozen=True, slots=True)
class LabMeasurements:
    """A lab panel, one field per Codex parameter.

    Every field names the quantity *and* its unit. `02` R9 records what happens
    otherwise: v1 carried a field called `sucrose_level` that actually held
    total sugars (~75-80%), while the Codex sucrose limit is <=5 g/100g. A
    scorer fed that against the 5 limit would fail every honest batch.
    """

    moisture_pct: Decimal | float | None = None
    fructose_glucose_g_100g: Decimal | float | None = None
    sucrose_g_100g: Decimal | float | None = None
    hmf_mg_kg: Decimal | float | None = None
    diastase_schade: Decimal | float | None = None
    free_acidity_meq_kg: Decimal | float | None = None


@dataclass(frozen=True, slots=True)
class ParameterResult:
    parameter: str
    measured: Decimal | None
    unit: str
    comparator: Comparator
    limit: Decimal
    basis: Basis
    status: Status


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    rule_set_version: str
    verdict: ConformanceVerdict
    parameters: tuple[ParameterResult, ...]


# --- Rule sets -------------------------------------------------------------
# Frozen. Amending a limit means registering a NEW version, never editing one
# in place: verdicts under the old version are already anchored.

_KENYA_V1 = RuleSet(
    version="codex-kenya-v1",
    parameters=(
        # Body of the standard - mandatory.
        Rule("moisture", "moisture_pct", "<=", Decimal("20.0"), "%", "codex-mandatory"),
        Rule(
            "fructose_glucose",
            "fructose_glucose_g_100g",
            ">=",
            Decimal("60.0"),
            "g/100g",
            "codex-mandatory",
        ),
        Rule("sucrose", "sucrose_g_100g", "<=", Decimal("5.0"), "g/100g", "codex-mandatory"),
        # Annex - the standard describes it as voluntary for commercial
        # partners rather than for governments, so a failure here is a
        # commercial-quality nonconformity, not a regulatory one.
        #
        # HMF is the tropical limit. Annex 1.3 sets 40 mg/kg generally but
        # "in the case of honey of declared origin from countries or regions
        # with tropical ambient temperatures ... not more than 80 mg/kg".
        # Kenya qualifies; hardcoding 40 would flag legitimate Kenyan honey,
        # which is exactly the v1 failure mode this scorer replaces.
        Rule("hmf", "hmf_mg_kg", "<=", Decimal("80.0"), "mg/kg", "codex-annex"),
        # Annex 1.2 also allows >=3 for honeys of low natural enzyme content,
        # but conditions it on HMF <=15 mg/kg and a declaration this platform
        # does not yet collect. Omitted on purpose, not by oversight.
        Rule("diastase", "diastase_schade", ">=", Decimal("8.0"), "Schade", "codex-annex"),
        Rule(
            "free_acidity",
            "free_acidity_meq_kg",
            "<=",
            Decimal("50.0"),
            "meq/kg",
            "codex-annex",
        ),
    ),
)

# The general (non-tropical) reading of the same standard. Not used by any
# Kenyan batch; registered so the versioning mechanism is real rather than
# theoretical, and so a future non-tropical deployment has somewhere to go.
_GENERAL_V1 = RuleSet(
    version="codex-general-v1",
    parameters=tuple(
        Rule(r.parameter, r.field, r.comparator, Decimal("40.0"), r.unit, r.basis)
        if r.parameter == "hmf"
        else r
        for r in _KENYA_V1.parameters
    ),
)

RULES: MappingProxyType[str, RuleSet] = MappingProxyType(
    {rs.version: rs for rs in (_KENYA_V1, _GENERAL_V1)}
)

DEFAULT_RULE_SET = _KENYA_V1.version


def evaluate(
    measurements: LabMeasurements, *, rule_set_version: str = DEFAULT_RULE_SET
) -> ConformanceReport:
    """Check a lab panel against a named rule set. Pure and total.

    Raises `KeyError` for an unregistered version rather than falling back to
    the default: silently scoring against different limits than the caller
    asked for is how a verdict becomes uninterpretable.
    """
    rule_set = RULES[rule_set_version]

    results = tuple(_check(rule, getattr(measurements, rule.field)) for rule in rule_set.parameters)

    if any(r.status == "fail" for r in results):
        verdict = ConformanceVerdict.FAIL
    elif any(r.status == "not_measured" for r in results):
        verdict = ConformanceVerdict.INCOMPLETE
    else:
        verdict = ConformanceVerdict.PASS

    return ConformanceReport(rule_set.version, verdict, results)


def _check(rule: Rule, raw: Decimal | float | None) -> ParameterResult:
    if raw is None:
        status: Status = "not_measured"
        measured = None
    else:
        # Round to the reported precision before comparing, so a value that
        # renders as exactly the limit is not failed by float noise below the
        # second decimal place.
        measured = Decimal(str(raw)).quantize(_QUANTUM)
        within = measured <= rule.limit if rule.comparator == "<=" else measured >= rule.limit
        status = "pass" if within else "fail"

    return ParameterResult(
        parameter=rule.parameter,
        measured=measured,
        unit=rule.unit,
        comparator=rule.comparator,
        limit=rule.limit,
        basis=rule.basis,
        status=status,
    )


def as_payload(report: ConformanceReport) -> dict[str, Any]:
    """The verdict in the shape that gets hashed into the audit row.

    Numbers are fixed-precision strings, not floats, so the anchored hash is
    stable across float/Decimal round-trips. Parameter order follows the rule
    set and is never sorted — the order is part of the pinned format.
    """
    return {
        "rule_set_version": report.rule_set_version,
        "verdict": str(report.verdict),
        "parameters": [
            {
                "parameter": p.parameter,
                "measured": _q2(p.measured),
                "unit": p.unit,
                "comparator": p.comparator,
                "limit": _q2(p.limit),
                "basis": p.basis,
                "status": p.status,
            }
            for p in report.parameters
        ],
    }
