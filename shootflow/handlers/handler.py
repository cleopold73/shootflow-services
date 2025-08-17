# python
# file: generic_handler.py
import importlib
import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


def _load_target(entry: str) -> Callable[[Dict[str, Any], Any], Any]:
    """
    Load target callable from 'module.submodule:function'.
    """
    if ":" not in entry:
        raise ValueError("LAMBDA_TARGET must be in the form 'module.path:function'")
    mod_name, func_name = entry.split(":", 1)
    mod = importlib.import_module(mod_name)
    try:
        func = getattr(mod, func_name)
    except AttributeError as e:
        raise ValueError(f"Function '{func_name}' not found in module '{mod_name}'") from e
    if not callable(func):
        raise TypeError(f"Target '{entry}' is not callable")
    return func


def _json_maybe_twice(s: str) -> Any:
    """
    Try to json.loads once; if it yields a string, try again.
    """
    try:
        obj = json.loads(s)
        if isinstance(obj, str):
            try:
                return json.loads(obj)
            except Exception:
                return obj
        return obj
    except Exception:
        return s


def _extract_s3_records_from_payload(payload: Any) -> List[Dict[str, Any]]:
    """
    Accepts either a direct S3 event (dict with Records) or an SNS envelope containing Message.
    Returns a list of S3 records (possibly empty).
    """
    if isinstance(payload, dict):
        # SNS envelope
        if isinstance(payload.get("Message"), str):
            inner = _json_maybe_twice(payload["Message"])
            if isinstance(inner, dict) and isinstance(inner.get("Records"), list):
                return list(inner["Records"])
        # Direct S3
        if isinstance(payload.get("Records"), list):
            return list(payload["Records"])
    return []


def _adapter_passthrough(target: Callable, event: Dict[str, Any], context: Any) -> Any:
    return target(event, context)


def _adapter_sqs(target: Callable, event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Pass each SQS message body (JSON-decoded when possible) to target as its event.
    If any message processing raises, mark that message as failed.
    Return partial batch failure response.
    """
    failures: List[Dict[str, str]] = []
    for rec in event.get("Records", []) or []:
        msg_id = rec.get("messageId") or rec.get("messageID")
        body = rec.get("body", "")
        payload = _json_maybe_twice(body)
        try:
            target(payload, context)
        except Exception:
            logger.exception("Target failed for SQS messageId=%s", msg_id)
            if msg_id:
                failures.append({"itemIdentifier": msg_id})
    return {"batchItemFailures": failures}


def _adapter_sqs_s3(target: Callable, event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    For each SQS message, extract S3 records from the body and invoke target once per S3 record.
    If any S3 record in a message fails, the whole SQS message is marked failed for retry.
    """
    failures: List[Dict[str, str]] = []

    for rec in event.get("Records", []) or []:
        msg_id = rec.get("messageId") or rec.get("messageID")
        body = rec.get("body", "")
        payload = _json_maybe_twice(body)

        s3_records = _extract_s3_records_from_payload(payload)
        if not s3_records:
            # No S3 content: consider the message handled
            continue

        msg_failed = False
        for s3r in s3_records:
            try:
                # Call target with a single-record S3 event shape for convenience
                target({"Records": [s3r]}, context)
            except Exception:
                logger.exception("Target failed for S3 record in SQS messageId=%s", msg_id)
                msg_failed = True

        if msg_failed and msg_id:
            failures.append({"itemIdentifier": msg_id})

    return {"batchItemFailures": failures}


def _adapter_sns(target: Callable, event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Invoke target for each SNS record's Message (JSON-decoded when possible).
    Returns a simple summary; SNS has no partial-batch response contract like SQS.
    """
    processed = 0
    errors = 0
    for rec in event.get("Records", []) or []:
        if rec.get("EventSource") == "aws:sns" and "Sns" in rec:
            msg = rec["Sns"].get("Message", "")
            payload = _json_maybe_twice(msg)
            try:
                target(payload, context)
                processed += 1
            except Exception:
                logger.exception("Target failed for SNS messageId=%s", rec["Sns"].get("MessageId"))
                errors += 1
    return {"processed": processed, "errors": errors}


_ADAPTERS = {
    "passthrough": _adapter_passthrough,
    "sqs": _adapter_sqs,
    "sqs_s3": _adapter_sqs_s3,
    "sns": _adapter_sns,
}


def handler(event: Dict[str, Any], context: Any) -> Any:
    """
    Generic Lambda entrypoint. Configure via env:
      - LAMBDA_TARGET='module.path:function'
      - LAMBDA_ADAPTER in {passthrough, sqs, sqs_s3, sns} (default: passthrough)

    The adapter shapes input to the target and formats the output as required by the event source.
    """
    target_entry = os.getenv("LAMBDA_TARGET")
    if not target_entry:
        raise RuntimeError("LAMBDA_TARGET is required (format 'module.path:function')")

    adapter_name = os.getenv("LAMBDA_ADAPTER", "passthrough").strip().lower()
    adapter = _ADAPTERS.get(adapter_name)
    if not adapter:
        raise RuntimeError(f"Unsupported LAMBDA_ADAPTER='{adapter_name}'")

    target = _load_target(target_entry)
    logger.info("Dispatching with adapter=%s to target=%s", adapter_name, target_entry)
    return adapter(target, event, context)
