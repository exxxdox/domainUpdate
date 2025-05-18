import os
from cloudflare import Cloudflare

client = Cloudflare(
    api_token=os.environ.get("CLOUDFARE_TOKEN")
)


def dns_records_list(zone_id):
    page = client.dns.records.list(
        zone_id=zone_id,
    )
    return page


def dns_record_create(zone_id, content, name, proxied, type):
    record_response = client.dns.records.create(
        zone_id=zone_id,
        content=content,
        name=name,
        proxied=proxied,
        type=type
    )

    return record_response


def dns_record_update(record_id, zone_id, content):
    record_response = client.dns.records.edit(
        dns_record_id=record_id,
        zone_id=zone_id,
        content=content,
    )


if __name__ == '__main__':
    pass
