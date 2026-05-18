#!/usr/bin/env python3
from __future__ import annotations

import unittest
from unittest import mock

import scalper


TARGET = {
    "id": "test-target",
    "max_price_usd": 100,
}
CONFIG = scalper.load_json(scalper.DEFAULT_CONFIG)
MACBOOK_TARGET = next(
    target for target in CONFIG["targets"] if target["id"] == "macbook-13-apple-silicon-16-24gb"
)
MAC_MINI_M1_TARGET = next(target for target in CONFIG["targets"] if target["id"] == "mac-mini-m1-16gb")
MAC_MINI_M2_TARGET = next(target for target in CONFIG["targets"] if target["id"] == "mac-mini-m2-newer-16gb")


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

    def test_accepts_offerup_local_pickup_with_local_marketplace_config(self) -> None:
        config = {"local_marketplaces": {"offerup": {"enabled": True, "zip": "27519"}}}
        self.assertIsNotNone(
            scalper.normalize_deal(
                TARGET,
                raw_deal(
                    url="https://offerup.com/item/detail/example",
                    source="OfferUp",
                    ships_to_us=False,
                    shipping_destination="Local pickup only in Cary, NC",
                ),
                config,
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
                raw_deal(title="Apple MacBook Air 13-inch M2 16GB RAM 512GB"),
            )
        )

    def test_macbook_target_rejects_256gb_storage(self) -> None:
        self.assertIsNone(
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

    def test_mac_mini_m1_target_accepts_16gb(self) -> None:
        self.assertIsNotNone(
            scalper.normalize_deal(
                MAC_MINI_M1_TARGET,
                raw_deal(title="Apple Mac mini M1 16GB RAM 512GB"),
            )
        )

    def test_mac_mini_m1_target_rejects_8gb(self) -> None:
        self.assertIsNone(
            scalper.normalize_deal(
                MAC_MINI_M1_TARGET,
                raw_deal(title="Apple Mac mini M1 8GB RAM 256GB"),
            )
        )

    def test_mac_mini_m1_target_rejects_256gb_storage(self) -> None:
        self.assertIsNone(
            scalper.normalize_deal(
                MAC_MINI_M1_TARGET,
                raw_deal(title="Apple Mac mini M1 16GB RAM 256GB"),
            )
        )

    def test_mac_mini_m1_target_rejects_newer_chip(self) -> None:
        self.assertIsNone(
            scalper.normalize_deal(
                MAC_MINI_M1_TARGET,
                raw_deal(title="Apple Mac mini M2 16GB RAM 512GB"),
            )
        )

    def test_mac_mini_m2_target_accepts_16gb(self) -> None:
        self.assertIsNotNone(
            scalper.normalize_deal(
                MAC_MINI_M2_TARGET,
                raw_deal(title="Apple Mac mini M2 16GB RAM 512GB", total_usd=500),
            )
        )

    def test_mac_mini_m2_target_rejects_m1(self) -> None:
        self.assertIsNone(
            scalper.normalize_deal(
                MAC_MINI_M2_TARGET,
                raw_deal(title="Apple Mac mini M1 16GB RAM 512GB", total_usd=350),
            )
        )

    def test_mac_mini_m2_target_rejects_256gb_storage(self) -> None:
        self.assertIsNone(
            scalper.normalize_deal(
                MAC_MINI_M2_TARGET,
                raw_deal(title="Apple Mac mini M2 16GB RAM 256GB", total_usd=500),
            )
        )

    def test_mac_mini_targets_reject_parts(self) -> None:
        self.assertIsNone(
            scalper.normalize_deal(
                MAC_MINI_M1_TARGET,
                raw_deal(title="Apple Mac mini M1 16GB logic board for parts"),
            )
        )

    def test_browser_validation_rejects_rendered_sold_page(self) -> None:
        deal = scalper.normalize_deal(TARGET, raw_deal(title="B&O Beoplay EX charging case"))
        self.assertIsNotNone(deal)
        completed = scalper.subprocess.CompletedProcess(
            args=["chromium"],
            returncode=0,
            stdout="<html><body><h1>B&O Beoplay EX charging case</h1><p>This item has been sold.</p></body></html>",
            stderr="",
        )
        with (
            mock.patch.object(scalper, "chromium_bin", return_value="/usr/bin/chromium"),
            mock.patch.object(scalper.subprocess, "run", return_value=completed),
        ):
            self.assertEqual(scalper.validate_listing_in_browser(deal, {}), "sold")

    def test_browser_validation_rejects_browser_failure(self) -> None:
        deal = scalper.normalize_deal(TARGET, raw_deal(title="B&O Beoplay EX charging case"))
        self.assertIsNotNone(deal)
        completed = scalper.subprocess.CompletedProcess(
            args=["chromium"],
            returncode=1,
            stdout="",
            stderr="navigation failed",
        )
        with (
            mock.patch.object(scalper, "chromium_bin", return_value="/usr/bin/chromium"),
            mock.patch.object(scalper.subprocess, "run", return_value=completed),
        ):
            self.assertEqual(
                scalper.validate_listing_in_browser(deal, {}),
                "browser_error:navigation failed",
            )

    def test_browser_validation_allows_offerup_local_pickup_text(self) -> None:
        config = {"local_marketplaces": {"offerup": {"enabled": True, "zip": "27519"}}}
        deal = scalper.normalize_deal(
            TARGET,
            raw_deal(
                title="B&O Beoplay EX charging case",
                url="https://offerup.com/item/detail/example",
                source="OfferUp",
                ships_to_us=False,
                shipping_destination="Local pickup only in Cary, NC",
            ),
            config,
        )
        self.assertIsNotNone(deal)
        completed = scalper.subprocess.CompletedProcess(
            args=["chromium"],
            returncode=0,
            stdout=(
                "<html><body><h1>B&O Beoplay EX charging case</h1>"
                "<p>Local pickup only in Cary, NC. Available now.</p>"
                "<p>Listing details include original charging case, clean condition, "
                "seller location in Cary near 27519, and a public meetup option.</p>"
                "</body></html>"
            ),
            stderr="",
        )
        with (
            mock.patch.object(scalper, "chromium_bin", return_value="/usr/bin/chromium"),
            mock.patch.object(scalper.subprocess, "run", return_value=completed),
        ):
            self.assertIsNone(scalper.validate_listing_in_browser(deal, config))

    def test_ebay_antibot_http_result_is_inconclusive_reason(self) -> None:
        self.assertTrue("http_403_antibot_unverified".endswith("_antibot_unverified"))


if __name__ == "__main__":
    unittest.main()
