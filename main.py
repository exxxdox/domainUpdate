import os
from datetime import datetime

from utils.cloudfare_api import dns_records_list, dns_record_create, dns_record_update
from utils.gotify import send_gotify_message
from utils.ip_detect import get_ipv6_temporary_addresses

zone_id = os.environ.get('CLOUDFARE_ZONE_ID')
record_name = os.environ.get('CLOUDFARE_RECORD_NAME')


def update_record():
    ipv6_address_now = get_ipv6_temporary_addresses()
    if ipv6_address_now is None:
        return
    page = dns_records_list(zone_id)
    is_record_created = False
    for record in page:
        if record.name == record_name:
            is_record_created = True
            if ipv6_address_now == record.content:
                print("无需更新")
            else:
                dns_record_update(record.id, zone_id, ipv6_address_now)
                print("更新ip")
                send_gotify_message(title=f"Server ip update", message=f"Record updated at {datetime.now()}")
    if not is_record_created:
        # 创建记录
        dns_record_create(zone_id, ipv6_address_now, record_name, False, "AAAA")
        print("创建ip")
        send_gotify_message(title=f"Server ip created", message=f"Record created at {datetime.now()}")


if __name__ == "__main__":
    print(f"Start--------------------------")
    update_record()
    print(f"End----------------------------")
