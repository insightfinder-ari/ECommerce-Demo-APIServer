"""Connection-leak trigger for the Demo Store (Case 1).

This load profile drives the connection leak in GET /api/products/{id}: every
request for a product id that does not exist leaks one pool connection (the
route acquires a connection and never releases it on the 404 path). After
POOL_MAX_SIZE (default 20) misses, the pool is exhausted and *every* DB-backed
request starts returning 503 db_pool_timeout after POOL_ACQUIRE_TIMEOUT seconds.

The leaks are spread evenly over LEAK_DURATION, so apiserver_db_pool_in_use
climbs step by step instead of jumping: the first leak is sent at start, the
last one at LEAK_DURATION, and then Locust exits. The leaked connections stay
leaked; the apiserver keeps failing until it is restarted.

Unlike the normal locustfile, point this one straight at the apiserver, not the
UI's nginx. Always run exactly one user (-u 1); each extra user would leak at
the same rate on its own:

    locust -f loadgen/leak_locustfile.py --host http://APISERVER:8000 \
        --headless -u 1 -r 1

Run the normal locustfile at the same time so there is real traffic to degrade.

The run is bracketed by two InsightFinder change events (deploymentEventReceive):
one just before the first leak, posing as the code deploy that introduced the
leak, and one right after the last leak, posing as the ARI action deploying the
fix. They are only sent when IF_LICENSE_KEY, IF_PROJECT and IF_USER are set.

Knobs (environment variables):
    LEAK_DURATION  seconds from the first leak to the last (default 7200, 2 hours)
    LEAK_COUNT     connections to leak; set it to the apiserver's POOL_MAX_SIZE
                   to end with an exhausted pool (default 20)
    MISS_ID_BASE   first product id to request; must be above the real catalog
                   so the lookup always misses (default 10000000)
    IF_URL         InsightFinder base URL (default https://app.insightfinder.com)
    IF_LICENSE_KEY InsightFinder license key
    IF_PROJECT     InsightFinder project that receives the change events
    IF_USER        InsightFinder user name
    IF_INSTANCE    instance name the change events are attached to
                   (default build-server)

Watch while it runs:
    curl -s http://APISERVER:8000/metrics | grep apiserver_db_pool
    tail -f /var/log/apiserver/apiserver.log | grep -E 'pool|503'

Recover: restart the apiserver (systemctl restart apiserver); leaked
connections are only reclaimed on restart.
"""

import json
import logging
import os
import time

import requests
from locust import HttpUser, constant_pacing, events, task

log = logging.getLogger("leak")

LEAK_DURATION = float(os.getenv("LEAK_DURATION", "7200"))
LEAK_COUNT = int(os.getenv("LEAK_COUNT", "20"))
MISS_ID_BASE = int(os.getenv("MISS_ID_BASE", "10000000"))

IF_URL = os.getenv("IF_URL", "https://app.insightfinder.com").rstrip("/")
IF_LICENSE_KEY = os.getenv("IF_LICENSE_KEY", "")
IF_PROJECT = os.getenv("IF_PROJECT", "")
IF_USER = os.getenv("IF_USER", "")
IF_INSTANCE = os.getenv("IF_INSTANCE", "build-server")

# First leak at t=0, last at t=LEAK_DURATION.
LEAK_INTERVAL = LEAK_DURATION / max(LEAK_COUNT - 1, 1)

DEPLOY_EVENT = (
    "jobType: deploy\n"
    "buildStatus: SUCCESS\n"
    "service: apiserver\n"
    "change: GET /api/products/{id} returns 404 for unknown product ids"
)
FIX_EVENT = (
    "jobType: ari-remediation\n"
    "buildStatus: SUCCESS\n"
    "service: apiserver\n"
    "change: ARI action deployed fix: release the DB pool connection on the "
    "404 path of GET /api/products/{id}"
)


def send_change_event(data):
    """Post one change event to InsightFinder; never fails the run."""
    if not (IF_LICENSE_KEY and IF_PROJECT and IF_USER):
        log.warning("IF_LICENSE_KEY/IF_PROJECT/IF_USER not set; skipping change event")
        return
    event = {
        "timestamp": int(time.time() * 1000),
        "instanceName": IF_INSTANCE,
        "data": data,
    }
    try:
        r = requests.post(
            f"{IF_URL}/api/v1/deploymentEventReceive",
            data={
                "deploymentData": json.dumps([event]),
                "licenseKey": IF_LICENSE_KEY,
                "projectName": IF_PROJECT,
                "userName": IF_USER,
                "instanceName": IF_INSTANCE,
            },
            timeout=10,
        )
        log.info("change event sent: HTTP %s %s", r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.error("change event failed: %s", e)


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    send_change_event(DEPLOY_EVENT)


class Leaker(HttpUser):
    # constant_pacing keeps the spacing fixed even when a request is slow.
    wait_time = constant_pacing(LEAK_INTERVAL)

    def on_start(self):
        self.sent = 0
        self.leaked = 0
        self.started = time.monotonic()
        log.info("leaking %d connections over %.0fs (one every %.1fs)", LEAK_COUNT, LEAK_DURATION, LEAK_INTERVAL)

    @task
    def leak_one_connection(self):
        pid = MISS_ID_BASE + self.sent
        self.sent += 1
        with self.client.get(
            f"/api/products/{pid}",
            name="/api/products/{id} (miss -> leak)",
            headers={"X-Request-ID": f"leak-{pid}"},
            catch_response=True,
        ) as r:
            if r.status_code == 404:
                # The miss went through and its connection is now leaked.
                self.leaked += 1
                r.success()
            elif r.status_code == 503:
                # Pool already exhausted (e.g. LEAK_COUNT > POOL_MAX_SIZE): the
                # symptom we are demonstrating, not a load-generator failure.
                r.success()
        elapsed = time.monotonic() - self.started
        log.info("leak %d/%d at t=%.0fs: HTTP %s", self.sent, LEAK_COUNT, elapsed, r.status_code)

        if self.sent >= LEAK_COUNT:
            log.info("done: %d connections leaked in %.0fs; restart the apiserver to recover", self.leaked, elapsed)
            #send_change_event(FIX_EVENT)
            self.environment.runner.quit()
