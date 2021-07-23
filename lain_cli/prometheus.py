import json
from datetime import datetime, timedelta, timezone
from statistics import quantiles

import click
from humanfriendly import parse_timespan

from lain_cli.utils import RequestClientMixin, ensure_str, tell_cluster_info, warn

LAIN_LINT_PROMETHEUS_QUERY_RANGE = '2d'
LAIN_LINT_PROMETHEUS_QUERY_STEP = int(
    int(parse_timespan(LAIN_LINT_PROMETHEUS_QUERY_RANGE)) / 1440
)


class Prometheus(RequestClientMixin):
    timeout = 20

    def __init__(self, endpoint=None):
        if not endpoint:
            cluster_info = tell_cluster_info()
            endpoint = cluster_info.get('prometheus')
            if not endpoint:
                raise click.Abort(f'prometheus not provided in cluster: {cluster_info}')

        self.endpoint = endpoint

    @staticmethod
    def format_time(dt):
        if isinstance(dt, str):
            return dt
        return dt.isoformat()

    def query_cpu(self, appname, proc_name, **kwargs):
        cluster_info = tell_cluster_info()
        query_template = cluster_info.get('pql_template', {}).get('cpu')
        if not query_template:
            raise ValueError('pql_template.cpu not configured in cluster_info')
        q = query_template.format(
            appname=appname, proc_name=proc_name, range=LAIN_LINT_PROMETHEUS_QUERY_RANGE
        )
        kwargs.setdefault('step', LAIN_LINT_PROMETHEUS_QUERY_STEP)
        kwargs['end'] = datetime.now(timezone.utc)
        res = self.query(q, **kwargs)
        return res

    def cpu_p95(self, appname, proc_name, **kwargs):
        cpu_result = self.query_cpu(appname, proc_name)
        # [{'metric': {}, 'value': [1595486084.053, '4.990567343235413']}]
        if cpu_result:
            cpu_top_list = [int(float(p[-1])) for p in cpu_result[0]['values']]
            cnt = len(cpu_top_list)
            if cpu_top_list.count(0) / cnt > 0.7:
                warn(f'lint suggestions might not be accurate for {proc_name}')

            cpu_top = int(quantiles(cpu_top_list, n=10)[-1])
        else:
            cpu_top = 5

        return max([cpu_top, 5])

    def memory_p95(self, appname, proc_name, **kwargs):
        cluster_info = tell_cluster_info()
        query_template = cluster_info.get('pql_template', {}).get('memory_p95')
        if not query_template:
            raise ValueError('pql_template.memory_p95 not configured in cluster_info')
        q = query_template.format(
            appname=appname, proc_name=proc_name, range=LAIN_LINT_PROMETHEUS_QUERY_RANGE
        )
        kwargs.setdefault('step', LAIN_LINT_PROMETHEUS_QUERY_STEP)
        res = self.query(q, **kwargs)
        if not res:
            return
        # [{'metric': {}, 'value': [1583388354.31, '744079360']}]
        memory_p95 = int(float(res[0]['value'][-1]))
        return memory_p95

    def query(self, query, start=None, end=None, step=None, timeout=20):
        # https://prometheus.io/docs/prometheus/latest/querying/api/#range-queries
        data = {
            'query': query,
            'timeout': timeout,
        }
        if start or end:
            if not start:
                start = end - timedelta(days=1)

            if not end:
                end = datetime.now(timezone.utc).isoformat()

            if not step:
                step = 60

            path = '/api/v1/query_range'
            data.update(
                {
                    'start': self.format_time(start),
                    'end': self.format_time(end),
                    'step': step,
                }
            )
        else:
            path = '/api/v1/query'

        res = self.post(path, data=data)
        try:
            responson = res.json()
        except json.decoder.JSONDecodeError as e:
            raise ValueError(
                'cannot decode this shit: {}'.format(ensure_str(res.text))
            ) from e
        if responson.get('status') == 'error':
            raise ValueError(responson['error'])
        return responson['data']['result']
