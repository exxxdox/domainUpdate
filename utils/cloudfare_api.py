import os
from cloudflare import Cloudflare
from cloudflare.types.dns import AAAARecord
from dotenv import load_dotenv

from utils.gotify import send_gotify_message

load_dotenv()

api_token = os.environ.get("CLOUDFARE_TOKEN")

cloudfareClient = Cloudflare(
    api_token=os.environ.get("CLOUDFARE_TOKEN")
)


def dns_records_list(zone_id):
    page = cloudfareClient.dns.records.list(
        zone_id=zone_id,
    )
    return page


def dns_record_create(zone_id, content, name, proxied):
    send_gotify_message(title='Record Creating', message=f'{content}')
    record_response = cloudfareClient.dns.records.create(
        zone_id=zone_id,
        content=content,
        name=name,
        proxied=proxied,
        type="AAAA",
        ttl=60,
    )

    return record_response


def dns_record_update(record_id, zone_id, content):
    send_gotify_message(title='Record Updating', message=f'{content}')
    record_response = cloudfareClient.dns.records.edit(
        dns_record_id=record_id,
        name="server",
        type="AAAA",
        zone_id=zone_id,
        content=content,
        ttl=60,
    )
    return record_response


if __name__ == '__main__':
    print(os.environ.get("CLOUDFARE_RECORD_NAME"))
    print(dns_records_list(os.environ.get("CLOUDFARE_ZONE_ID")))
    pass
