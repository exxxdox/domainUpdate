from datetime import datetime
import requests
import netifaces
from alibabacloud_util.domain_analyze import DomainUpdate
from rest.gotify import send_gotify_message
import subprocess
import re


def get_ipv6_addresses(interface) -> str | None:
    # 调用 ip -6 addr dev xxx 获取接口的 IPv6 地址信息
    result = subprocess.run(['ip', '-6', 'addr', 'show', 'dev', interface], stdout=subprocess.PIPE)
    output = result.stdout.decode('utf-8')

    # 匹配 IPv6 地址行和是否包含 temporary 标志
    ipv6_pattern = re.compile(r'inet6 (\S+)/\d+ scope(.*)')

    for line in output.splitlines():
        match = ipv6_pattern.search(line)
        if match:
            address = match.group(1)
            scope = match.group(2)
            if address.startswith('240e') and 'global temporary dynamic' in scope:
                return address

    return None


def get_ipv6_temporary_addresses() -> str | None:
    # 获取所有网络接口
    interfaces = netifaces.interfaces()

    for interface in interfaces:
        address = get_ipv6_addresses(interface)
        if address:
            return address

    return None


def update_ip_job():
    # 获取 IPv6 地址
    ipv6_address = get_ipv6_temporary_addresses()
    if not ipv6_address:
        print(f"ipv6_address is None!")
        return

    print(f"Current ipv6 is {ipv6_address}")

    current_domian_analyze = DomainUpdate.get_domain_record_value()
    print(f"Current Ali record is {current_domian_analyze}")
    if current_domian_analyze != ipv6_address:
        DomainUpdate.update_domain_record(ipv6_address)
        send_gotify_message(title=f"Server ip update", message=f"Record updated at {datetime.now()}")
        print(f"Domain updated to {ipv6_address}!!!!!!!!!!")


if __name__ == "__main__":
    print(f"Start--------------------------")
    update_ip_job()
    print(f"End----------------------------")
