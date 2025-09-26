
from typing import Any
import openstack
import time
from blazarclient.client import Client as BlazarClient
from requests import Response
from zunclient.client import Client as ZunClient
import utils
import os

from keystoneauth1.session import Session as ksaSession

import logging

# Enable debug logging
logging.basicConfig(level=logging.INFO, filename='output.log', filemode='a',)
openstack.enable_logging(debug=False)

class RequestIdCapturingSession(object):

    def __init__(self, session: ksaSession) -> None:
        self._session = session
        self.last_request_id = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def request(self, *args, **kwargs) -> Response:
        response = self._session.request(*args, **kwargs)
        self.last_request_id = response.headers.get('X-OpenStack-Request-ID')
        return response

def run_test()  -> None: 
    # Initialize connection to OpenStack
    conn = openstack.connect()

    # customize session with request logging
    wrapped_session = RequestIdCapturingSession(conn.session)

    # reserve a device
    blazar = BlazarClient(session=wrapped_session)
    zun = ZunClient(version='1', session=wrapped_session)

    res_t0 = time.perf_counter()
    lease = utils.reserve_device(blazar, node_type="raspberrypi4-64", duration_hours=1)
    res_t1 = time.perf_counter()
    logging.info(f"Reserved device with lease ID {lease['id']} in {res_t1 - res_t0:.2f} seconds")

    reservation_id = utils.get_device_reservation_id(lease)
    reserved_devices = utils.get_devices_from_lease_id(blazar, lease['id'])

    for reservation_id, devices in reserved_devices.items():
        for device in devices:
            device_id = device.get("id")
            device_hostname = device.get("name")
            device_type = device.get("machine_name")
            logging.info(f"Reservation ID: {reservation_id}, Device ID: {device_id}, Hostname: {device_hostname}, Type: {device_type}")

    hints = {'reservation': reservation_id}
    # launch container using reserved device


    zun_t0 = time.perf_counter()
    container = zun.containers.create(name='test-container',
                                      image='alpine',
                                      command=['sleep', '3600'],
                                      hints=hints)
    
    zun_t1 = time.perf_counter()
    logging.info(f"Created container {container.uuid} in {zun_t1 - zun_t0:.2f} seconds")

    # wait for container to start
    utils.wait_for_container_status(zun, container.uuid, desired_status='Running', timeout=300)
    zun_t2 = time.perf_counter()
    logging.info(f"Container {container.uuid} is running after {zun_t2 - zun_t1:.2f} seconds")


    container_details = zun.containers.get(container.uuid)
    port_id = None
    for uuid, addrs in container_details.addresses.items():
        for addr in addrs:
            # TODO: handle multiple addresses
            port_id = addr.get('port')

    if port_id is not None:

        fip_t0 = time.perf_counter()
        # allocate floating IP
        floating_ip = conn.network.create_ip(
            floating_network_id='17446dec-0c72-4d28-abf5-99f43e152221',
            port_id=port_id,
        )
        fip_t1 = time.perf_counter()
        fip_create_elapsed = fip_t1 - fip_t0
        logging.info(f"Associated floating IP {floating_ip.floating_ip_address} to fixed ip {floating_ip.fixed_ip_address} on container {container.uuid} in {fip_create_elapsed:.2f} seconds")

        # wait for it to become active
        while True:
            floating_ip = conn.network.get_ip(floating_ip.id)
            fip_t2 = time.perf_counter()
            if floating_ip.status == 'ACTIVE':
                logging.info(f"Floating IP {floating_ip.floating_ip_address} is active after {fip_t2 - fip_t1:.2f} seconds")
                break
            time.sleep(1)

            if fip_t2 - fip_t1 > 60:
                logging.info("Timeout waiting for floating IP to become active")
                return
            
        # test connectivity
        ping_t0 = time.perf_counter()

        response = os.system(f"ping -t 120 -o {floating_ip.floating_ip_address} > /dev/null 2>&1")
        ping_t1 = time.perf_counter()
        ping_elapsed = ping_t1 - ping_t0
        if response == 0:
            logging.info(f"Ping to {floating_ip.floating_ip_address} successful in {ping_elapsed:.2f} seconds")
        else:
            logging.info(f"Ping to {floating_ip.floating_ip_address} failed at {ping_elapsed:.2f} seconds")


    logging.info("cleaning up resources")

    conn.network.delete_ip(floating_ip.id)

    blazar.lease.delete(lease['id'])
    #check if it still exists
    try:
        lease = blazar.lease.get(lease['id'])
        logging.info("Lease still exists:", lease)
    except Exception as e:
        logging.info("Lease deleted successfully.")
        pass

def main():
    while True:
        try:
            run_test()
        except Exception as e:
            logging.info("Error during test:", str(e))
        
        logging.info("starting next test")



if __name__ == "__main__":
    main()
