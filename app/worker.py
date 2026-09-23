import logging
import os
import signal
import shutil
import socket
import struct
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import Event
from uuid import uuid4

from sqlalchemy import or_, select

from app.config import settings
from app.classification_worker import process_classification
from app.review_analysis import process_review
from app.db import SessionLocal, dependency_status
from app.logging_config import configure_logging
from app.models import Document, Requirement, RequirementDocument

configure_logging()
logger = logging.getLogger(__name__)
stop_event = Event()
worker_id = str(uuid4())


class MalwareFound(Exception):
    pass


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_file(path: Path) -> None:
    with path.open("rb") as source:
        if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in source.read():
            raise MalwareFound
    if not settings.clamav_host:
        if settings.environment == "production":
            raise RuntimeError("ClamAV is required in production")
        return
    with socket.create_connection(
        (settings.clamav_host, settings.clamav_port), timeout=30
    ) as scanner, path.open("rb") as source:
        scanner.sendall(b"zINSTREAM\0")
        while chunk := source.read(1024 * 1024):
            scanner.sendall(struct.pack(">I", len(chunk)) + chunk)
        scanner.sendall(struct.pack(">I", 0))
        result = scanner.recv(4096)
    if b"FOUND" in result:
        raise MalwareFound
    if b"OK" not in result:
        raise RuntimeError("ClamAV scan did not complete")


def _finish(document_id, *, failure_code: str | None = None) -> None:
    now = datetime.now(UTC)
    with SessionLocal.begin() as db:
        document = db.get(Document, document_id)
        if document is None or document.status != "QUARANTINED":
            return
        document.locked_by = None
        document.locked_until = None
        if failure_code:
            document.status = "FAILED"
            document.failure_code = failure_code
            document.processed_at = now
            return
        document.status = "AVAILABLE"
        document.processed_at = now
        requirement_ids = db.scalars(select(RequirementDocument.requirement_id).where(
            RequirementDocument.document_id == document.id,
            RequirementDocument.requirement_id.is_not(None),
            RequirementDocument.excluded_at.is_(None),
        ))
        for requirement_id in requirement_ids:
            requirement = db.get(Requirement, requirement_id)
            if requirement and requirement.status == "PENDING":
                requirement.status = "RECEIVED"


def process_next_document() -> bool:
    now = datetime.now(UTC)
    with SessionLocal.begin() as db:
        document = db.scalar(
            select(Document)
            .where(
                Document.status == "QUARANTINED",
                or_(Document.next_attempt_at.is_(None), Document.next_attempt_at <= now),
                or_(Document.locked_until.is_(None), Document.locked_until <= now),
            )
            .order_by(Document.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if document is None:
            return False
        document.locked_by = worker_id
        document.locked_until = now + timedelta(minutes=2)
        document.attempts += 1
        document_id = document.id
        expected_hash = document.sha256
        storage_key = document.storage_key

    source = Path(settings.quarantine_path) / f"{document_id}.part"
    target = Path(settings.document_path) / storage_key
    try:
        if target.exists():
            if _hash_file(target) != expected_hash:
                raise RuntimeError("Stored document hash mismatch")
        else:
            if not source.exists():
                raise RuntimeError("Quarantined document is missing")
            _scan_file(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.part")
            shutil.copyfile(source, temporary)
            if _hash_file(temporary) != expected_hash:
                temporary.unlink(missing_ok=True)
                raise RuntimeError("Copied document hash mismatch")
            os.replace(temporary, target)
        source.unlink(missing_ok=True)
        _finish(document_id)
    except MalwareFound:
        _finish(document_id, failure_code="MALWARE_DETECTED")
    except Exception:
        logger.exception("Document processing failed", extra={"document_id": str(document_id)})
        with SessionLocal.begin() as db:
            document = db.get(Document, document_id)
            if document and document.status == "QUARANTINED":
                document.locked_by = None
                document.locked_until = None
                if document.attempts >= 3:
                    document.status = "FAILED"
                    document.failure_code = "PROCESSING_FAILED"
                    document.processed_at = datetime.now(UTC)
                else:
                    document.next_attempt_at = datetime.now(UTC) + timedelta(
                        seconds=2 ** document.attempts
                    )
    return True


def request_stop(signum, _frame) -> None:
    logger.info("Worker stop requested by signal %s", signum)
    stop_event.set()


def main() -> None:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("Worker started")

    while not stop_event.is_set():
        if process_next_document():
            continue
        if process_classification():
            continue
        if process_review():
            continue
        status = dependency_status()
        log = logger.info if all(value == "ok" for value in status.values()) else logger.error
        log("Worker dependency check", extra={"dependencies": status})
        stop_event.wait(settings.worker_poll_seconds)

    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
