import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import ValidationError
from sqlalchemy import or_, select

from app.classification_schemas import ClassificationOutput
from app.config import settings
from app.db import SessionLocal
from app.models import AIRun, Document


def process_classification() -> bool:
    now = datetime.now(UTC)
    lease = str(uuid4())
    with SessionLocal.begin() as db:
        run = db.scalar(select(AIRun).where(
            AIRun.purpose == "CLASSIFY", AIRun.status.in_(("QUEUED", "PROCESSING")),
            or_(AIRun.next_attempt_at.is_(None), AIRun.next_attempt_at <= now),
            or_(AIRun.locked_until.is_(None), AIRun.locked_until <= now),
        ).order_by(AIRun.created_at).with_for_update(skip_locked=True).limit(1))
        if run is None:
            return False
        ids = [UUID(item["document_id"]) for item in run.input_snapshot["documents"]]
        docs = {doc.id: doc for doc in db.scalars(select(Document).where(Document.id.in_(ids), Document.firm_id == run.firm_id))}
        if any(doc.status == "QUARANTINED" for doc in docs.values()):
            run.next_attempt_at = now + timedelta(seconds=2)
            return False
        run.status = "PROCESSING"
        run.attempts += 1
        run.locked_by = lease
        run.locked_until = now + timedelta(seconds=210)
        run_id, snapshot = run.id, run.input_snapshot
        documents = [{"document_id": str(doc.id), "storage_key": doc.storage_key, "content_type": doc.content_type, "sha256": doc.sha256, "original_name": doc.original_name} for id in ids if (doc := docs.get(id)) and doc.status == "AVAILABLE"]
        available_ids = {item["document_id"] for item in documents}
        invalid = [{"document_id": str(id), "category": "INVALID", "requirement_id": None, "document_type": None, "confidence": 0} for id in ids if str(id) not in available_ids]
    error, result = None, None
    try:
        if snapshot["provider"] == "MANUAL" or not documents:
            result = {"schema_version": "1", "run_id": str(run_id), "model_version": "manual", "classifications": [{"document_id": doc["document_id"], "category": "OTHER", "requirement_id": None, "document_type": None, "confidence": 0} for doc in documents]}
        else:
            body = {"schema_version": "1", "run_id": str(run_id), "purpose": "CLASSIFY", "documents": documents, "requirements": snapshot["requirements"]}
            request = Request(settings.agent_url + "/v1/analyze", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Idempotency-Key": f"{run_id}:0"})
            with urlopen(request, timeout=180) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("Response too large")
                result = json.loads(raw)
        output = ClassificationOutput.model_validate(result)
        targets = {item["id"] for item in snapshot["requirements"]}
        if output.run_id != run_id or len(output.classifications) != len(available_ids) or {str(row.document_id) for row in output.classifications} != available_ids:
            raise ValueError("Mismatched response")
        if any(row.requirement_id and str(row.requirement_id) not in targets for row in output.classifications):
            raise ValueError("Unknown target")
        result = output.model_dump(mode="json")
        by_id = {item["document_id"]: item for item in result["classifications"] + invalid}
        result["classifications"] = [by_id[str(id)] for id in ids]
    except HTTPError as exc:
        error = "AGENT_UNAVAILABLE" if exc.code >= 500 else "AGENT_INVALID_RESPONSE"
    except (URLError, TimeoutError, OSError):
        error = "AGENT_UNAVAILABLE"
    except (ValueError, ValidationError):
        error = "AGENT_INVALID_RESPONSE"
    with SessionLocal.begin() as db:
        run = db.scalar(select(AIRun).where(AIRun.id == run_id).with_for_update())
        if run.status != "PROCESSING" or run.locked_by != lease:
            return True
        run.locked_by = run.locked_until = None
        run.error = error
        if error:
            run.status = "QUEUED" if error == "AGENT_UNAVAILABLE" and run.attempts < 3 else "FAILED"
            run.next_attempt_at = datetime.now(UTC) + timedelta(seconds=2 ** run.attempts)
        else:
            run.output = result
            run.model_version = result["model_version"]
            run.status = "SUCCEEDED"
        if run.status in ("FAILED", "SUCCEEDED"):
            run.finished_at = datetime.now(UTC)
    return True
