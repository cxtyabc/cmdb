# -*- coding:utf-8 -*-

import copy
import json
import traceback

import requests
from flask import current_app
from requests.auth import HTTPBasicAuth

from api.extensions import db
from api.lib.cmdb.const import ValueTypeEnum
from api.lib.cmdb.cache import AttributeCache
from api.lib.cmdb.cache import CITypeAttributesCache
from api.lib.cmdb.cache import CITypeCache
from api.lib.cmdb.auto_discovery.const import DEFAULT_INNER
from api.models.cmdb import Attribute
from api.models.cmdb import AutoDiscoveryAccount
from api.models.cmdb import AutoDiscoveryCI
from api.models.cmdb import AutoDiscoveryCIType
from api.models.cmdb import AutoDiscoveryExecHistory
from api.models.cmdb import AutoDiscoveryRule
from api.models.cmdb import CIType
from api.models.cmdb import CITypeAttribute


CLOUD_RESOURCE_TYPE = "cloud_resource"
CLOUD_RESOURCE_ALIAS = "云资源"
CLOUD_RESOURCE_CRON = "*/30 * * * *"

CLOUD_RESOURCE_ATTRIBUTES = [
    ("cloud_resource_uid", "云资源唯一标识", ValueTypeEnum.TEXT, True),
    ("cloud_provider", "云厂商", ValueTypeEnum.TEXT, False),
    ("cloud_account", "云账号", ValueTypeEnum.TEXT, False),
    ("cloud_region", "区域", ValueTypeEnum.TEXT, False),
    ("cloud_resource_type", "资源类型", ValueTypeEnum.TEXT, False),
    ("cloud_resource_id", "资源ID", ValueTypeEnum.TEXT, False),
    ("cloud_resource_name", "资源名称", ValueTypeEnum.TEXT, False),
    ("cloud_service", "云服务", ValueTypeEnum.TEXT, False),
    ("cloud_status", "状态", ValueTypeEnum.TEXT, False),
    ("cloud_private_ip", "内网IP", ValueTypeEnum.TEXT, False),
    ("cloud_public_ip", "公网IP", ValueTypeEnum.TEXT, False),
    ("raw_data", "原始数据", ValueTypeEnum.JSON, False),
]

PROVIDER_RULE_NAME = {
    item.get("option", {}).get("en"): item["name"]
    for item in DEFAULT_INNER
    if item.get("option", {}).get("en") in {"aliyun", "tencentcloud", "huaweicloud", "aws"}
}

RALPH_SCHEMA = "ralph.multicloud.normalized"
RALPH_SCHEMA_VERSION = "1.0"
COMPUTE_RESOURCE_TYPES = {"ecs", "cvm", "ec2"}


