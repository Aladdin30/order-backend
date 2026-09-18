"""Unit tests for the dynamic branch financial calculation engine (Tax, Service Fee, Compounding)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.enums import OrderType
from app.services.order_service import OrderService


def test_simple_financial_calculation() -> None:
    """Verify simple addition: tax is computed on subtotal only, service on subtotal only."""
    subtotal = Decimal("100.00")
    order_type = OrderType.DINE_IN
    tax_rate = Decimal("0.1500")  # 15%
    service_fee_rate = Decimal("0.1000")  # 10%
    is_service_taxable = False
    service_fee_dine_in_only = True

    fin = OrderService.calculate_order_financials(
        subtotal=subtotal,
        order_type=order_type,
        tax_rate=tax_rate,
        service_fee_rate=service_fee_rate,
        is_service_taxable=is_service_taxable,
        service_fee_dine_in_only=service_fee_dine_in_only,
    )

    assert fin["subtotal"] == Decimal("100.00")
    assert fin["service_fee_total"] == Decimal("10.00")
    assert fin["applied_tax_rate"] == Decimal("0.1500")
    assert fin["tax_total"] == Decimal("15.00")
    assert fin["total_amount"] == Decimal("125.00")


def test_compound_financial_calculation_tax_over_service() -> None:
    """Verify compound calculation: tax is computed over (subtotal + service fee)."""
    subtotal = Decimal("100.00")
    order_type = OrderType.DINE_IN
    tax_rate = Decimal("0.1400")  # 14%
    service_fee_rate = Decimal("0.1200")  # 12%
    is_service_taxable = True  # Compound mode
    service_fee_dine_in_only = True

    fin = OrderService.calculate_order_financials(
        subtotal=subtotal,
        order_type=order_type,
        tax_rate=tax_rate,
        service_fee_rate=service_fee_rate,
        is_service_taxable=is_service_taxable,
        service_fee_dine_in_only=service_fee_dine_in_only,
    )

    # Service = 100 * 0.12 = 12.00
    # Taxable Base = 100 + 12 = 112.00
    # Tax = 112 * 0.14 = 15.68
    # Total = 100 + 12 + 15.68 = 127.68
    assert fin["subtotal"] == Decimal("100.00")
    assert fin["service_fee_total"] == Decimal("12.00")
    assert fin["tax_total"] == Decimal("15.68")
    assert fin["total_amount"] == Decimal("127.68")


def test_takeaway_service_fee_exemption() -> None:
    """Verify takeaway orders are automatically exempt from service charge when dine_in_only is True."""
    subtotal = Decimal("100.00")
    order_type = OrderType.TAKEAWAY
    tax_rate = Decimal("0.1500")
    service_fee_rate = Decimal("0.1200")
    is_service_taxable = True
    service_fee_dine_in_only = True

    fin = OrderService.calculate_order_financials(
        subtotal=subtotal,
        order_type=order_type,
        tax_rate=tax_rate,
        service_fee_rate=service_fee_rate,
        is_service_taxable=is_service_taxable,
        service_fee_dine_in_only=service_fee_dine_in_only,
    )

    assert fin["subtotal"] == Decimal("100.00")
    assert fin["service_fee_total"] == Decimal("0.00")  # Exempt
    assert fin["tax_total"] == Decimal("15.00")
    assert fin["total_amount"] == Decimal("115.00")


def test_fractional_cent_rounding_precision() -> None:
    """Verify Decimal ROUND_HALF_UP rounding precision with uneven quantities and odd prices."""
    subtotal = Decimal("33.33")
    order_type = OrderType.DINE_IN
    tax_rate = Decimal("0.1500")  # 15% of 33.33 = 4.9995 -> rounds to 5.00
    service_fee_rate = Decimal("0.0750")  # 7.5% of 33.33 = 2.49975 -> rounds to 2.50
    is_service_taxable = False

    fin = OrderService.calculate_order_financials(
        subtotal=subtotal,
        order_type=order_type,
        tax_rate=tax_rate,
        service_fee_rate=service_fee_rate,
        is_service_taxable=is_service_taxable,
    )

    assert fin["subtotal"] == Decimal("33.33")
    assert fin["service_fee_total"] == Decimal("2.50")
    assert fin["tax_total"] == Decimal("5.00")
    assert fin["total_amount"] == Decimal("40.83")
