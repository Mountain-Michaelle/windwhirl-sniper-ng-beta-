"""
field_selectors.py

Static mapping of confirmed selectors from the successful manual recording
(add_multi_order footprint) to friendly names. This is the "recipe" that
add_products.py follows -- no selector strings live anywhere else in the
codebase so the site can be re-verified/updated in exactly one place.

Every entry has a primary CSS/attribute selector plus the xpath fallback
that was captured in the recording, in case the primary breaks after a
front-end change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    name: str
    selector: str
    fallback_xpath: str
    kind: str  # "select" | "text" | "button" | "repeatable_select" | "repeatable_text"


# ---- Product line (one product = one #selpro + #pricevar interaction) -----
SELECT_PRODUCT = Field(
    name="select_product",
    selector="#selpro",
    fallback_xpath='//*[@id="selpro"]',
    kind="select",
)

SELECT_PRICEVAR = Field(
    name="select_pricevar",
    selector="#pricevar",
    fallback_xpath='//*[@id="pricevar"]',
    kind="select",
)

# ---- Custom price line items (only used when Excel price != CRM default) --
QTY_CUSTOM = Field(
    name="qty_custom",
    selector='[name="qty_c[]"]',
    fallback_xpath='//body/div[4]/div[2]/div[2]/section[1]/div[1]/div[1]/div[1]'
                    '/div[1]/div[1]/div[1]/form[1]/table[1]/tbody[1]/tr[1]/td[2]'
                    '/div[1]/div[1]/input[1]',
    kind="repeatable_text",
)

PRICE_CUSTOM = Field(
    name="price_custom",
    selector='[name="price_c[]"]',
    fallback_xpath='//body/div[4]/div[2]/div[2]/section[1]/div[1]/div[1]/div[1]'
                    '/div[1]/div[1]/div[1]/form[1]/table[1]/tbody[1]/tr[1]/td[2]'
                    '/div[1]/div[2]/input[1]',
    kind="repeatable_text",
)

# ---- Customer info ----------------------------------------------------------
CUSTOMER_NAME = Field(
    name="customer_name",
    selector='[name="cname"]',
    fallback_xpath='//body/div[4]/div[2]/div[2]/section[1]/div[1]/div[1]/div[1]'
                    '/div[1]/div[1]/div[1]/form[1]/table[3]/tbody[1]/tr[1]/td[1]/input[1]',
    kind="repeatable_text",
)

CUSTOMER_PHONE = Field(
    name="customer_phone",
    selector='[name="cphone"]',
    fallback_xpath='//body/div[4]/div[2]/div[2]/section[1]/div[1]/div[1]/div[1]'
                    '/div[1]/div[1]/div[1]/form[1]/table[3]/tbody[1]/tr[2]/td[2]/input[1]',
    kind="text",
)

CUSTOMER_WHATSAPP = Field(
    name="customer_whatsapp",
    selector='[name="cwaphone"]',
    fallback_xpath='//body/div[4]/div[2]/div[2]/section[1]/div[1]/div[1]/div[1]'
                    '/div[1]/div[1]/div[1]/form[1]/table[3]/tbody[1]/tr[2]/td[3]/input[1]',
    kind="text",
)

CUSTOMER_ADDRESS = Field(
    name="customer_address",
    selector='[name="caddress"]',
    fallback_xpath='//body/div[4]/div[2]/div[2]/section[1]/div[1]/div[1]/div[1]'
                    '/div[1]/div[1]/div[1]/form[1]/table[3]/tbody[1]/tr[1]/td[2]/input[1]',
    kind="text",
)

SELECT_STATE = Field(
    name="select_state",
    selector="#state",
    fallback_xpath='//*[@id="state"]',
    kind="select",
)

# ---- Payment -----------------------------------------------------------------
SELECT_PAYMENT = Field(
    name="select_payment",
    selector='[name="pgmain"]',
    fallback_xpath='//body/div[4]/div[2]/div[2]/section[1]/div[1]/div[1]/div[1]'
                    '/div[1]/div[1]/div[1]/form[1]/table[2]/tbody[1]/tr[1]/td[3]/select[1]',
    kind="select",
)
PAYMENT_METHOD_VALUE = "Cash"  # business rule 12: this NEVER changes

# ---- Submit --------------------------------------------------------------------
SUBMIT = Field(
    name="submit",
    selector="#submit",
    fallback_xpath='//*[@id="submit"]',
    kind="button",
)

# The recording shows submit clicked twice: once after product+customer+state
# are filled (reveals the payment section), then again after payment method
# is chosen (finalizes the order). Both use the same #submit selector.
SUBMIT_STAGES = 2

# ---- Custom option label inside #pricevar --------------------------------------
# Business rule 8: if Excel price != CRM default price, the "Custom" option in
# the #pricevar dropdown must be selected, which reveals qty_c[]/price_c[].
CUSTOM_PRICE_OPTION_LABEL = "Custom"


def locator(page, field: Field):
    """Return a Playwright locator for `field`, preferring the primary
    selector and falling back to the recorded xpath if the primary yields
    nothing. Callers should still call .wait_for(...) themselves -- this
    just picks *which* locator to use, it doesn't wait."""
    primary = page.locator(field.selector)
    try:
        if primary.count() > 0:
            return primary
    except Exception:
        pass
    return page.locator(f"xpath={field.fallback_xpath}")