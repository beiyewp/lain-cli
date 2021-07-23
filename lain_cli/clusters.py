from types import MappingProxyType


SENTRY_DSN = ''
# MappingProxyType is like an immutable dict
CLUSTERS = MappingProxyType(
    {
        'test': MappingProxyType(
            {
                # you will publish lain to your internal pypi index, if you
                # have one
                'pypi_index': 'https://pypi.example.com/pypi/simple/',
                # extra index to use when installing lain through pip
                'pypi_extra_index': 'https://mirrors.cloud.tencent.com/pypi/simple/',
                # docker registry for this cluster
                'registry': 'registry.example.com',
                # default to the docker registry 2.0
                # also supports aliyun, tencent, harbor
                'registry_type': 'registry',
                # prometheus url, for monitoring related functionalities
                'prometheus': 'http://prometheus.example.com',
                # pql query to use when executing cpu / memory queries
                'pql_template': {
                    'cpu': '''max(
                    rate(container_cpu_user_seconds_total{{container!="sandbox",pod=~"{appname}-{proc_name}-[[:alnum:]]+-.+"}}[{range}]) * 1000
                )''',
                    'memory_p95': '''max(
                    quantile_over_time(0.95,
                    container_memory_usage_bytes{{container!="sandbox",pod=~"{appname}-{proc_name}-[[:alnum:]]+-.+"}}[{range}]
                )
                    -
                    quantile_over_time(0.95,
                    container_memory_cache{{container!="sandbox",pod=~"{appname}-{proc_name}-[[:alnum:]]+-.+"}}[{range}]
                ))''',
                },
                # print grafana urls for app using lain status -s
                'grafana_url': 'http://grafana.example.com/d/7sl4vJAZk/docker-monitoring',
                # open kibana log url, if supported
                'kibana': 'kibana.example.com',
                # for gitlab integration, like lain fetch-chart
                'gitlab': 'http://git.example.com',
                # domain for this cluster, will be used in ingress declarations
                'domain': 'test.example.com',
                # kubernetes namespace to use
                'namespace': 'default',
            },
        ),
    }
)
