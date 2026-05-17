#!/usr/bin/env python3
from __future__ import annotations

import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import scalper


CONFIG = scalper.load_json(scalper.DEFAULT_CONFIG)
MACBOOK_TARGET = next(
    target for target in CONFIG["targets"] if target["id"] == "macbook-13-apple-silicon-16-24gb"
)


def raw_deal(**overrides):
    deal = {
        "title": 'Apple MacBook Air A2681 13.6" 2022 M2 16GB RAM 512GB SSD STARLIGHT',
        "url": "https://www.ebay.com/itm/327105153050",
        "source": "eBay",
        "price_usd": 587.95,
        "shipping_usd": 18.40,
        "total_usd": 606.35,
        "availability": "available",
        "ships_to_us": True,
    }
    deal.update(overrides)
    return deal


class EbayValidationTests(unittest.TestCase):
    def test_rejects_antibot_blocked_ebay_item_because_availability_is_unverified(self) -> None:
        deal = scalper.normalize_deal(MACBOOK_TARGET, raw_deal())
        self.assertIsNotNone(deal)
        with patch(
            "scalper.request.urlopen",
            side_effect=HTTPError(deal.url, 403, "Forbidden", hdrs=None, fp=None),
        ):
            self.assertEqual(scalper.validate_listing_url(deal), "http_403_antibot_unverified")


if __name__ == "__main__":
    unittest.main()
