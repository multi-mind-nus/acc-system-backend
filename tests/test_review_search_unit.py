from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from app.analysis_schemas import AmountRelation, Search
from app.review_analysis import amount_reconciled, amount_valid, file_reference, search_documents


def test_file_reference_carries_document_type_and_submission_round():
    doc = SimpleNamespace(id=uuid4(), storage_key="client/file.pdf", content_type="application/pdf",
                          document_type="OPEN_ITEMS_REGISTER", sha256="0" * 64, original_name="file.pdf")
    assert file_reference(doc, submission_round=2)["document_type"] == "OPEN_ITEMS_REGISTER"
    assert file_reference(doc, submission_round=2)["submission_round"] == 2


def test_unindexed_search_falls_back_within_authorized_scope():
    document = object()
    db = Mock()
    db.scalars.side_effect = [iter(()), iter((document,))]
    item = SimpleNamespace(id=uuid4(), firm_id=uuid4(), client_id=uuid4(), period=date(2026, 6, 1))
    search = Search(action="SEARCH_HISTORY", requirement_id=uuid4(), query="counterparty-not-indexed")

    assert search_documents(db, item, search, set()) == [document]
    assert db.scalars.call_count == 2


def test_correct_arithmetic_does_not_mean_amounts_reconcile():
    document_id = uuid4()
    relation = AmountRelation.model_validate({
        "currency": "SGD", "operation": "SUM",
        "operands": [{"document_id": str(document_id), "amount": "100.00", "label": "Invoice"}],
        "actual_amount": "100.00", "expected_amount": "90.00", "difference": "10.00",
    })
    assert amount_valid(relation)
    assert not amount_reconciled(relation)
