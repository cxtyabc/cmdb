import json
import sys

sys.path.insert(0, "/data/apps/cmdb")

from autoapp import app

from flask_login import login_user

from api.lib.cmdb.auto_discovery.auto_discovery import AutoDiscoveryCICRUD
from api.lib.utils import AESCrypto
from api.models.acl import User
from api.models.cmdb import AutoDiscoveryAccount
from api.models.cmdb import AutoDiscoveryCIType

from aliyunsdkcore.client import AcsClient
from aliyunsdkecs.request.v20140526 import DescribeInstancesRequest
from aliyunsdkecs.request.v20140526 import DescribeRegionsRequest


ADR_ID = 1
TYPE_ID = 5
CATEGORY = "云服务器 ECS"
CRON = "*/10 * * * *"
STATUS_MAP = {
    "Running": "在线",
    "Starting": "在线",
    "Stopping": "下线",
    "Stopped": "下线",
}
ATTRIBUTE_MAP = {
    "InstanceId": "uuid",
    "InstanceName": "vserver_name",
    "PrivateIpAddress": "private_ip",
    "Cpu": "cpu_count",
    "Memory": "ram_size",
    "OSName": "os_version",
    "InstanceType": "vserver_type",
    "Status": "status",
}


def decrypt_secret(secret):
    if not secret:
        return secret

    try:
        return AESCrypto.decrypt(secret)
    except Exception:
        return secret


def ensure_adt(account):
    extra_option = {
        "_reference": account.id,
        "alias": "阿里云 ECS",
        "category": CATEGORY,
        "collect_key": "ali.ecs",
        "provider": "aliyun",
    }
    adt = AutoDiscoveryCIType.get_by(type_id=TYPE_ID, adr_id=ADR_ID, first=True, to_dict=False)
    if adt is None:
        return AutoDiscoveryCIType.create(
            type_id=TYPE_ID,
            adr_id=ADR_ID,
            attributes=ATTRIBUTE_MAP,
            auto_accept=False,
            cron=CRON,
            extra_option=extra_option,
            uid=account.uid,
            enabled=True,
        )

    adt.update(
        attributes=ATTRIBUTE_MAP,
        auto_accept=False,
        cron=CRON,
        extra_option=extra_option,
        enabled=True,
        filter_none=False,
    )
    return adt


def get_regions(access_key, access_secret):
    client = AcsClient(access_key, access_secret, "cn-hangzhou")
    request = DescribeRegionsRequest.DescribeRegionsRequest()
    request.set_accept_format("json")
    response = json.loads(client.do_action_with_exception(request))
    return [item["RegionId"] for item in response.get("Regions", {}).get("Region", [])]


def iter_instances(access_key, access_secret, region_id):
    client = AcsClient(access_key, access_secret, region_id)
    page = 1
    while True:
        request = DescribeInstancesRequest.DescribeInstancesRequest()
        request.set_accept_format("json")
        request.set_PageSize(100)
        request.set_PageNumber(page)
        response = json.loads(client.do_action_with_exception(request))
        instances = response.get("Instances", {}).get("Instance", []) or []
        for instance in instances:
            yield instance

        total_count = int(response.get("TotalCount", 0) or 0)
        if page * 100 >= total_count or not instances:
            break
        page += 1


def normalize_instance(instance):
    status = STATUS_MAP.get(instance.get("Status"), "下线")
    instance_id = instance.get("InstanceId")
    private_ips = (
        (((instance.get("VpcAttributes") or {}).get("PrivateIpAddress") or {}).get("IpAddress") or [])
        or (((instance.get("InnerIpAddress") or {}).get("IpAddress")) or [])
    )
    return {
        "InstanceId": instance_id,
        "InstanceName": instance_id,
        "PrivateIpAddress": (private_ips[0] if private_ips else ""),
        "Cpu": instance.get("Cpu"),
        "Memory": str(instance.get("Memory") or ""),
        "OSName": instance.get("OSName") or instance.get("OSNameEn") or "",
        "InstanceType": instance.get("InstanceType") or "",
        "Status": status,
    }


def main():
    with app.app_context():
        account = AutoDiscoveryAccount.get_by(adr_id=ADR_ID, first=True, to_dict=False)
        if account is None:
            raise RuntimeError("Aliyun account is not configured in c_ad_accounts")

        config = account.config or {}
        access_key = config.get("key")
        access_secret = decrypt_secret(config.get("secret"))
        if not access_key or not access_secret:
            raise RuntimeError("Aliyun key/secret is incomplete")

        adt = ensure_adt(account)
        sync_user = User.get_by_id(account.uid)

        total = 0
        synced = 0
        for region_id in get_regions(access_key, access_secret):
            for instance in iter_instances(access_key, access_secret, region_id):
                payload = normalize_instance(instance)
                unique_value = payload["InstanceId"]
                if not unique_value:
                    continue

                AutoDiscoveryCICRUD().upsert(
                    type_id=TYPE_ID,
                    adt_id=adt.id,
                    unique_value=unique_value,
                    instance=payload,
                )
                adc = AutoDiscoveryCICRUD.cls.get_by(
                    type_id=TYPE_ID,
                    unique_value=unique_value,
                    first=True,
                    to_dict=False,
                )
                if adc is not None and not adc.is_accept:
                    with app.test_request_context(headers={"Accept-Language": "zh-CN"}):
                        if sync_user is not None:
                            login_user(sync_user)
                        AutoDiscoveryCICRUD.accept(adc, nickname="aliyun_sync")
                total += 1
                synced += 1

        print(
            json.dumps(
                {
                    "adt_id": adt.id,
                    "type_id": TYPE_ID,
                    "category": CATEGORY,
                    "synced": synced,
                    "total_seen": total,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
