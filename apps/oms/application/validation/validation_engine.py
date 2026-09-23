from __future__ import annotations

from apps.oms.application.models.parsed_order import ParsedOrder
from apps.oms.application.models.validated_order import ValidatedOrder
from apps.oms.application.models.validation_report import ValidationReport
from apps.oms.application.models.validation_warning import (
    ValidationWarning, WarningCode
)
from apps.oms.application.validators import (
    RequiredFieldValidator,
    PhoneValidator,
    WhatsAppValidator,
    AddressValidator,
    PackageValidator,
    PriceValidator,
    DeliveryValidator,
)
from apps.oms.events import dispatcher
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)

VALIDATION_VERSION = "1.0"


class ValidationEngine:
    '''
    Runs all validators against a ParsedOrder and produces a ValidatedOrder.

    Validators run independently — a crash in one never stops others.
    All errors and warnings from all validators are merged into one
    ValidationReport. Consistency observations are added by the engine
    after all validators complete.

    Usage:
        engine = ValidationEngine()
        validated = await engine.validate(parsed_order)
    '''

    def __init__(self):
        # Ordered validator pipeline
        self._validators = [
            RequiredFieldValidator(),
            PhoneValidator(),
            WhatsAppValidator(),
            AddressValidator(),
            PackageValidator(),
            PriceValidator(),
            DeliveryValidator(),
        ]

    async def validate(self, parsed_order: ParsedOrder) -> ValidatedOrder:
        '''
        Validate a ParsedOrder and return a ValidatedOrder.

        ParsedOrder is NEVER modified.
        All results live in the ValidationReport.

        Args:
            parsed_order: The ParsedOrder from Day 7 parser.

        Returns:
            ValidatedOrder containing original ParsedOrder + ValidationReport.
        '''
        log.debug(
            f"ValidationEngine: validating order {parsed_order.order_id!r}"
        )

        report = ValidationReport(validation_version=VALIDATION_VERSION)

        # ── Run each validator independently ──────────────────────
        for validator in self._validators:
            try:
                errors, warnings = validator.validate(parsed_order)
                for e in errors:
                    report.add_error(e)
                    log.debug(
                        f"  Error [{e.code.value}] on {e.field!r}: "
                        f"{e.description}"
                    )
                for w in warnings:
                    report.add_warning(w)
                    log.debug(
                        f"  Warning [{w.code.value}] on {w.field!r}: "
                        f"{w.description}"
                    )
            except Exception as exc:
                # Validator crashed — log and continue with others
                log.error(
                    f"  Validator {validator.__class__.__name__} crashed: {exc}",
                    exc_info=True
                )
                # Add a warning so downstream consumers know this validator failed
                report.add_warning(ValidationWarning(
                    code       =WarningCode.DELIVERY_MISSING,  # Generic placeholder
                    field      ="unknown",
                    description=(
                        f"Validator {validator.__class__.__name__} failed: {exc}"
                    ),
                ))

        # ── Consistency observations (engine-level, not a validator) ──
        self._check_consistency(parsed_order, report)

        # ── Compute final flags and quality score ──────────────────
        report.compute(parsed_order)

        # ── Build ValidatedOrder ───────────────────────────────────
        validated = ValidatedOrder(
            parsed_order=parsed_order,
            report      =report,
        )

        # ── Emit events ───────────────────────────────────────────
        if report.is_valid:
            event_name = "order.validated"
            log.info(
                f"✅ Validated: {parsed_order.order_id!r} | "
                f"quality={report.quality_score:.0%} | "
                f"warnings={len(report.warnings)}"
            )
        else:
            event_name = "order.validation_failed"
            log.warning(
                f"❌ Validation failed: {parsed_order.order_id!r} | "
                f"errors={len(report.errors)} | "
                f"quality={report.quality_score:.0%}"
            )

        await dispatcher.emit(
            event_name,
            validated_order=validated,
            order_id       =parsed_order.order_id,
            is_valid       =report.is_valid,
            error_codes    =report.error_codes(),
            warning_codes  =report.warning_codes(),
            quality_score  =report.quality_score,
            flags          =report.flag_values(),
        )

        if report.warnings:
            for w in report.warnings:
                await dispatcher.emit(
                    "order.validation_warning",
                    order_id  =parsed_order.order_id,
                    code      =w.code.value,
                    field     =w.field,
                    description=w.description,
                )

        return validated

    def _check_consistency(self, parsed_order: ParsedOrder, report: ValidationReport) -> None:
        '''
        Cross-field consistency observations.
        These are informational — they never produce errors.
        Different phone and WhatsApp numbers are both valid and common.
        '''
        phone = parsed_order.phone_number
        wa    = parsed_order.whatsapp_number

        if phone and wa and phone.strip() != wa.strip():
            report.add_warning(ValidationWarning(
                code       =WarningCode.PHONE_WHATSAPP_DIFFER,
                field      ="phone_number/whatsapp_number",
                description=(
                    f"Phone number ({phone!r}) differs from "
                    f"WhatsApp number ({wa!r}). "
                    "This is allowed — customer uses separate numbers."
                ),
            ))

        # Single-word name warning
        name = parsed_order.customer_name
        if name and len(name.strip().split()) < 2:
            report.add_warning(ValidationWarning(
                code       =WarningCode.NAME_SINGLE_WORD,
                field      ="customer_name",
                description=(
                    f"Customer name {name!r} is a single word. "
                    "Full name is preferred for identification."
                ),
            ))
