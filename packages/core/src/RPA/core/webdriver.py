import contextlib
import logging
import os
import platform
import stat
from pathlib import Path
from typing import Optional

import requests
from requests import Response
from selenium import webdriver
from selenium.webdriver.common.service import Service
from selenium.webdriver.remote.webdriver import WebDriver
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.download_manager import WDMDownloadManager
from webdriver_manager.core.http import WDMHttpClient
from webdriver_manager.core.manager import DriverManager
from webdriver_manager.core.utils import os_name as get_os_name
from webdriver_manager.firefox import GeckoDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager, IEDriverManager
from webdriver_manager.opera import OperaDriverManager

from RPA.core.robocorp import robocorp_home


LOGGER = logging.getLogger(__name__)

DRIVER_ROOT = robocorp_home() / "webdrivers"
DRIVER_PREFERENCE = {
    "Windows": ["Chrome", "Firefox", "ChromiumEdge"],
    "Linux": ["Chrome", "Firefox", "ChromiumEdge"],
    "Darwin": ["Chrome", "Firefox", "ChromiumEdge", "Safari"],
    "default": ["Chrome", "Firefox"],
}
AVAILABLE_DRIVERS = {
    # Driver names taken from `webdrivermanager` and adapted to `webdriver_manager`.
    "chrome": ChromeDriverManager,
    "firefox": GeckoDriverManager,
    "gecko": GeckoDriverManager,
    "mozilla": GeckoDriverManager,
    # NOTE: Selenium 4 dropped support for Opera.
    #  (https://github.com/SeleniumHQ/selenium/issues/10835)
    "opera": OperaDriverManager,
    # NOTE: In Selenium 4 `Edge` is the same with `ChromiumEdge`.
    "edge": EdgeChromiumDriverManager,
    "chromiumedge": EdgeChromiumDriverManager,
    # NOTE: IE is discontinued and not supported/encouraged anymore.
    "ie": IEDriverManager,
}


class Downloader(WDMHttpClient):

    """Custom downloader which disables download progress reporting."""

    def get(self, url, **kwargs) -> Response:
        resp = requests.get(url=url, verify=self._ssl_verify, stream=True, **kwargs)
        self.validate_response(resp)
        return resp


@contextlib.contextmanager
def suppress_logging():
    """Suppress webdriver-manager logging."""
    wdm_log = "WDM_LOG"
    original_value = os.getenv(wdm_log, "")
    try:
        os.environ[wdm_log] = str(logging.NOTSET)
        yield
    finally:
        os.environ[wdm_log] = original_value


def start(browser: str, service: Optional[Service] = None, **options) -> WebDriver:
    """Start a webdriver with the given options."""
    browser = browser.strip()
    webdriver_factory = getattr(webdriver, browser, None)
    if not webdriver_factory:
        raise ValueError(f"Unsupported browser: {browser}")

    # NOTE: It is recommended to pass a `service` rather than deprecated `options`.
    driver = webdriver_factory(service=service, **options)
    return driver


def _to_manager(browser: str, root: Path = DRIVER_ROOT) -> DriverManager:
    browser = browser.strip()
    manager_factory = AVAILABLE_DRIVERS.get(browser.lower())
    if not manager_factory:
        raise ValueError(
            f"Unsupported browser {browser!r}! (choose from: {list(AVAILABLE_DRIVERS)})"
        )

    download_manager = WDMDownloadManager(Downloader())
    manager = manager_factory(path=str(root), download_manager=download_manager)
    return manager


def _set_executable(path: str) -> None:
    st = os.stat(path)
    os.chmod(
        path,
        st.st_mode | stat.S_IXOTH | stat.S_IXGRP | stat.S_IEXEC,
    )


def download(browser: str, root: Path = DRIVER_ROOT) -> Optional[str]:
    """Download a webdriver binary for the given browser and return the path to it."""
    manager = _to_manager(browser, root)
    driver = manager.driver
    resolved_os = getattr(driver, "os_type", driver.get_os_type())
    os_name = get_os_name()
    if os_name not in resolved_os:
        LOGGER.warning(
            "Attempting to download incompatible driver for OS %r on OS %r! Skip",
            resolved_os,
            os_name,
        )
        return None  # incompatible driver download attempt

    with suppress_logging():
        path: str = manager.install()
    if platform.system() != "Windows":
        _set_executable(path)
    LOGGER.debug("Downloaded webdriver to: %s", path)
    return path
