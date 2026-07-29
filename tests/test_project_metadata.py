# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

from importlib.metadata import metadata


def test_installed_distribution_metadata_matches_samsarix_brand() -> None:
    package = metadata("samsarix-notifications")

    assert package["Name"] == "samsarix-notifications"
    assert package["Version"] == "0.1.0"
    assert package["License-Expression"] == "MPL-2.0"
    assert "Samsarix LLC" in package["Author-email"]
    assert "contact@samsarix.com" in package["Author-email"]
    assert "support@samsarix.com" in package["Maintainer-email"]
    assert package["Requires-Python"] == ">=3.10"
