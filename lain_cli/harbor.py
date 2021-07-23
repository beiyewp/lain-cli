from lain_cli.utils import (
    RegistryUtils,
    RequestClientMixin,
    flatten_list,
    tell_cluster,
    tell_cluster_info,
)


class HarborRegistry(RequestClientMixin, RegistryUtils):
    def __init__(self, registry_url=None, token=None):
        if not all([registry_url, token]):
            cluster_info = tell_cluster_info()
            registry_url = cluster_info['registry']
            if 'harbor_token' not in cluster_info:
                cluster = tell_cluster()
                raise ValueError(
                    f'harbor_token not provided in cluster_info, lain use {cluster} again to see what\'s wrong'
                )
            token = cluster_info['harbor_token']

        self.host = registry_url
        host, project = registry_url.split('/')
        self.endpoint = f'http://{host}/api/v2.0'
        self.headers = {
            # get from your harbor console
            'authorization': f'Basic {token}',
            'accept': 'application/json',
        }
        self.project = project

    def list_repos(self):
        res = self.get(f'/projects/{self.project}/repositories')
        responson = res.json()
        return responson

    def list_tags(self, appname, **kwargs):
        res = self.get(
            f'/projects/{self.project}/repositories/{appname}/artifacts',
            params={'page_size': 50},
        )
        responson = res.json()
        tag_dics = flatten_list([dic['tags'] for dic in responson if dic['tags']])
        tags = [tag['name'] for tag in tag_dics]
        return tags
