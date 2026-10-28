"""Permissive models for MakeLeaps API v2023.06.26 read responses."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict


class Resource(BaseModel):
    """Retain unknown fields and serialize only fields present in the response."""

    model_config = ConfigDict(extra="allow")

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("mode", "json")
        kwargs.setdefault("exclude_unset", True)
        return super().model_dump(**kwargs)


class Money(Resource):
    amount: Decimal | None = None
    currency: str | None = None


class TaxTotal(Resource):
    tax_type: str | None = None
    tax_rate: Decimal | None = None
    currency: str | None = None
    subtotal: Decimal | Money | None = None
    tax: Decimal | Money | None = None


class Partner(Resource):
    url: str | None = None
    name: str | None = None
    clients: str | None = None
    contacts: str | None = None
    documents: str | None = None
    line_item_catalog: str | None = None
    tags: str | None = None
    sending_orders: str | None = None
    history: str | None = None
    integrations: dict[str, Any] | None = None
    account_name: str | None = None
    tax_registration_number: str | None = None


class Client(Resource):
    url: str | None = None
    mid: str | None = None
    display_name: str | None = None
    organization_type: str | None = None
    is_organization: bool | None = None
    date_archived: dt.datetime | None = None
    client_external_id: str | None = None
    contacts: list[Any] | None = None
    default_contact: str | dict[str, Any] | None = None
    managing_teams: list[Any] | None = None
    default_presets: list[Any] | None = None
    integrations: dict[str, Any] | None = None
    lang_code: str | None = None
    settings: str | dict[str, Any] | None = None
    date_cutoff: int | None = None
    tax_registration_number: str | None = None
    virtual_bank_accounts: list[Any] | None = None


class Contact(Resource):
    url: str | None = None
    mid: str | None = None
    owner: str | dict[str, Any] | None = None
    owner_type: str | None = None
    contact_type: str | None = None
    name: str | None = None
    family_name: str | None = None
    given_name: str | None = None
    display_name: str | None = None
    title: str | None = None
    department: str | None = None
    format: str | None = None
    phone_number: str | None = None
    fax_number: str | None = None
    is_default: bool | str | None = None
    email: str | dict[str, Any] | None = None
    addresses: list[Any] | None = None
    default_address: str | dict[str, Any] | None = None
    integrations: dict[str, Any] | None = None


class LineItem(Resource):
    kind: str | None = None
    description: str | None = None
    unit_cost: Decimal | None = None
    quantity: Decimal | None = None
    quantity_unit: str | None = None
    price: Decimal | None = None
    is_taxable: bool | None = None
    is_tax_inclusive: bool | None = None
    has_jp_withholding_tax: bool | None = None
    currency: str | None = None
    tax_rate: Decimal | None = None
    tax_type: str | None = None
    extended_data: dict[str, Any] | None = None
    vendor_cost: Decimal | None = None
    source_catalog_item: str | None = None


class Document(Resource):
    document_number: str | None = None
    document_type: str | None = None
    title: str | None = None
    project_name: str | None = None
    bank_details_header: str | None = None
    bank_details: str | None = None
    message: str | None = None
    proviso: str | None = None
    group_lineitem_by_tax_rate: bool | None = None
    has_hanko: bool | None = None
    has_prices: bool | None = None
    has_totals: bool | None = None
    has_currency_code: bool | None = None
    has_lineitem_tax_state: bool | None = None
    show_lineitem_tax_rate: bool | None = None
    show_lineitem_tax_state: bool | None = None
    show_lineitem_tax_type: bool | None = None
    signature: bool | None = None
    display_tax_rate: bool | None = None
    hide_tax_description: bool | None = None
    has_tax_stamp: bool | None = None
    currency: str | None = None
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    jp_withholding_tax: Decimal | None = None
    tax_rate: Decimal | None = None
    tax_type: str | None = None
    subtotal_sales_tax: Decimal | None = None
    subtotal_withholding_tax: Decimal | None = None
    total: Decimal | None = None
    mixed_tax_rate_totals: dict[str, TaxTotal] | None = None
    tax_group_totals: list[TaxTotal] | None = None
    extended_data: dict[str, Any] | None = None
    mid: str | None = None
    document_template: str | dict[str, Any] | None = None
    display_name: str | None = None
    note: str | None = None
    external_id: str | None = None
    round_tax_down: bool | None = None
    tax_rounding: str | None = None
    lineitem_price_rounding: str | None = None
    tags: list[Any] | None = None
    url: str | None = None
    pickup_links: str | dict[str, Any] | None = None
    pdf_content_url: str | None = None
    pdf_upload_url: str | None = None
    client: Client | str | None = None
    client_contact: Contact | str | None = None
    modified: dt.datetime | None = None
    date_paid: dt.datetime | None = None
    date_sent: dt.datetime | None = None
    date_cancelled: dt.datetime | None = None
    date_removed: dt.datetime | None = None
    date_accepted: dt.datetime | None = None
    date_confirmed: dt.datetime | None = None
    date_converted: dt.datetime | None = None
    payment_date: dt.date | None = None
    invoicing_period_start_date: dt.date | None = None
    invoicing_period_end_date: dt.date | None = None
    is_accepted: bool | None = None
    is_confirmed: bool | None = None
    is_converted: bool | None = None
    approval_status: str | None = None
    integrations: dict[str, Any] | None = None
    date: dt.date | None = None
    date_due: dt.date | None = None
    date_valid: dt.date | None = None
    date_shipped: dt.date | None = None
    sender_name: str | None = None
    sender_contact_department: str | None = None
    sender_contact_title: str | None = None
    sender_contact_name: str | None = None
    sender_address_format: str | None = None
    sender_country: str | None = None
    sender_postal_code: str | None = None
    sender_region: str | None = None
    sender_locality: str | None = None
    sender_street_address: str | None = None
    sender_extended_address: str | None = None
    sender_phone_number: str | None = None
    sender_fax_number: str | None = None
    partner_tax_registration_number: str | None = None
    display_partner_tax_registration_number: bool | None = None
    recipient_name: str | None = None
    recipient_contact_department: str | None = None
    recipient_contact_title: str | None = None
    recipient_contact_name: str | None = None
    recipient_honorific: str | None = None
    recipient_address_format: str | None = None
    recipient_country: str | None = None
    recipient_postal_code: str | None = None
    recipient_region: str | None = None
    recipient_locality: str | None = None
    recipient_street_address: str | None = None
    recipient_extended_address: str | None = None
    recipient_phone_number: str | None = None
    recipient_fax_number: str | None = None
    client_tax_registration_number: str | None = None
    display_client_tax_registration_number: bool | None = None
    lineitems: list[LineItem] | None = None
    purchase_order_ids: list[Any] | None = None
    peppol_details: Any | None = None


class DocumentArchive(Resource):
    mid: str | None = None
    url: str | None = None
    pdf_content_url: str | None = None
    uses_external_pdf: bool | str | None = None
    document_type: str | None = None
    document_number: str | None = None
    sending_methods: list[str] | None = None
    legal_categories: list[Any] | None = None
    date_sent: dt.datetime | None = None
    date: dt.date | None = None
    sender: str | None = None
    client: str | None = None
    currency: str | None = None
    total: Decimal | None = None
    external_id: str | None = None
    delivery_methods: list[str] | None = None


class MarkSentResult(Resource):
    """The saved document date and whether its Document Archive was created."""

    mid: str
    date_sent: dt.datetime
    archive_created: bool
    archive: DocumentArchive | None


class SentStatus(Resource):
    """A document's sent date and a matching archive of delivery outside MakeLeaps."""

    mid: str | None
    document_type: str | None
    document_number: str | None
    date_sent: dt.datetime | None
    has_external_archive: bool
