"""Decision browser package."""

from importlib.metadata import PackageNotFoundError, version

from decision_browser.browser import (
    DEFAULT_CDP_TIMEOUT_MS,
    DEFAULT_DEVTOOLS_ACTIVE_PORT,
    SIGNED_IN_PROVENANCE,
    BrowserBlocked,
    BrowserDriver,
    BrowserMode,
    DevtoolsEndpoint,
    InventorySnapshot,
    TargetRecord,
    digest_inventory,
    parse_devtools_active_port,
    read_devtools_active_port,
)

__all__ = [
    "DEFAULT_CDP_TIMEOUT_MS",
    "DEFAULT_DEVTOOLS_ACTIVE_PORT",
    "SIGNED_IN_PROVENANCE",
    "BrowserBlocked",
    "BrowserDriver",
    "BrowserMode",
    "DevtoolsEndpoint",
    "InventorySnapshot",
    "TargetRecord",
    "digest_inventory",
    "parse_devtools_active_port",
    "read_devtools_active_port",
]

try:
    __version__ = version("decision-browser")
except PackageNotFoundError:
    __version__ = "0+unknown"
