import json
import logging
import os
import time
from typing import Any

import openstack
from blazarclient.client import Client as BlazarClient
from blazarclient.v1.client import Client as BlazarV1Client
from keystoneauth1.session import Session as ksaSession
from openstack.connection import Connection
from requests import Response
from zunclient.client import Client as ZunClient
from zunclient.v1.client import Client as ZunV1Client
from zunclient.v1.containers import Container

import utils

# Enable debug logging
logging.basicConfig(level=logging.DEBUG)
LOG = logging.getLogger(__name__)
openstack.enable_logging(debug=False, http_debug=False)


class RequestIdCapturingSession(object):
    def __init__(self, session: ksaSession) -> None:
        self._session = session
        self.last_request_id = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def request(self, *args, **kwargs) -> Response:
        response = self._session.request(*args, **kwargs)
        self.last_request_id = response.headers.get("X-OpenStack-Request-ID")
        return response


class TestLaunchDeviceContainer(object):
    def __init__(
        self, conn: Connection, blazar: BlazarV1Client, zun: ZunV1Client
    ) -> None:
        self.conn = conn
        self.blazar = blazar
        self.zun = zun

    def reservation_stage(self) -> None:
        res_t0 = time.perf_counter()
        lease = utils.reserve_device(
            self.blazar, node_type="raspberrypi4-64", duration_hours=1
        )
        res_t1 = time.perf_counter()

        self.lease_create_elapsed = res_t1 - res_t0
        self.lease_id = lease["id"]

        lease = utils.wait_for_lease_status(
            blazar=self.blazar, lease_id=lease["id"], timeout=300
        )
        res_t2 = time.perf_counter()
        self.lease_active_elapsed = res_t2 - res_t1

        self.reservation_id = utils.get_device_reservation_id(lease)
        devices_by_reservation_id = utils.get_devices_from_lease_id(
            self.blazar, lease["id"]
        )

        devices = devices_by_reservation_id.get(self.reservation_id, [])
        device = devices[0]  # assuming one device per reservation

        self.device_id = device.get("id")
        self.device_name = device.get("name")
        self.device_type = device.get("machine_name")

    def container_stage(self) -> None:
        # get hint from reservation
        hints = {"reservation": self.reservation_id}

        zun_t0 = time.perf_counter()
        self.container: Container = self.zun.containers.create(
            name="test-container",
            image="alpine",
            command=["sleep", "3600"],
            hints=hints,
        )  # type: ignore
        zun_t1 = time.perf_counter()

        self.container_create_elapsed = zun_t1 - zun_t0

        # wait for container to start
        wait_result = utils.wait_for_container_status(
            self.zun, self.container.uuid, desired_status="Running", timeout=300
        )
        zun_t2 = time.perf_counter()

        if wait_result:
            self.container = wait_result
        self.container_active_elapsed = zun_t2 - zun_t1

        # populate port and internal ip addr
        # structure is dict of container "??" object
        # each one has array of dicts
        # addr
        # version
        # port
        container_ifaces = self.container.addresses.items()
        for _, address_dict_list in container_ifaces:
            if len(address_dict_list) > 1:
                LOG.warning(
                    "More than 1 address on container! %s %s",
                    self.container.uuid,
                    address_dict_list,
                )
            for address_dict in address_dict_list:
                # TODO: handle multiple addresses
                self.container_port_id = address_dict.get("port")
                self.container_ip_address = address_dict.get("addr")

    def fip_stage(self) -> None:
        fip_t0 = time.perf_counter()
        floating_ip = self.conn.network.create_ip(
            floating_network_id="17446dec-0c72-4d28-abf5-99f43e152221",
            port_id=self.container_port_id,
        )
        fip_t1 = time.perf_counter()
        self.fip_create_elapsed = fip_t1 - fip_t0

        floating_ip = utils.wait_for_fip_status(
            self.conn, fip_id=floating_ip.id, timeout=300
        )
        fip_t2 = time.perf_counter()
        self.fip_start_elapsed = fip_t2 - fip_t1

        self.fip_id = floating_ip.id
        self.fip_address = floating_ip.floating_ip_address

    def ping_stage(self) -> bool:
        ping_t0 = time.perf_counter()
        response = os.system(f"ping -t 120 -o {self.fip_address} > /dev/null 2>&1")
        ping_t1 = time.perf_counter()
        self.ping_elapsed = ping_t1 - ping_t0

        if response == 0:
            self.ping_success = True
        else:
            self.ping_success = False

        return self.ping_success

    def cleanup(self):
        self.conn.network.delete_ip(self.fip_id)
        self.blazar.lease.delete(self.lease_id)

    def run_test(self) -> None:
        self.reservation_stage()
        LOG.info(
            "Device %s; Lease %s, Reservation %s",
            self.device_name,
            self.lease_id,
            self.reservation_id,
        )

        self.container_stage()
        LOG.info(
            "Container id %s; fixed_ip %s, port_id %s",
            self.container.uuid,
            self.container_ip_address,
            self.container_port_id,
        )

        self.fip_stage()
        LOG.info("Bound floating IP %s %s", self.fip_address, self.fip_id)

        if self.ping_stage():
            LOG.info(
                "Ping success to address %s on container %s device %s",
                self.fip_address,
                self.container.uuid,
                self.device_name,
            )
            self.cleanup()
        else:
            LOG.warning(
                "Ping Fail to address %s on container %s device %s",
                self.fip_address,
                self.container.uuid,
                self.device_name,
            )

    def output_results(self) -> dict:
        result = {
            "lease_id": self.lease_id,
            "reservation_id": self.reservation_id,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "container_id": self.container.uuid,
            "port_id": self.container_port_id,
            "fixed_addr": self.container_ip_address,
            "fip_id": self.fip_id,
            "fip_addr": self.fip_address,
            "ping_success": self.ping_success,
        }
        return result


def main():
    # Initialize connection to OpenStack
    conn = openstack.connect()
    # customize session with request logging
    wrapped_session = RequestIdCapturingSession(conn.session)

    # reserve a device
    blazar: BlazarV1Client = BlazarClient(session=wrapped_session)
    zun: ZunV1Client = ZunClient(session=wrapped_session)

    while True:
        testcase = TestLaunchDeviceContainer(conn, blazar, zun)
        testcase.run_test()
        # to write a new line
        with open("results.jsonl", "a") as f:
            f.write(json.dumps(testcase.output_results()) + "\n")


if __name__ == "__main__":
    main()
