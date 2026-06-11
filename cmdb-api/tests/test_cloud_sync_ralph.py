# -*- coding: utf-8 -*-

from types import SimpleNamespace

from api.lib.cmdb.auto_discovery.cloud_sync import CloudResource
from api.lib.cmdb.auto_discovery.cloud_sync import RalphNormalizedPayloadBuilder
from api.lib.cmdb.auto_discovery.cloud_sync import RalphSyncClient


def test_build_ralph_payload_for_aliyun_compute():
    account = SimpleNamespace(id=7, name='aliyun-prod')
    resource = CloudResource(
        provider='aliyun',
        account=account,
        region='cn-hangzhou',
        resource_type='ecs',
        resource_id='i-abc123',
        name='app-1',
        service='云服务器 ECS',
        status='Running',
        private_ip='10.0.0.10',
        public_ip='47.0.0.10',
        raw={
            'InstanceType': 'ecs.g6.large',
            'Cpu': 2,
            'Memory': 4096,
            'ImageId': 'ubuntu_22_04_x64_20G_alibase',
            'ResourceGroupId': 'rg-prod',
            'SystemDisk': {'Size': 40},
            'DataDisk': [{'Size': 100}],
            'Tags': {'Tag': [{'TagKey': 'env', 'TagValue': 'prod'}]},
            'VpcAttributes': {'PrivateIpAddress': {'IpAddress': ['10.0.0.10']}},
            'PublicIpAddress': {'IpAddress': ['47.0.0.10']},
        },
    )

    payload = RalphNormalizedPayloadBuilder(
        account,
        'aliyun',
        {
            'account_id': '1234567890',
            'account_name': 'aliyun-prod',
            'delete_missing': True,
        },
    ).build([resource])

    assert payload['schema'] == 'ralph.multicloud.normalized'
    assert payload['account']['id'] == '1234567890'
    assert payload['sync'] == {'mode': 'full', 'delete_missing': True}
    assert payload['projects'][0]['id'] == 'resource-group:rg-prod'
    assert payload['flavors'][0]['id'] == 'cn-hangzhou@ecs.g6.large'
    assert payload['flavors'][0]['memory_mib'] == 4096
    assert payload['hosts'][0]['id'] == 'i-abc123'
    assert payload['hosts'][0]['project_id'] == 'resource-group:rg-prod'
    assert payload['hosts'][0]['flavor_id'] == 'cn-hangzhou@ecs.g6.large'
    assert payload['hosts'][0]['disk_gib'] == 140
    assert payload['hosts'][0]['ips'] == ['10.0.0.10', '47.0.0.10']


def test_ralph_sync_client_uses_cloudsync_endpoint_and_token(monkeypatch):
    captured = {}

    class DummyResponse(object):
        status_code = 204
        text = ''

    def fake_post(url, json=None, headers=None, timeout=None, verify=None, auth=None):
        captured['url'] = url
        captured['json'] = json
        captured['headers'] = headers
        captured['timeout'] = timeout
        captured['verify'] = verify
        captured['auth'] = auth
        return DummyResponse()

    monkeypatch.setattr('api.lib.cmdb.auto_discovery.cloud_sync.requests.post', fake_post)

    result = RalphSyncClient({
        'base_url': 'http://127.0.0.1:8088',
        'cloud_provider_id': 6,
        'timeout': 9,
        'verify': False,
        'authorization': {'type': 'token', 'token': 'ralph-token'},
    }).push({
        'schema': 'ralph.multicloud.normalized',
        'schema_version': '1.0',
        'provider': 'aliyun',
        'account': {'id': '1', 'name': 'demo'},
        'sync': {'mode': 'full', 'delete_missing': False},
    })

    assert result['endpoint'] == 'http://127.0.0.1:8088/cloudsync/6/'
    assert captured['url'] == 'http://127.0.0.1:8088/cloudsync/6/'
    assert captured['headers']['Authorization'] == 'Token ralph-token'
    assert captured['timeout'] == 9
    assert captured['verify'] is False
    assert captured['auth'] is None
