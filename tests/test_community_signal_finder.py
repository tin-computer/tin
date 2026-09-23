"""The community signal finder package satisfies the contributed-package contract."""

from pathlib import Path

import pytest

from tin_lite.community import ContributedPackage, validate


ROOT = Path(__file__).parents[1]
KEY = "marketing.community_signal_finder"


@pytest.mark.asyncio
async def test_community_signal_finder_package_is_valid():
    await validate(
        ContributedPackage(
            key=KEY,
            path=ROOT / "workflow_packages" / KEY,
        ),
        root=ROOT,
    )
