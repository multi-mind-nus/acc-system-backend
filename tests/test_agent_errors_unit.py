from io import BytesIO
from urllib.error import HTTPError

import pytest

from app.agent_errors import agent_http_error
from app.collection_schemas import RequirementInput


@pytest.mark.parametrize("status,payload,expected", [
    (502, b'{"code":"MODEL_INVALID_RESPONSE"}', "AGENT_INVALID_RESPONSE"),
    (503, b'{"code":"MODEL_UNAVAILABLE"}', "AGENT_UNAVAILABLE"),
    (504, b'{"code":"MODEL_TIMEOUT"}', "AGENT_UNAVAILABLE"),
    (502, b'not json', "AGENT_UNAVAILABLE"),
    (422, b'{}', "AGENT_INVALID_RESPONSE"),
])
def test_agent_error_retry_classification(status, payload, expected):
    assert agent_http_error(HTTPError("http://agent", status, "error", {}, BytesIO(payload))) == expected


def test_bank_requirement_accepts_explicit_document_validation():
    requirement = RequirementInput(type="BANK_STATEMENT", title="Check holder and month",
                                   analysis_type="DOCUMENT_REQUIREMENT_VALIDATION")
    assert requirement.analysis_type == "DOCUMENT_REQUIREMENT_VALIDATION"
    assert RequirementInput(type="BANK_STATEMENT", title="Reconcile").analysis_type is None
