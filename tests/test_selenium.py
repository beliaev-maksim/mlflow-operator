from pathlib import Path
from subprocess import check_output
from time import sleep

import pytest
import yaml
from selenium.common.exceptions import JavascriptException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumwire import webdriver


@pytest.fixture()
def driver(request):
    status = yaml.safe_load(
        check_output(
            [
                "microk8s",
                "kubectl",
                "get",
                "services/istio-ingressgateway",
                "-nmlflow",
                "-oyaml",
            ]
        )
    )
    endpoint = status["status"]["loadBalancer"]["ingress"][0]["ip"]
    url = f"http://{endpoint}.nip.io/mlflow/"
    options = Options()
    options.headless = True
    options.log.level = "trace"
    max_wait = 10  # seconds

    kwargs = {
        "options": options,
        "seleniumwire_options": {"enable_har": True},
    }

    with webdriver.Firefox(**kwargs) as driver:
        wait = WebDriverWait(driver, max_wait)
        driver.get(url)
        yield driver, wait, url

        Path(f"/tmp/selenium-{request.node.name}.har").write_text(driver.har)
        driver.get_screenshot_as_file(f"/tmp/selenium-{request.node.name}.png")


def test_dashboard(driver):
    """Ensures the dashboard can be connected to."""

    driver, wait, url = driver

    # TODO: More testing
    wait.until(EC.presence_of_element_located((By.CLASS_NAME, "experiment-view-container")))
