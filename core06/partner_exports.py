"""Read-only exchange contracts for separately authorized PLM and ERP exporters."""
from collections import defaultdict
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from revision_gateway import AuthorityError, canonical, verify_envelope

Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
MinorAmount = Annotated[int, Field(ge=-10**13, le=10**13)]


class ExportRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AppliedLot(ExportRecord):
    schema_version: Literal["factory-1c-applied-lot/v1"]
    source_event_id: Identifier
    part_key: Identifier
    lot_id: Identifier
    recorded_on: date
    applied_revision: Identifier | None
    plm_release_event_id: Identifier | None


class OrderActual(ExportRecord):
    order_id: Identifier
    posting_date: date
    currency: Currency
    planned_minor: MinorAmount
    actual_minor: MinorAmount


class FinanceExport(ExportRecord):
    schema_version: Literal["factory-1c-order-actuals/v1"]
    batch_id: Identifier
    records: Annotated[list[OrderActual], Field(max_length=5_000)]


def correlate_lot(lot, envelope, pinned_plm_keys, drawing_sha256, cad_sha256, *, at=None):
    """Compare an ERP export received through a trusted channel with signed PLM evidence."""
    result = {"state": "NEEDS_REVIEW", "manufacturing_authorization": False}
    try:
        release = verify_envelope(envelope, pinned_plm_keys, at=at)
    except AuthorityError:
        return {**result, "reason": "UNTRUSTED_RELEASE"}
    if release["source_system"] != "PLM" or release["status"] != "RELEASED":
        return {**result, "reason": "RELEASE_NOT_CONFIRMED_IN_EXPORT"}
    if (release["part_key"], release["drawing_sha256"], release["cad_sha256"]) != (
        lot.part_key, drawing_sha256, cad_sha256,
    ):
        return {**result, "reason": "PART_OR_SOURCE_HASH_MISMATCH"}
    if not lot.applied_revision or not lot.plm_release_event_id:
        return {**result, "reason": "APPLIED_REVISION_MISSING"}
    if (release["revision"], release["release_event_id"]) != (
        lot.applied_revision, lot.plm_release_event_id,
    ):
        return {**result, "reason": "APPLIED_REVISION_MISMATCH"}
    return {**result, "state": "EXPORTS_MATCH", "lot_id": lot.lot_id,
            "erp_event_id": lot.source_event_id, "plm_event_id": release["release_event_id"]}


def finance_projection(export):
    """Build a monthly currency-separated input for the finance-only BI boundary."""
    totals = defaultdict(lambda: [0, 0])
    orders = set()
    for row in export.records:
        if row.order_id in orders:
            raise ValueError("DUPLICATE_ORDER_IN_EXPORT")
        orders.add(row.order_id)
        total = totals[row.posting_date.isoformat()[:7], row.currency]
        total[0] += row.planned_minor
        total[1] += row.actual_minor
    return [{"month": month, "currency": currency, "planned_minor": planned,
             "actual_minor": actual, "variance_minor": actual - planned}
            for (month, currency), (planned, actual) in sorted(totals.items())]


def preview_finance(raw):
    if len(raw) > 2_000_000:
        raise ValueError("FINANCE_EXPORT_TOO_LARGE")
    batch = FinanceExport.model_validate_json(raw)
    return canonical({"schema_version": "factory-finance-projection/v1",
                      "batch_id": batch.batch_id, "rows": finance_projection(batch)})
