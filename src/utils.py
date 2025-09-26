import datetime
import time
from collections import defaultdict

def reserve_device(blazar, node_type, duration_hours=1, wait_for_active=True):
    end_date = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=duration_hours)
    device_reservation_request = {
        "resource_type": "device",
        "min": "1",
        "max": "1",
        "resource_properties": '["==", "$machine_name", "{}"]'.format(node_type),
    }
    lease = blazar.lease.create(
        name='test-lease',
        start="now",
        end=end_date.strftime("%Y-%m-%d %H:%M"),
        reservations=[device_reservation_request],
        events=[],
    )

    if wait_for_active:

        lease_start = time.perf_counter()
        # wait until lease moves to 'active' state
        while True:
            lease = blazar.lease.get(lease['id'])
            lease_status = lease.get("status")

            elasped_time = time.perf_counter() - lease_start
            if elasped_time > 300:
                # print("Lease activation timed out.")
                break

            # print("Lease status:", lease_status)
            if lease_status == 'ACTIVE':
                # print("Lease started after {:.2f} seconds".format(elasped_time))
                break

            if lease_status == 'ERROR':
                break
            time.sleep(1)
    return lease

def get_device_reservation_id(lease):
    reservations = lease.get("reservations")
    if not reservations or len(reservations) == 0:
        return None

    device_reservations = [r for r in reservations if r.get("resource_type") == "device"]
    if not device_reservations or len(device_reservations) == 0:
        return None
    
    if len(device_reservations) > 1:
        raise Exception("Multiple device reservations found in lease.")

    return device_reservations[0]['id']


def wait_for_container_status(zun, container_id, desired_status, timeout=300):
    start_time = time.perf_counter()
    while True:
        
        container = zun.containers.get(container_id)
        elapsed_time = time.perf_counter() - start_time

        container_status = container.status
        # print("Container status:", container_status, "after {:.02f}s".format(elapsed_time))
        
        if container_status == desired_status:
            break
        elif container_status in ['Error', 'Exited']:
            # print("Container entered failure state:", container_status)
            raise Exception("Container entered failure state: " + container_status)

        if elapsed_time > timeout:
            # print("Timeout waiting for container to reach status:", desired_status)
            raise TimeoutError("Timeout waiting for container status")
        time.sleep(1)

    return container


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
