"""Python fixtures F-PY-01 … F-PY-14 (spec "Fixtures με γνωστή αλήθεια")."""

from __future__ import annotations

from dead_code_hunter.models import (
    Confidence,
    Contract,
    Coverage,
    Recommendation,
    ReferenceKind,
    Rule,
)

from .conftest import coverage_of, one, run_fixture
from .fixtures_repo import ACTIVE_SYMBOLS


def test_f_py_01_private_function_without_use_is_candidate(tmp_path):
    out = run_fixture(tmp_path, "F-PY-01")
    finding = one(out, "calculate_internal_margin")
    assert finding.recommendation is Recommendation.CANDIDATE
    assert finding.rule is Rule.R8
    assert finding.contract is Contract.NONE
    assert finding.reference is ReferenceKind.NONE
    assert finding.impact is not None and finding.impact.value == "LOW"


def test_f_py_02_unused_import_in_a_normal_module_is_candidate(tmp_path):
    out = run_fixture(tmp_path, "F-PY-02")
    finding = one(out, "json")
    assert finding.kind == "import"
    assert finding.confidence is Confidence.HIGH
    assert finding.recommendation is Recommendation.CANDIDATE
    assert finding.rule is Rule.R8


def test_f_py_03_reexport_used_elsewhere_is_retained(tmp_path):
    out = run_fixture(tmp_path, "F-PY-03")
    finding = one(out, "Order")
    assert finding.recommendation is Recommendation.RETAIN
    assert finding.rule is Rule.R3
    assert finding.contract is Contract.INTERNAL_SHARED
    assert any("pkg/main.py" in reason for reason in finding.reference_reasons)


def test_f_py_03b_reexport_without_consumer_is_review_not_candidate(tmp_path):
    out = run_fixture(tmp_path, "F-PY-03b")
    finding = one(out, "Report")
    assert finding.contract is Contract.INTERNAL_SHARED
    assert finding.recommendation is Recommendation.REVIEW_REQUIRED
    assert finding.rule is Rule.R5


def test_f_py_04_all_name_without_consumer_is_review(tmp_path):
    out = run_fixture(tmp_path, "F-PY-04")
    finding = one(out, "PaymentPlan")
    assert finding.contract is Contract.INTERNAL_SHARED
    assert finding.recommendation is Recommendation.REVIEW_REQUIRED
    assert any("__all__" in reason for reason in finding.contract_reasons)


def test_f_py_05_route_is_protected(tmp_path):
    out = run_fixture(tmp_path, "F-PY-05")
    finding = one(out, "list_orders")
    assert finding.contract is Contract.PUBLIC
    assert finding.recommendation is Recommendation.PROTECTED
    assert finding.rule is Rule.R2


def test_f_py_06_celery_task_is_retained(tmp_path):
    out = run_fixture(tmp_path, "F-PY-06")
    finding = one(out, "send_customer_invoice")
    assert finding.recommendation is Recommendation.RETAIN
    assert finding.rule is Rule.R3


def test_f_py_07_pytest_fixture_is_retained(tmp_path):
    out = run_fixture(tmp_path, "F-PY-07")
    finding = one(out, "db_session")
    assert finding.recommendation is Recommendation.RETAIN


def test_f_py_08_qt_slot_is_retained(tmp_path):
    out = run_fixture(tmp_path, "F-PY-08")
    finding = one(out, "on_btnSave_clicked")
    assert finding.recommendation is Recommendation.RETAIN


def test_f_py_09_getattr_dispatch_is_retained(tmp_path):
    out = run_fixture(tmp_path, "F-PY-09")
    finding = one(out, "legacy_handler")
    assert finding.recommendation is Recommendation.RETAIN
    assert any("getattr" in reason for reason in finding.reference_reasons)


def test_f_py_10_console_script_is_protected(tmp_path):
    out = run_fixture(tmp_path, "F-PY-10")
    finding = one(out, "run")
    assert finding.contract is Contract.PUBLIC
    assert finding.recommendation is Recommendation.PROTECTED


def test_f_py_11_ibkr_event_handler_is_retained(tmp_path):
    out = run_fixture(tmp_path, "F-PY-11")
    finding = one(out, "handle_order_status")
    assert finding.recommendation is Recommendation.RETAIN


def test_f_py_12_dead_function_in_billing_needs_review(tmp_path):
    out = run_fixture(tmp_path, "F-PY-12")
    finding = one(out, "legacy_fee_amount")
    assert finding.impact is not None and finding.impact.value == "CRITICAL"
    assert finding.recommendation is Recommendation.REVIEW_REQUIRED
    assert finding.rule is Rule.R6


def test_f_py_13_syntax_error_downgrades_to_partial_and_names_the_file(tmp_path):
    out = run_fixture(tmp_path, "F-PY-13")
    entry = coverage_of(out, "vulture")
    assert entry["coverage"] == Coverage.PARTIAL.value
    assert "app/broken.py" in entry["reason"]


def test_f_py_14_library_reexport_is_protected(tmp_path):
    out = run_fixture(tmp_path, "F-PY-14")
    finding = one(out, "Widget")
    assert finding.contract is Contract.PUBLIC
    assert finding.recommendation is Recommendation.PROTECTED


def test_no_active_python_symbol_is_a_candidate(tmp_path):
    for name, symbols in ACTIVE_SYMBOLS.items():
        if not name.startswith("F-PY"):
            continue
        out = run_fixture(tmp_path / name, name)
        for symbol in symbols:
            for finding in [f for f in out.findings if f.symbol == symbol]:
                assert finding.recommendation is not Recommendation.CANDIDATE, (
                    f"{name}: active symbol {symbol} reached CANDIDATE"
                )
