import netifaces
import subprocess
import re


def get_ipv6_addresses(interface) -> str | None:
    # 调用 ip -6 addr show dev [portname] 获取接口的 IPv6 地址信息
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


if __name__ == "__main__":
    print(get_ipv6_temporary_addresses())
