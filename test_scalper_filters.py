#!/usr/bin/env python3
from __future__ import annotations

import unittest

import scalper


TARGET = {
    "id": "test-target",
    "max_price_usd": 100,
}
CONFIG = scalper.load_json(scalper.DEFAULT_CONFIG)
MACBOOK_TARGET = next(
    target for target in CONFIG["targets"] if target["id"] == "macbook-13-apple-silicon-16-24gb"
)


def raw_deal(**overrides):
    deal = {
        "title": "B&O Beoplay EX charging case",
        "url": "https://www.ebay.com/itm/123",
        "source": "eBay",
        "price_usd": 40,
        "shipping_usd": 10,
        "total_usd": 50,
        "availability": "available",
        "ships_to_us": True,
    }
    deal.update(overrides)
    return deal


class ScalperFilterTests(unittest.TestCase):
    def test_accepts_available_direct_shipping_listing(self) -> None:
        self.assertIsNotNone(scalper.normalize_deal(TARGET, raw_deal()))

    def test_rejects_sold_listing(self) -> None:
        self.assertIsNone(
            scalper.normalize_deal(
                TARGET,
                raw_deal(availability="sold"),
            )
        )

    def test_rejects_sold_listing_status_even_when_availability_claims_available(self) -> None:
        self.assertIsNone(
            scalper.normalize_deal(
                TARGET,
                raw_deal(availability="available", listing_status="This item has been sold"),
            )
        )

    def test_rejects_not_available_text_in_reason(self) -> None:
        self.assertIsNone(
            scalper.normalize_deal(
                TARGET,
                raw_deal(availability="available", why_good="Good price, but currently unavailable"),
            )
        )

    def test_rejects_no_us_shipping_field(self) -> None:
        self.assertIsNone(
            scalper.normalize_deal(
                TARGET,
                raw_deal(ships_to_us=False, shipping_destination="Japan only"),
            )
        )

    def test_rejects_no_shipping_text(self) -> None:
        self.assertIsNone(
            scalper.normalize_deal(
                TARGET,
                raw_deal(shipping_destination="Does not ship to United States"),
            )
        )

    def test_rejects_local_only_craigslist(self) -> None:
        self.assertIsNone(
            scalper.normalize_deal(
                TARGET,
                raw_deal(
                    url="https://orlando.craigslist.org/sop/d/example.html",
                    source="Craigslist",
                ),
            )
        )

    def test_rejects_japanese_mercari_domain(self) -> None:
        self.assertIsNone(
            scalper.normalize_deal(
                TARGET,
                raw_deal(
                    url="https://jp.mercari.com/item/m64542036988",
                    source="Mercari Japan",
                ),
            )
        )

    def test_ntfy_header_sanitizes_non_latin_text(self) -> None:
        self.assertEqual(scalper.http_header_value("ケース"), "???")

    def test_macbook_target_accepts_m2_or_newer(self) -> None:
        self.assertIsNotNone(
            scalper.normalize_deal(
                MACBOOK_TARGET,
                raw_deal(title="Apple MacBook Air 13-inch M2 16GB RAM 256GB"),
            )
        )

    def test_macbook_target_rejects_m1(self) -> None:
        self.assertIsNone(
            scalper.normalize_deal(
                MACBOOK_TARGET,
                raw_deal(title="Apple MacBook Air 13-inch M1 16GB RAM 512GB"),
            )
        )


if __name__ == "__main__":
    unittest.main()
