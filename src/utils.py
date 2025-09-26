import datetime
import time
from collections import defaultdict


def reserve_device(blazar, node_type, duration_hours=1):
    end_date = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        hours=duration_hours
    )
    device_reservation_request = {
        "resource_type": "device",
        "min": "1",
        "max": "1",
        "resource_properties": '["==", "$machine_name", "{}"]'.format(node_type),
    }
    lease = blazar.lease.create(
        name="test-lease",
        start="now",
        end=end_date.strftime("%Y-%m-%d %H:%M"),
        reservations=[device_reservation_request],
        events=[],
    )
    return lease


def wait_for_lease_status(blazar, lease_id, timeout=300):
    ERROR_STATES = ["ERROR"]

    lease_start = time.perf_counter()
    # wait until lease moves to 'active' state
    while True:
        lease = blazar.lease.get(lease_id)
        lease_status = lease.get("status")

        elasped_time = time.perf_counter() - lease_start
        if elasped_time > timeout:
            raise TimeoutError("Timeout waiting for lease to become active")
        elif lease_status in ERROR_STATES:
            raise Exception("Lease entered error state: " + lease_status)
        elif lease_status == "ACTIVE":
            return lease
        time.sleep(1)


def get_device_reservation_id(lease):
    reservations = lease.get("reservations")
    if not reservations or len(reservations) == 0:
        return None

    device_reservations = [
        r for r in reservations if r.get("resource_type") == "device"
    ]
    if not device_reservations or len(device_reservations) == 0:
        return None

    if len(device_reservations) > 1:
        raise Exception("Multiple device reservations found in lease.")

    return device_reservations[0]["id"]


def wait_for_container_status(zun, container_id, desired_status, timeout=300):
    start_time = time.perf_counter()
    while True:
        container = zun.containers.get(container_id)
        elapsed_time = time.perf_counter() - start_time

        container_status = container.status

        if elapsed_time > timeout:
            raise TimeoutError("Timeout waiting for container status")
        elif container_status in ["Error", "Exited"]:
            raise Exception("Container entered failure state: " + container_status)
        elif container_status == desired_status:
            return container
        time.sleep(1)


def wait_for_fip_status(conn, fip_id, timeout):
  
  start_time = time.perf_counter()
  while True:
    floating_ip = conn.network.get_ip(fip_id)
    elapsed = time.perf_counter() - start_time
    if floating_ip.status == "ACTIVE":
        return floating_ip
    elif elapsed > timeout:
        raise TimeoutError
    

def get_devices_from_lease_id(blazar, lease_id) -> dict:
    all_device_allocations = blazar.device.list_allocations()

    # invert allocations. Each allocation has a resource_id, and list of reservations
    # each reservation in the list has a lease_id

    devices_by_reservation_id = defaultdict(list)

    for alloc in all_device_allocations:
        for reservation in alloc.get("reservations", []):
            if reservation.get("lease_id") == lease_id:
                resource_id = alloc.get("resource_id")
                blazar_device = blazar.device.get(resource_id)
                devices_by_reservation_id[reservation.get("id")].append(blazar_device)

    return devices_by_reservation_id
