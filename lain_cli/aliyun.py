import re

from aliyunsdkcore.acs_exception.exceptions import ServerException
from aliyunsdkcore.client import AcsClient
from aliyunsdkcr.request.v20160607 import GetRepoTagsRequest

from lain_cli.utils import (
    RegistryUtils,
    jalo,
    tell_cluster,
    tell_cluster_info,
    warn,
)


class AliyunRegistry(RegistryUtils):
    def __init__(
        self,
        access_key_id=None,
        access_key_secret=None,
        region_id=None,
        repo_namespace=None,
    ):
        if not all([access_key_id, access_key_secret, region_id, repo_namespace]):
            cluster_info = tell_cluster_info()
            access_key_id = cluster_info.get('access_key_id')
            access_key_secret = cluster_info.get('access_key_secret')
            if not all([access_key_id, access_key_secret]):
                cluster = tell_cluster()
                raise ValueError(
                    f'access_key_id, access_key_secret not provided in cluster_info, lain use {cluster} again to see what\'s wrong'
                )
            if not all([region_id, repo_namespace]):
                host = cluster_info['registry']
                _, region_id, _, _, repo_namespace = re.split(r'[\./]', host)

        self.host = f'registry.{region_id}.aliyuncs.com/{repo_namespace}'
        self.acs_client = AcsClient(access_key_id, access_key_secret, region_id)
        self.repo_namespace = repo_namespace
        self.endpoint = f'cr.{region_id}.aliyuncs.com'

    def list_tags(self, repo_name, **kwargs):
        request = GetRepoTagsRequest.GetRepoTagsRequest()
        request.set_RepoNamespace(self.repo_namespace)
        request.set_RepoName(repo_name)
        request.set_endpoint(self.endpoint)
        request.set_PageSize(100)
        try:
            response = self.acs_client.do_action_with_exception(request)
        except ServerException as e:
            if e.http_status == 404:
                return None
            if e.http_status == 400:
                warn(f'error during aliyun api query: {e}')
                return None
            raise
        tags_data = jalo(response)['data']['tags']
        tags = [d['tag'] for d in tags_data]
        return tags
