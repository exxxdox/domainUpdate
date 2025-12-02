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
        print("临时ipv6地址为空！")
        return
    print(f"当前ipv6临时地址为: {ipv6_address_now}")
    page = dns_records_list(zone_id)
    is_record_created = False
    for record in page:
        if record.name == record_name:
            is_record_created = True
            print(f"当前dns解析地址为: {record.content}")
            if ipv6_address_now == record.content:
                print("无需更新")
            else:
                result = dns_record_update(record.id, zone_id, ipv6_address_now)
                if result is None:
                    print("更新解析记录失败")
                else:
                    print("更新解析记录成功")
                # send_gotify_message(title=f"Server ip update", message=f"Record updated at {datetime.now()}")
    if not is_record_created:
        print("无dns记录")
        # 创建记录
        result = dns_record_create(zone_id, ipv6_address_now, record_name, False)
        if result is None:
            print("创建解析记录失败")
        else:
            print("创建解析记录成功")
        # send_gotify_message(title=f"Server ip created", message=f"Record created at {datetime.now()}")


if __name__ == "__main__":
    print(f"Start--------------------------")
    update_record()
    print(f"End----------------------------")