def _safe_json(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return str(value)


def _first(value):
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def _to_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _unique_list(values):
    result = []
    seen = set()
    for value in values or []:
        if value in (None, ""):
            continue
        value = str(value)
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _dict_get(data, *keys):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _ensure_attribute(name, alias, value_type, is_unique=False):
    attr = Attribute.get_by(name=name, first=True, to_dict=False)
    if attr is not None:
        return attr

    attr = Attribute.create(
        flush=True,
        name=name,
        alias=alias,
        value_type=value_type,
        is_unique=is_unique,
        is_index=is_unique,
    )
    db.session.commit()
    AttributeCache.clean(attr)
    return attr


def ensure_cloud_resource_type():
    unique_attr = None
    attr_ids = []
    for name, alias, value_type, is_unique in CLOUD_RESOURCE_ATTRIBUTES:
        attr = _ensure_attribute(name, alias, value_type, is_unique)
        attr_ids.append(attr.id)
        if is_unique:
            unique_attr = attr

    ci_type = CIType.get_by(name=CLOUD_RESOURCE_TYPE, first=True, to_dict=False)
    if ci_type is None:
        ci_type = CIType.create(
            flush=True,
            name=CLOUD_RESOURCE_TYPE,
            alias=CLOUD_RESOURCE_ALIAS,
            unique_id=unique_attr.id,
            show_id=Attribute.get_by(name="cloud_resource_name", first=True, to_dict=False).id,
            enabled=True,
            icon="caise-cloud",
        )
        db.session.commit()
        CITypeCache.clean(CLOUD_RESOURCE_TYPE)

    for attr_id in attr_ids:
        existed = CITypeAttribute.get_by(type_id=ci_type.id, attr_id=attr_id, first=True, to_dict=False)
        if existed is None:
            CITypeAttribute.create(type_id=ci_type.id, attr_id=attr_id, is_required=(attr_id == unique_attr.id))

    CITypeAttributesCache.clean(ci_type.id)
    return ci_type


def get_provider_by_rule(adr_id):
    rule = AutoDiscoveryRule.get_by_id(adr_id)
    if rule is None:
        return

    option = rule.option or {}
    if option.get("en"):
        return option["en"]

    for provider, rule_name in PROVIDER_RULE_NAME.items():
        if rule.name == rule_name:
            return provider


def ensure_cloud_adt(account, provider):
    ci_type = ensure_cloud_resource_type()
    rule = AutoDiscoveryRule.get_by_id(account.adr_id)
    if rule is None:
        return

    extra_option = {
        "_reference": account.id,
        "alias": "{} {}".format(rule.name, CLOUD_RESOURCE_ALIAS),
        "category": CLOUD_RESOURCE_ALIAS,
        "collect_key": "{}.all".format(provider),
        "provider": provider,
    }
    attributes = {name: name for name, _, _, _ in CLOUD_RESOURCE_ATTRIBUTES}

    adt = AutoDiscoveryCIType.get_by(
        type_id=ci_type.id,
        adr_id=account.adr_id,
        first=True,
        to_dict=False,
    )
    if adt is None:
        adt = AutoDiscoveryCIType.create(
            type_id=ci_type.id,
            adr_id=account.adr_id,
            attributes=attributes,
            auto_accept=True,
            cron=CLOUD_RESOURCE_CRON,
            extra_option=extra_option,
            uid=account.uid,
            enabled=True,
        )
    else:
        adt.update(
            attributes=attributes,
            auto_accept=True,
            cron=adt.cron or CLOUD_RESOURCE_CRON,
            extra_option=extra_option,
            enabled=True,
            filter_none=False,
        )

    return adt


class CloudResource(object):
    def __init__(self, provider, account, region, resource_type, resource_id, name=None,
                 service=None, status=None, private_ip=None, public_ip=None, raw=None):
        self.provider = provider
        self.account = account
        self.region = region or ""
        self.resource_type = resource_type
        self.resource_id = str(resource_id or "")
        self.name = name or self.resource_id
        self.service = service or resource_type
        self.status = status or ""
        self.private_ip = private_ip or ""
        self.public_ip = public_ip or ""
        self.raw = _safe_json(raw or {})

    @property
    def unique_value(self):
        return ":".join([self.provider, str(self.account.id), self.region, self.resource_type, self.resource_id])

    def as_instance(self):
        return {
            "cloud_resource_uid": self.unique_value,
            "cloud_provider": self.provider,
            "cloud_account": self.account.name,
            "cloud_region": self.region,
            "cloud_resource_type": self.resource_type,
            "cloud_resource_id": self.resource_id,
            "cloud_resource_name": self.name,
            "cloud_service": self.service,
            "cloud_status": self.status,
            "cloud_private_ip": self.private_ip,
            "cloud_public_ip": self.public_ip,
            "raw_data": self.raw,
        }


class BaseProviderCollector(object):
    provider = None

    def __init__(self, account, config):
        self.account = account
        self.config = config or {}
        self.key = self.config.get("key") or self.config.get("access_key") or self.config.get("accessKey")
        self.secret = self.config.get("secret") or self.config.get("access_secret") or self.config.get("secretKey")

    def collect(self):
        return []

    def _resource(self, **kwargs):
        return CloudResource(self.provider, self.account, **kwargs)


class AliyunCollector(BaseProviderCollector):
    provider = "aliyun"
    PAGE_SIZE = 50

    def _client(self, region_id):
        from aliyunsdkcore.client import AcsClient

        return AcsClient(self.key, self.secret, region_id)

    @staticmethod
    def _do(client, request):
        request.set_accept_format("json")
        return json.loads(client.do_action_with_exception(request))

    def _regions(self):
        from aliyunsdkecs.request.v20140526 import DescribeRegionsRequest

        client = self._client("cn-hangzhou")
        request = DescribeRegionsRequest.DescribeRegionsRequest()
        response = self._do(client, request)
        return [item["RegionId"] for item in response.get("Regions", {}).get("Region", [])]

    def _ecs(self, region_id):
        from aliyunsdkecs.request.v20140526 import DescribeInstancesRequest

        page = 1
        while True:
            request = DescribeInstancesRequest.DescribeInstancesRequest()
            request.set_PageNumber(page)
            request.set_PageSize(self.PAGE_SIZE)
            response = self._do(self._client(region_id), request)
            items = response.get("Instances", {}).get("Instance", []) or []
            for item in items:
                private_ips = (
                    (((item.get("VpcAttributes") or {}).get("PrivateIpAddress") or {}).get("IpAddress") or [])
                    or (((item.get("InnerIpAddress") or {}).get("IpAddress")) or [])
                )
                public_ips = (
                    (((item.get("PublicIpAddress") or {}).get("IpAddress")) or [])
                    or (((item.get("EipAddress") or {}).get("IpAddress")) and [item["EipAddress"]["IpAddress"]])
                    or []
                )
                yield self._resource(
                    region=region_id,
                    resource_type="ecs",
                    resource_id=item.get("InstanceId"),
                    name=item.get("InstanceName") or item.get("InstanceId"),
                    service="云服务器 ECS",
                    status=item.get("Status"),
                    private_ip=_first(private_ips),
                    public_ip=_first(public_ips),
                    raw=item,
                )

            total = int(response.get("TotalCount", 0) or 0)
            if page * self.PAGE_SIZE >= total or not items:
                break
            page += 1

    def _ecs_disks(self, region_id):
        from aliyunsdkecs.request.v20140526 import DescribeDisksRequest

        page = 1
        while True:
            request = DescribeDisksRequest.DescribeDisksRequest()
            request.set_PageNumber(page)
            request.set_PageSize(self.PAGE_SIZE)
            response = self._do(self._client(region_id), request)
            items = response.get("Disks", {}).get("Disk", []) or []
            for item in items:
                yield self._resource(
                    region=region_id,
                    resource_type="disk",
                    resource_id=item.get("DiskId"),
                    name=item.get("DiskName") or item.get("DiskId"),
                    service="云服务器 Disk",
                    status=item.get("Status"),
                    raw=item,
                )
            total = int(response.get("TotalCount", 0) or 0)
            if page * self.PAGE_SIZE >= total or not items:
                break
            page += 1

    def _security_groups(self, region_id):
        from aliyunsdkecs.request.v20140526 import DescribeSecurityGroupsRequest

        page = 1
        while True:
            request = DescribeSecurityGroupsRequest.DescribeSecurityGroupsRequest()
            request.set_PageNumber(page)
            request.set_PageSize(self.PAGE_SIZE)
            response = self._do(self._client(region_id), request)
            items = response.get("SecurityGroups", {}).get("SecurityGroup", []) or []
            for item in items:
                yield self._resource(
                    region=region_id,
                    resource_type="security_group",
                    resource_id=item.get("SecurityGroupId"),
                    name=item.get("SecurityGroupName") or item.get("SecurityGroupId"),
                    service="安全组",
                    raw=item,
                )
            total = int(response.get("TotalCount", 0) or 0)
            if page * self.PAGE_SIZE >= total or not items:
                break
            page += 1

    def _paged_items(self, client, request_cls, list_key, item_key):
        page = 1
        while True:
            request = request_cls()
            request.set_PageNumber(page)
            request.set_PageSize(self.PAGE_SIZE)
            response = self._do(client, request)
            items = response.get(list_key, {}).get(item_key, []) or []
            for item in items:
                yield item

            total = int(response.get("TotalCount", 0) or 0)
            if page * self.PAGE_SIZE >= total or not items:
                break
            page += 1

    def _vpcs(self, region_id):
        from aliyunsdkvpc.request.v20160428 import DescribeVpcsRequest
        from aliyunsdkvpc.request.v20160428 import DescribeVSwitchesRequest
        from aliyunsdkvpc.request.v20160428 import DescribeEipAddressesRequest

        client = self._client(region_id)
        for item in self._paged_items(client, DescribeVpcsRequest.DescribeVpcsRequest, "Vpcs", "Vpc"):
            yield self._resource(
                region=region_id,
                resource_type="vpc",
                resource_id=item.get("VpcId"),
                name=item.get("VpcName") or item.get("VpcId"),
                service="专有网络VPC",
                raw=item,
            )

        for item in self._paged_items(
            client, DescribeVSwitchesRequest.DescribeVSwitchesRequest, "VSwitches", "VSwitch"
        ):
            yield self._resource(
                region=region_id,
                resource_type="vswitch",
                resource_id=item.get("VSwitchId"),
                name=item.get("VSwitchName") or item.get("VSwitchId"),
                service="交换机Switch",
                raw=item,
            )

        for item in self._paged_items(
            client, DescribeEipAddressesRequest.DescribeEipAddressesRequest, "EipAddresses", "EipAddress"
        ):
            yield self._resource(
                region=region_id,
                resource_type="eip",
                resource_id=item.get("AllocationId"),
                name=item.get("Name") or item.get("IpAddress") or item.get("AllocationId"),
                service="弹性公网IP",
                public_ip=item.get("IpAddress"),
                status=item.get("Status"),
                raw=item,
            )

    def collect(self):
        resources = []
        for region_id in self._regions():
            for func in (self._ecs, self._ecs_disks, self._security_groups, self._vpcs):
                try:
                    resources.extend(list(func(region_id)))
                except Exception as e:
                    current_app.logger.warning("aliyun sync {} {} failed: {}".format(region_id, func.__name__, e))
        return resources


class TencentCloudCollector(BaseProviderCollector):
    provider = "tencentcloud"

    def _credential(self):
        from tencentcloud.common.credential import Credential

        return Credential(self.key, self.secret)

    @staticmethod
    def _models_to_dict(items):
        return [json.loads(item.to_json_string()) for item in items or []]

    def _regions(self):
        from tencentcloud.cvm.v20170312 import cvm_client
        from tencentcloud.cvm.v20170312 import models

        client = cvm_client.CvmClient(self._credential(), "ap-guangzhou")
        response = client.DescribeRegions(models.DescribeRegionsRequest())
        return [item.Region for item in (response.RegionSet or [])]

    def _cvm(self, region_id):
        from tencentcloud.cvm.v20170312 import cvm_client
        from tencentcloud.cvm.v20170312 import models

        client = cvm_client.CvmClient(self._credential(), region_id)
        offset = 0
        while True:
            request = models.DescribeInstancesRequest()
            request.Offset = offset
            request.Limit = 100
            response = client.DescribeInstances(request)
            items = self._models_to_dict(response.InstanceSet)
            for item in items:
                yield self._resource(
                    region=region_id,
                    resource_type="cvm",
                    resource_id=item.get("InstanceId"),
                    name=item.get("InstanceName") or item.get("InstanceId"),
                    service="云服务器 CVM",
                    status=item.get("InstanceState"),
                    private_ip=_first(item.get("PrivateIpAddresses")),
                    public_ip=_first(item.get("PublicIpAddresses")),
                    raw=item,
                )
            if offset + 100 >= int(response.TotalCount or 0) or not items:
                break
            offset += 100

    def _vpc(self, region_id):
        from tencentcloud.vpc.v20170312 import models
        from tencentcloud.vpc.v20170312 import vpc_client

        client = vpc_client.VpcClient(self._credential(), region_id)

        request = models.DescribeVpcsRequest()
        response = client.DescribeVpcs(request)
        for item in self._models_to_dict(response.VpcSet):
            yield self._resource(
                region=region_id,
                resource_type="vpc",
                resource_id=item.get("VpcId"),
                name=item.get("VpcName") or item.get("VpcId"),
                service="私有网络VPC",
                raw=item,
            )

        request = models.DescribeSubnetsRequest()
        response = client.DescribeSubnets(request)
        for item in self._models_to_dict(response.SubnetSet):
            yield self._resource(
                region=region_id,
                resource_type="subnet",
                resource_id=item.get("SubnetId"),
                name=item.get("SubnetName") or item.get("SubnetId"),
                service="子网",
                raw=item,
            )

        request = models.DescribeSecurityGroupsRequest()
        response = client.DescribeSecurityGroups(request)
        for item in self._models_to_dict(response.SecurityGroupSet):
            yield self._resource(
                region=region_id,
                resource_type="security_group",
                resource_id=item.get("SecurityGroupId"),
                name=item.get("SecurityGroupName") or item.get("SecurityGroupId"),
                service="安全组",
                raw=item,
            )

        request = models.DescribeAddressesRequest()
        response = client.DescribeAddresses(request)
        for item in self._models_to_dict(response.AddressSet):
            yield self._resource(
                region=region_id,
                resource_type="eip",
                resource_id=item.get("AddressId"),
                name=item.get("AddressName") or item.get("AddressIp") or item.get("AddressId"),
                service="弹性公网IP",
                public_ip=item.get("AddressIp"),
                status=item.get("AddressStatus"),
                raw=item,
            )

    def collect(self):
        resources = []
        for region_id in self._regions():
            for func in (self._cvm, self._vpc):
                try:
                    resources.extend(list(func(region_id)))
                except Exception as e:
                    current_app.logger.warning("tencentcloud sync {} {} failed: {}".format(region_id, func.__name__, e))
        return resources


class AWSCollector(BaseProviderCollector):
    provider = "aws"

    def _session(self):
        import boto3

        return boto3.session.Session(
            aws_access_key_id=self.key,
            aws_secret_access_key=self.secret,
        )

    def _regions(self):
        client = self._session().client("ec2", region_name="us-east-1")
        return [item["RegionName"] for item in client.describe_regions(AllRegions=False).get("Regions", [])]

    def _ec2(self, region_id):
        client = self._session().client("ec2", region_name=region_id)
        paginator = client.get_paginator("describe_instances")
        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                for item in reservation.get("Instances", []):
                    name = item.get("InstanceId")
                    for tag in item.get("Tags", []):
                        if tag.get("Key") == "Name":
                            name = tag.get("Value") or name
                    yield self._resource(
                        region=region_id,
                        resource_type="ec2",
                        resource_id=item.get("InstanceId"),
                        name=name,
                        service="云服务器 EC2",
                        status=(item.get("State") or {}).get("Name"),
                        private_ip=item.get("PrivateIpAddress"),
                        public_ip=item.get("PublicIpAddress"),
                        raw=item,
                    )

        for item in client.describe_vpcs().get("Vpcs", []):
            yield self._resource(region=region_id, resource_type="vpc", resource_id=item.get("VpcId"),
                                 name=item.get("VpcId"), service="VPC", raw=item)
        for item in client.describe_subnets().get("Subnets", []):
            yield self._resource(region=region_id, resource_type="subnet", resource_id=item.get("SubnetId"),
                                 name=item.get("SubnetId"), service="Subnet", raw=item)
        for item in client.describe_security_groups().get("SecurityGroups", []):
            yield self._resource(region=region_id, resource_type="security_group", resource_id=item.get("GroupId"),
                                 name=item.get("GroupName") or item.get("GroupId"), service="SecurityGroup", raw=item)
        for item in client.describe_volumes().get("Volumes", []):
            yield self._resource(region=region_id, resource_type="volume", resource_id=item.get("VolumeId"),
                                 name=item.get("VolumeId"), service="EBS", status=item.get("State"), raw=item)
        for item in client.describe_addresses().get("Addresses", []):
            yield self._resource(region=region_id, resource_type="eip",
                                 resource_id=item.get("AllocationId") or item.get("PublicIp"),
                                 name=item.get("PublicIp"), service="Elastic IP", public_ip=item.get("PublicIp"),
                                 raw=item)

    def collect(self):
        resources = []
        for region_id in self._regions():
            try:
                resources.extend(list(self._ec2(region_id)))
            except Exception as e:
                current_app.logger.warning("aws sync {} failed: {}".format(region_id, e))
        return resources


class HuaweiCloudCollector(BaseProviderCollector):
    provider = "huaweicloud"
    DEFAULT_REGIONS = [
        "cn-east-3",
        "cn-south-1",
        "cn-north-4",
        "cn-north-1",
        "cn-east-2",
        "cn-southwest-2",
    ]

    def _credential(self):
        from huaweicloudsdkcore.auth.credentials import BasicCredentials

        return BasicCredentials(self.key, self.secret)

    @staticmethod
    def _to_dict(item):
        if isinstance(item, dict):
            return item
        if hasattr(item, "to_dict"):
            return item.to_dict()
        return json.loads(json.dumps(item, default=lambda o: getattr(o, "__dict__", str(o))))

    def _ecs(self, region_id):
        from huaweicloudsdkecs.v2 import EcsClient
        from huaweicloudsdkecs.v2 import ListServersDetailsRequest
        from huaweicloudsdkecs.v2.region.ecs_region import EcsRegion

        try:
            region = EcsRegion.value_of(region_id)
        except KeyError:
            region = EcsRegion._PROVIDER.get_region(region_id)
            if region is None:
                endpoint = "https://ecs.{}.myhuaweicloud.com".format(region_id)
                from huaweicloudsdkcore.region.region import Region
                region = Region(region_id, endpoint)

        client = EcsClient.new_builder().with_credentials(self._credential()).with_region(region).build()
        request = ListServersDetailsRequest(limit=1000)
        response = client.list_servers_details(request)
        for raw in getattr(response, "servers", None) or []:
            item = self._to_dict(raw)
            private_ip = ""
            public_ip = ""
            for addresses in (item.get("addresses") or {}).values():
                for address in addresses or []:
                    if address.get("OS-EXT-IPS:type") == "floating":
                        public_ip = public_ip or address.get("addr", "")
                    else:
                        private_ip = private_ip or address.get("addr", "")
            yield self._resource(
                region=region_id,
                resource_type="ecs",
                resource_id=item.get("id"),
                name=item.get("name") or item.get("id"),
                service="云服务器 ECS",
                status=item.get("status"),
                private_ip=private_ip,
                public_ip=public_ip,
                raw=item,
            )

            metadata = item.get("metadata") or {}
            if metadata.get("vpc_id"):
                yield self._resource(
                    region=region_id,
                    resource_type="vpc",
                    resource_id=metadata.get("vpc_id"),
                    name=metadata.get("vpc_id"),
                    service="虚拟私有云VPC",
                    raw={"vpc_id": metadata.get("vpc_id"), "server_id": item.get("id")},
                )

            for sg in item.get("security_groups") or []:
                sg_name = sg.get("name") or sg.get("id") or ""
                if not sg_name:
                    continue
                yield self._resource(
                    region=region_id,
                    resource_type="security_group",
                    resource_id=sg.get("id") or sg_name,
                    name=sg_name,
                    service="安全组",
                    raw=sg,
                )

            for volume in item.get("os-extended-volumes:volumes_attached") or []:
                volume_id = volume.get("id")
                if not volume_id:
                    continue
                yield self._resource(
                    region=region_id,
                    resource_type="evs",
                    resource_id=volume_id,
                    name=volume_id,
                    service="云硬盘EVS",
                    raw=volume,
                )

            if public_ip:
                yield self._resource(
                    region=region_id,
                    resource_type="eip",
                    resource_id=public_ip,
                    name=public_ip,
                    service="弹性公网IP",
                    public_ip=public_ip,
                    raw={"public_ip": public_ip, "server_id": item.get("id")},
                )

    def collect(self):
        resources = []
        for region_id in self.config.get("regions") or self.DEFAULT_REGIONS:
            try:
                resources.extend(list(self._ecs(region_id)))
            except Exception as e:
                current_app.logger.warning("huaweicloud sync {} ecs failed: {}".format(region_id, e))
        return resources


class RalphNormalizedPayloadBuilder(object):
    def __init__(self, account, provider, config=None):
        self.account = account
        self.provider = provider
        self.config = config or {}
        self.project_strategy = (self.config.get("project_strategy") or "auto").lower()

    def build(self, resources):
        payload = {
            "schema": RALPH_SCHEMA,
            "schema_version": RALPH_SCHEMA_VERSION,
            "provider": self.config.get("provider") or self.provider,
            "account": {
                "id": str(self.config.get("account_id") or self.account.id),
                "name": self.config.get("account_name") or self.account.name,
            },
            "sync": {
                "mode": self._sync_mode(),
                "delete_missing": bool(self.config.get("delete_missing", False)),
            },
            "projects": [],
            "flavors": [],
            "hosts": [],
        }

        projects = {}
        flavors = {}
        hosts = {}

        for resource in resources or []:
            if resource.resource_type not in COMPUTE_RESOURCE_TYPES or not resource.resource_id:
                continue

            project = self._build_project(resource)
            flavor = self._build_flavor(resource)
            host = self._build_host(resource, project, flavor)
            if host is None:
                continue

            projects[project["id"]] = project
            flavors[flavor["id"]] = flavor
            hosts[host["id"]] = host

        payload["projects"] = [projects[key] for key in sorted(projects)]
        payload["flavors"] = [flavors[key] for key in sorted(flavors)]
        payload["hosts"] = [hosts[key] for key in sorted(hosts)]
        return payload

    def _sync_mode(self):
        mode = (self.config.get("sync_mode") or "full").lower()
        return mode if mode in {"full", "incremental"} else "full"

    def _build_project(self, resource):
        project_id, project_name = self._extract_project_identity(resource)
        tags = self._extract_tags(resource, include_runtime=False)
        if resource.region:
            tags.setdefault("region", resource.region)
        return {
            "id": project_id,
            "name": project_name,
            "tags": tags,
        }

    def _build_flavor(self, resource):
        spec = self._extract_compute_spec(resource)
        flavor_id = self._flavor_id(resource, spec)
        return {
            "id": flavor_id,
            "name": spec["name"] or flavor_id,
            "region": resource.region or "",
            "cpu": spec["cpu"],
            "memory_mib": spec["memory_mib"],
            "disk_gib": spec["disk_gib"],
            "tags": self._extract_tags(resource, include_runtime=False),
        }

    def _build_host(self, resource, project, flavor):
        spec = self._extract_compute_spec(resource)
        if not resource.resource_id:
            return

        host = {
            "id": resource.resource_id,
            "name": resource.name or resource.resource_id,
            "region": resource.region or "",
            "project_id": project["id"],
            "flavor_id": flavor["id"],
            "image": self._extract_image(resource),
            "status": resource.status or "",
            "cpu": spec["cpu"],
            "memory_mib": spec["memory_mib"],
            "disk_gib": spec["disk_gib"],
            "ips": self._extract_ips(resource),
            "tags": self._extract_tags(resource, include_runtime=True),
        }
        return host

    def _extract_project_identity(self, resource):
        raw = resource.raw if isinstance(resource.raw, dict) else {}
        region = resource.region or "global"

        if self.project_strategy == "region":
            return "region:{}".format(region), "{} {}".format(self.account.name, region)

        if self.provider == "aliyun":
            resource_group_id = raw.get("ResourceGroupId")
            if resource_group_id:
                return "resource-group:{}".format(resource_group_id), resource_group_id

        if self.provider == "tencentcloud":
            placement = raw.get("Placement") or {}
            project_id = placement.get("ProjectId") or raw.get("ProjectId")
            if project_id not in (None, "", 0, "0"):
                project_name = placement.get("ProjectName") or "project-{}".format(project_id)
                return "project:{}".format(project_id), project_name

        if self.provider == "huaweicloud":
            project_id = raw.get("tenant_id") or raw.get("project_id")
            if project_id:
                return "project:{}".format(project_id), "project-{}".format(project_id)

        return "region:{}".format(region), "{} {}".format(self.account.name, region)

    def _extract_compute_spec(self, resource):
        raw = resource.raw if isinstance(resource.raw, dict) else {}
        cpu = 0
        memory_mib = 0
        disk_gib = 0
        flavor_id = ""
        flavor_name = ""

        if self.provider == "aliyun":
            flavor_id = raw.get("InstanceType") or ""
            flavor_name = flavor_id
            cpu = _to_int(raw.get("Cpu"))
            memory_mib = _to_int(raw.get("Memory"))
            disk_gib = _to_int(_dict_get(raw, "SystemDisk", "Size"))
            for item in raw.get("DataDisk") or []:
                disk_gib += _to_int(item.get("Size"))
            disk_gib = disk_gib or _to_int(raw.get("LocalStorageCapacity"))

        elif self.provider == "tencentcloud":
            flavor_id = raw.get("InstanceType") or ""
            flavor_name = flavor_id
            cpu = _to_int(raw.get("CPU"))
            memory_mib = _to_int(raw.get("Memory")) * 1024
            disk_gib = _to_int(_dict_get(raw, "SystemDisk", "DiskSize"))
            for item in raw.get("DataDisks") or []:
                disk_gib += _to_int(item.get("DiskSize"))

        elif self.provider == "huaweicloud":
            flavor = raw.get("flavor") or {}
            metadata = raw.get("metadata") or {}
            flavor_id = (
                flavor.get("id")
                or metadata.get("metering.resourcespeccode")
                or metadata.get("resourcespec")
                or ""
            )
            flavor_name = (
                metadata.get("metering.resourcespeccode")
                or metadata.get("resourcespec")
                or flavor_id
            )
            cpu = _to_int(flavor.get("vcpus") or flavor.get("cpu"))
            memory_mib = _to_int(flavor.get("ram") or flavor.get("memory_mb"))
            disk_gib = _to_int(flavor.get("disk") or flavor.get("disk_gb"))

        else:
            flavor_id = raw.get("InstanceType") or raw.get("instance_type") or ""
            flavor_name = flavor_id
            cpu = _to_int(raw.get("Cpu") or raw.get("cpu") or raw.get("CPU"))
            memory_mib = _to_int(raw.get("Memory") or raw.get("memory_mib"))
            disk_gib = _to_int(raw.get("Disk") or raw.get("disk_gib"))
            if memory_mib and memory_mib < 512:
                memory_mib *= 1024

        return {
            "id": flavor_id,
            "name": flavor_name,
            "cpu": max(1, cpu),
            "memory_mib": max(1, memory_mib),
            "disk_gib": max(1, disk_gib),
        }

    def _extract_image(self, resource):
        raw = resource.raw if isinstance(resource.raw, dict) else {}
        if self.provider == "aliyun":
            return raw.get("ImageId") or raw.get("OSName") or raw.get("OSNameEn") or ""
        if self.provider == "tencentcloud":
            return raw.get("ImageId") or raw.get("OsName") or ""
        if self.provider == "huaweicloud":
            metadata = raw.get("metadata") or {}
            image = raw.get("image") or {}
            return metadata.get("image_name") or image.get("id") or ""
        return raw.get("ImageId") or raw.get("image") or ""

    def _extract_tags(self, resource, include_runtime=True):
        raw = resource.raw if isinstance(resource.raw, dict) else {}
        result = {}

        if self.provider == "aliyun":
            for item in _dict_get(raw, "Tags", "Tag") or []:
                key = item.get("TagKey")
                if key:
                    result[str(key)] = str(item.get("TagValue") or "")

        elif self.provider == "tencentcloud":
            for item in raw.get("Tags") or []:
                key = item.get("Key") or item.get("TagKey")
                if key:
                    result[str(key)] = str(item.get("Value") or item.get("TagValue") or "")

        elif self.provider == "huaweicloud":
            tags = raw.get("tags") or []
            if isinstance(tags, list):
                for item in tags:
                    if isinstance(item, dict):
                        key = item.get("key") or item.get("Key")
                        if key:
                            result[str(key)] = str(item.get("value") or item.get("Value") or "")
                    elif isinstance(item, str) and "=" in item:
                        key, value = item.split("=", 1)
                        result[str(key)] = str(value)

        if include_runtime and resource.status:
            result.setdefault("status", resource.status)
        if include_runtime and resource.service:
            result.setdefault("service", resource.service)

        return result

    def _extract_ips(self, resource):
        raw = resource.raw if isinstance(resource.raw, dict) else {}
        ips = [resource.private_ip, resource.public_ip]

        if self.provider == "aliyun":
            ips.extend(_dict_get(raw, "VpcAttributes", "PrivateIpAddress", "IpAddress") or [])
            ips.extend(_dict_get(raw, "InnerIpAddress", "IpAddress") or [])
            ips.extend(_dict_get(raw, "PublicIpAddress", "IpAddress") or [])
            eip = _dict_get(raw, "EipAddress", "IpAddress")
            if eip:
                ips.append(eip)

        elif self.provider == "tencentcloud":
            ips.extend(raw.get("PrivateIpAddresses") or [])
            ips.extend(raw.get("PublicIpAddresses") or [])
            ips.extend(raw.get("IPv6Addresses") or [])

        elif self.provider == "huaweicloud":
            for addresses in (raw.get("addresses") or {}).values():
                for address in addresses or []:
                    addr = address.get("addr")
                    if addr:
                        ips.append(addr)

        return _unique_list(ips)

    @staticmethod
    def _flavor_id(resource, spec):
        base_id = spec["id"] or "custom:{}:{}:{}".format(
            spec["cpu"],
            spec["memory_mib"],
            spec["disk_gib"],
        )
        if resource.region:
            return "{}@{}".format(resource.region, base_id)
        return base_id


class RalphSyncClient(object):
    def __init__(self, config=None):
        self.config = config or {}

    @property
    def enabled(self):
        return bool(
            self.config.get("enabled")
            or (self.config.get("base_url") and self.config.get("cloud_provider_id"))
            or self.config.get("endpoint")
        )

    def push(self, payload):
        endpoint = self._endpoint()
        headers = copy.deepcopy(self.config.get("headers") or {})
        headers.setdefault("Content-Type", "application/json")
        timeout = _to_int(self.config.get("timeout"), default=15)
        verify = self.config.get("verify", True)
        auth = self._auth(headers, self.config.get("authorization") or {})

        response = requests.post(
            endpoint,
            json=payload,
            headers=headers or None,
            timeout=timeout,
            verify=verify,
            auth=auth,
        )
        if response.status_code not in {200, 201, 202, 204}:
            raise ValueError(
                "Ralph sync failed status={} body={}".format(
                    response.status_code,
                    (response.text or "")[:500],
                )
            )

        return {
            "endpoint": endpoint,
            "status_code": response.status_code,
        }

    def _endpoint(self):
        if self.config.get("endpoint"):
            return self.config["endpoint"]

        base_url = (self.config.get("base_url") or "").rstrip("/")
        cloud_provider_id = self.config.get("cloud_provider_id")
        if not base_url or cloud_provider_id in (None, ""):
            raise ValueError("Ralph sync requires base_url and cloud_provider_id")

        return "{}/cloudsync/{}/".format(base_url, cloud_provider_id)

    @staticmethod
    def _auth(headers, authorization):
        authorization = copy.deepcopy(authorization) if isinstance(authorization, dict) else {}
        auth_type = (authorization.get("type") or "").lower()
        if auth_type in {"basic", "basicauth"}:
            return HTTPBasicAuth(authorization.get("username"), authorization.get("password"))

        if auth_type == "bearer" and authorization.get("token"):
            headers["Authorization"] = "Bearer {}".format(authorization["token"])
        elif auth_type == "token" and authorization.get("token"):
            headers["Authorization"] = "Token {}".format(authorization["token"])
        elif auth_type == "apikey" and authorization.get("key"):
            headers[authorization["key"]] = authorization.get("value") or ""

        return None


PROVIDER_COLLECTORS = {
    "aliyun": AliyunCollector,
    "tencentcloud": TencentCloudCollector,
    "huaweicloud": HuaweiCloudCollector,
    "aws": AWSCollector,
}


class CloudAccountSyncer(object):
    def __init__(self, account_id):
        self.account_id = account_id

    def sync(self):
        from api.lib.cmdb.auto_discovery.auto_discovery import AutoDiscoveryCICRUD
        from api.lib.cmdb.auto_discovery.auto_discovery import decrypt_account

        account = AutoDiscoveryAccount.get_by_id(self.account_id)
        if account is None:
            return {"account_id": self.account_id, "error": "account not found"}

        provider = get_provider_by_rule(account.adr_id)
        if provider not in PROVIDER_COLLECTORS:
            return {"account_id": account.id, "provider": provider, "synced": 0, "error": "unsupported provider"}

        config = copy.deepcopy(account.config or {})
        decrypt_account(config, account.uid)
        collector = PROVIDER_COLLECTORS[provider](account, config)
        if not collector.key or not collector.secret:
            return {"account_id": account.id, "provider": provider, "synced": 0, "error": "key/secret is incomplete"}

        adt = ensure_cloud_adt(account, provider)
        resources = collector.collect()
        resource_adts = self._get_resource_adts(account, provider, exclude_adt_id=adt.id if adt else None)

        synced = 0
        failed = 0
        seen_values = set()
        resource_seen_values = {item.id: set() for item in resource_adts}
        for resource in resources:
            if not resource.resource_id:
                continue

            seen_values.add(resource.unique_value)
            try:
                adc = AutoDiscoveryCICRUD().upsert(
                    type_id=adt.type_id,
                    adt_id=adt.id,
                    unique_value=resource.unique_value,
                    instance=resource.as_instance(),
                )
                # 只有配置了 auto_accept 才自动接受，否则让资源停留在自动发现池
                if adc is not None and not adc.is_accept and adt.auto_accept:
                    AutoDiscoveryCICRUD.accept(adc, nickname="cloud_sync")
                synced += 1
            except Exception as e:
                failed += 1
                current_app.logger.error("sync cloud resource {} failed: {}".format(resource.unique_value, e))

            self._sync_resource_adts(resource_adts, resource_seen_values, resource)

        self._mark_missing(adt, seen_values)
        for resource_adt in resource_adts:
            self._mark_missing(resource_adt, resource_seen_values.get(resource_adt.id) or set())

        # Ralph同步相关代码（暂时禁用）
        # ralph_result = self._sync_to_ralph(account, provider, config, resources)
        ralph_result = {"enabled": False}  # 暂时禁用Ralph同步

        stdout = "cloud sync {} account={} synced={} failed={}".format(
            provider, account.name, synced, failed
        )
        if ralph_result.get("enabled"):
            stdout = "{} ralph_status={}".format(stdout, ralph_result.get("status"))
        AutoDiscoveryExecHistory.create(
            type_id=adt.type_id,
            stdout=stdout,
        )
        result = {
            "account_id": account.id,
            "provider": provider,
            "synced": synced,
            "failed": failed,
            "total_seen": len(seen_values),
        }
        if ralph_result:
            result["ralph"] = ralph_result
        return result

    @staticmethod
    def _get_resource_adts(account, provider, exclude_adt_id=None):
        result = []
        for adt in AutoDiscoveryCIType.get_by(adr_id=account.adr_id, to_dict=False):
            if exclude_adt_id is not None and adt.id == exclude_adt_id:
                continue
            if not adt.enabled:
                continue

            extra_option = adt.extra_option or {}
            if extra_option.get("provider") and extra_option.get("provider") != provider:
                continue
            if extra_option.get("_reference") not in (None, account.id):
                continue
            if not extra_option.get("category"):
                continue

            result.append(adt)

        return result

    @staticmethod
    def _sync_resource_adts(resource_adts, resource_seen_values, resource):
        from api.lib.cmdb.auto_discovery.auto_discovery import AutoDiscoveryCICRUD

        if resource.resource_type not in COMPUTE_RESOURCE_TYPES:
            return
        if not isinstance(resource.raw, dict) or not resource.raw:
            return

        for adt in resource_adts:
            extra_option = adt.extra_option or {}
            if extra_option.get("category") != resource.service:
                continue

            resource_seen_values.setdefault(adt.id, set()).add(resource.unique_value)
            try:
                adc = AutoDiscoveryCICRUD().upsert(
                    type_id=adt.type_id,
                    adt_id=adt.id,
                    unique_value=resource.unique_value,
                    instance=copy.deepcopy(resource.raw),
                )
                if adc is not None and not adc.is_accept:
                    AutoDiscoveryCICRUD.accept(adc, nickname="cloud_sync")
            except Exception as e:
                current_app.logger.error(
                    "sync resource {} to adt {} failed: {}".format(resource.unique_value, adt.id, e)
                )

    @staticmethod
    def _mark_missing(adt, seen_values):
        if not seen_values:
            return

        for adc in AutoDiscoveryCI.get_by(adt_id=adt.id, to_dict=False):
            if adc.unique_value in seen_values:
                continue
            instance = copy.deepcopy(adc.instance or {})
            instance["cloud_status"] = "missing"
            adc.update(instance=instance, filter_none=False)

    @staticmethod
    def _sync_to_ralph(account, provider, config, resources):
        """
        同步到Ralph系统（暂时禁用）
        用途：将云资源推送到Ralph DCIM系统
        状态：暂时禁用，保留最原始功能
        """
        return {"enabled": False}  # 暂时禁用，直接返回未启用状态

        # ralph_config = copy.deepcopy(config.get("ralph") or {})
        # client = RalphSyncClient(ralph_config)
        # if not client.enabled:
        #     return {"enabled": False}
        #
        # payload = RalphNormalizedPayloadBuilder(account, provider, ralph_config).build(resources)
        # summary = {
        #     "enabled": True,
        #     "status": "success",
        #     "mode": payload["sync"]["mode"],
        #     "projects": len(payload.get("projects", [])),
        #     "flavors": len(payload.get("flavors", [])),
        #     "hosts": len(payload.get("hosts", [])),
        # }
        #
        # try:
        #     summary.update(client.push(payload))
        # except Exception as e:
        #     current_app.logger.error(
        #         "sync ralph cloud provider failed account={} provider={} error={}".format(
        #             account.name, provider, e
        #         )
        #     )
        #     summary["status"] = "failed"
        #     summary["error"] = str(e)
        #
        # return summary


def sync_all_cloud_accounts(adr_id=None):
    query = AutoDiscoveryAccount.get_by(to_dict=False, only_query=True)
    if adr_id is not None:
        query = query.filter(AutoDiscoveryAccount.adr_id == adr_id)

    result = []
    for account in query.all():
        try:
            result.append(CloudAccountSyncer(account.id).sync())
        except Exception as e:
            current_app.logger.error("sync cloud account {} failed: {}\n{}".format(
                account.id, e, traceback.format_exc()))
            result.append({"account_id": account.id, "error": str(e)})

    return result
