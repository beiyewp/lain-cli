import asyncio
import tarfile
import base64
import inspect
import itertools
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from contextlib import contextmanager, suppress
from copy import deepcopy
from functools import lru_cache
from hashlib import blake2b
from inspect import cleandoc
from numbers import Number
from os import getcwd as cwd
from os import readlink, remove
from os.path import abspath, basename, dirname, expanduser, isdir, isfile, join
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import sleep

import click
import requests
from click import BadParameter
from humanfriendly import (
    CombinedUnit,
    SizeUnit,
    parse_size,
    parse_timespan,
    round_number,
)
from jinja2 import Environment, FileSystemLoader
from marshmallow import INCLUDE, Schema, ValidationError, post_load, validates
from marshmallow.fields import Dict, Function, Int, List, Nested, Raw, Str
from marshmallow.validate import OneOf
from packaging import version
from pip._internal.index.collector import LinkCollector
from pip._internal.index.package_finder import PackageFinder
from pip._internal.models.search_scope import SearchScope
from pip._internal.models.selection_prefs import SelectionPreferences
from pip._internal.network.session import PipSession
from requests.exceptions import RequestException
from ruamel import yaml

from lain_cli import __version__
from lain_cli.clusters import CLUSTERS

# safe to delete when release is in this state
HELM_WEIRD_STATE = {'failed', 'pending-install'}
CLI_DIR = dirname(abspath(__file__))
TEMPLATE_DIR = join(CLI_DIR, 'templates')
CHART_TEMPLATE_DIR = join(CLI_DIR, 'chart_template')
INTERNAL_CLUSTER_VALUES_DIR = join(CLI_DIR, 'cluster_values')
template_env = Environment(
    trim_blocks=True,
    lstrip_blocks=True,
    loader=FileSystemLoader([CHART_TEMPLATE_DIR, TEMPLATE_DIR]),
    extensions=['jinja2.ext.loopcontrols'],
)
CHART_DIR_NAME = 'chart'
CHART_VERSION = version.parse('0.1.9')
ENV = os.environ.copy()
LOOKOUT_ENV = {'http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'}
LAIN_EXBIN_PREFIX = ENV.get('LAIN_EXBIN_PREFIX') or '/usr/local/bin'
HELM_MIN_VERSION_STR = 'v3.6.3'
HELM_MIN_VERSION = version.parse(HELM_MIN_VERSION_STR)
STERN_MIN_VERSION_STR = '1.11.0'
STERN_MIN_VERSION = version.parse(STERN_MIN_VERSION_STR)
KUBECTL_MIN_VERSION = version.parse('v1.18.0')
ENV['PATH'] = f'{LAIN_EXBIN_PREFIX}:{ENV["PATH"]}'
TIMESTAMP_PATTERN = re.compile(r'\d+')
LAIN_META_PATTERN = re.compile(r'\d{10,}-\w{40}$')
KUBERNETES_MIN_MEMORY = parse_size('4MiB', binary=True)
# lain build config
DEFAULT_WORKDIR = '/lain/app'
DOCKER_COMPOSE_FILE_PATH = 'docker-compose.yaml'
DOCKERFILE_NAME = 'Dockerfile'
DOCKERIGNORE_NAME = '.dockerignore'
BUILD_STAGES = {'prepare', 'build', 'release'}
PROTECTED_REPO_KEYWORDS = ('centos',)
RECENT_TAGS_COUNT = 10
BIG_DEPLOY_REPLICA_COUNT = 3
INGRESS_CANARY_ANNOTATIONS = {
    'nginx.ingress.kubernetes.io/canary-by-header',
    'nginx.ingress.kubernetes.io/canary-by-header-value',
    'nginx.ingress.kubernetes.io/canary-by-header-pattern',
    'nginx.ingress.kubernetes.io/canary-by-cookie',
    'nginx.ingress.kubernetes.io/canary-weight',
}


def click_parse_timespan(ctx, param, value):
    if not value:
        return
    if isinstance(value, Number):
        return int(value)
    return int(parse_timespan(value))


def recursive_update(d, u):
    """
    >>> recursive_update({'foo': {'spam': 'egg'}, 'should': 'preserve'}, {'foo': {'bar': 'egg'}})
    {'foo': {'spam': 'egg', 'bar': 'egg'}, 'should': 'preserve'}
    >>> recursive_update({'foo': 'xxx'}, {'foo': {'bar': 'egg'}})
    {'foo': {'bar': 'egg'}}
    """
    if not u:
        return d
    for k, v in u.items():
        if type(d.get(k)) is not type(v):
            d[k] = v
        elif isinstance(v, Mapping):
            d[k] = recursive_update(d.get(k, {}), v)
        else:
            d[k] = v
    return d


def quote(s):
    return shlex.quote(s)


def diff_dict(old, new):
    """
    >>> diff_dict({'del': '0', 'change': '0', 'stay': '0'}, {'change': '1', 'stay': '0', 'add': '0'})
    {'added': ['add'], 'removed': ['del'], 'changed': ['change']}
    """
    all_keys = set(old) | set(new)
    diff = {'added': [], 'removed': [], 'changed': []}
    for k in all_keys:
        lv = old.get(k)
        rv = new.get(k)
        if not lv:
            diff['added'].append(k)
        elif not rv:
            diff['removed'].append(k)
        elif lv != rv:
            diff['changed'].append(k)

    return diff


def context(silent=False):
    return click.get_current_context(silent=silent)


def excall(s, silent=None):
    """lain cli often calls other cli, might wanna notify the user what's being
    run"""
    # when running tests, this function will be invoked without a active click
    # context personally i hate adding extra handling in business code just to
    # take care of testing, forgive me because there's gonna be much more work
    # otherwise
    ctx = context(silent=True)
    if silent or ctx and ctx.obj.get('silent'):
        return
    if not isinstance(s, str):
        s = subprocess.list2cmdline(s)

    click.echo(click.style(s, fg='bright_yellow'), err=True)


def ensure_str(s):
    try:
        return s.decode('utf-8')
    except Exception:
        return str(s)


def echo(s, fg=None, exit=None, err=False, mark_error=False, clean=True):
    s = ensure_str(s)
    if clean:
        s = cleandoc(s)

    click.echo(click.style(s, fg=fg), err=err)
    ctx = context(silent=True)
    if ctx:
        if err and mark_error:
            ctx.obj['last_error'] = s

        if isinstance(exit, bool):
            if exit:
                ctx.exit(0)
        elif isinstance(exit, int):
            ctx.exit(exit)


def goodjob(s, exit=None, **kwargs):
    if exit:
        exit = 0

    return echo(s, fg='green', exit=exit, err=True, **kwargs)


def warn(s, exit=None, **kwargs):
    if exit:
        exit = 1

    return echo(s, fg='magenta', exit=exit, err=True, **kwargs)


def debug(s, exit=None, **kwargs):
    ctx = context(silent=True)
    if ctx and not ctx.obj.get('verbose'):
        return
    if exit:
        exit = 1

    return echo(s, fg='black', exit=exit, err=True, **kwargs)


def error(s, exit=None, **kwargs):
    if exit:
        exit = 1

    return echo(s, fg='red', exit=exit, err=True, mark_error=True, **kwargs)


def flatten_list(nested_list):
    return list(itertools.chain.from_iterable(nested_list))


def must_get_env(name, fail_msg=''):
    val = ENV.get(name)
    if not val:
        error(f'environment variable {name} not defined: {fail_msg}', exit=True)

    return val


def tell_pods_count():
    ctx = context()
    values = ctx.obj['values']
    count = sum(proc.get('replicaCount', 1) for proc in values['deployments'].values())
    return count


def tell_pod_deploy_name(s):
    """
    >>> tell_pod_deploy_name('dummy-web-dev-7557696ddf-52cc6')
    'dummy-web-dev'
    >>> tell_pod_deploy_name('dummy-web-7557696ddf-52cc6')
    'dummy-web'
    """
    return s.rsplit('-', 2)[0]


def tell_ingress_urls():
    ctx = context()
    cluster = ctx.obj['cluster']
    values = ctx.obj['values']
    ingresses = values.get('ingresses') or []
    cluster_info = tell_cluster_info(cluster)
    domain = cluster_info['domain']

    def make_external_url(ing):
        host = ing['host']
        url = f'http://{host}'
        ingress_external_port = cluster_info.get('ingress_external_port', 80)
        if ingress_external_port != 80:
            url += f':{ingress_external_port}'

        for path in ing['paths']:
            yield url + path

    def make_internal_url(ing):
        """internal ingress host can be either full domain or just the first
        part (usually appname)"""
        host = ing['host']
        url = f'http://{host}' if '.' in host else f'http://{host}.{domain}'
        ingress_internal_port = cluster_info.get('ingress_internal_port', 80)
        if ingress_internal_port != 80:
            url += f':{ingress_internal_port}'

        for path in ing['paths']:
            yield url + path

    part1 = itertools.chain.from_iterable([make_internal_url(i) for i in ingresses])
    externalIngresses = values.get('externalIngresses') or []
    part2 = itertools.chain.from_iterable(
        make_external_url(i) for i in externalIngresses
    )
    return list(part1) + list(part2)


def parse_ready(ready_str):
    """
    >>> parse_ready('0/1')
    False
    >>> parse_ready('1/1')
    True
    """
    left, right = ready_str.split('/')
    if left != right:
        return False
    return True


def get_pods(appname=None, headers=False, show_only_bad_pods=None, check=False):
    cmd = [
        'get',
        'pod',
        '-o=wide',
    ]
    if appname:
        cmd.append(f'-lapp.kubernetes.io/name={appname}')

    res = kubectl(*cmd, capture_output=True, check=check)
    pods = ensure_str(res.stdout).splitlines()
    if not show_only_bad_pods:
        if headers:
            return res, pods
        return res, pods[1:]
    header = pods.pop(0)
    bad_pods = []
    for line in pods:
        pod_name, _, state, *_ = line.split()
        for podline in pods[1:]:
            # ['deploy-x-x', '1/1', 'Running', '0', '6h6m', '192.168.0.13', 'node-1', '<none>', '1/1']
            _, ready_str, status, restarts, *_ = podline.split()
            if status == 'Completed':
                # job pods will be ignored
                continue
            if not parse_ready(ready_str):
                bad_pods.append(podline)
                continue
            if status not in {'Running', 'Terminating', 'ContainerCreating'}:
                # 状态异常的 pods 是我们最为关心的, 因此塞到头部方便取用
                bad_pods.insert(1, podline)
                continue
            if int(restarts) > 10:
                # 本来时不时就会重启节点, 造成容器重启, 因此设置个小阈值, 过滤噪声
                bad_pods.append(podline)
                continue

    if headers:
        return res, [header] + bad_pods
    return res, bad_pods


def pick_pod(deploy_name=None, phase=None, containerStatuses=None):
    ctx = context()
    appname = ctx.obj['appname']
    cmd = ['get', 'pod', '-o=json']
    if deploy_name:
        cmd.extend(['-l', f'app.kubernetes.io/instance={appname}-{deploy_name}'])
    else:
        cmd.extend(['-l', f'app.kubernetes.io/name={appname}'])

    if phase:
        cmd.extend([f'--field-selector=status.phase=={phase}'])

    res = kubectl(*cmd, capture_output=True, check=False)
    stdout = res.stdout
    if not stdout or rc(res):
        return
    responson = jalo(res.stdout)
    if containerStatuses:
        if not isinstance(containerStatuses, set):
            containerStatuses = {containerStatuses}

        # 这个数据的 parsing 真不用看, 随便 k get po -ojson
        # 看眼结构就知道怎么做 parsing 了, 不用怪我不封装
        items = [
            item
            for item in responson['items']
            if containerStatuses.intersection(
                set(
                    (
                        status['state'].get('waiting', {})
                        or status['state'].get('terminated', {})
                    ).get('reason')
                    for status in item['status'].get('containerStatuses', [])
                )
            )
        ]
    else:
        items = responson['items']

    items = sorted(items, key=lambda d: d['metadata']['creationTimestamp'])
    podnames = [item['metadata']['name'] for item in items]
    try:
        return podnames[-1]
    except IndexError:
        return


def tell_best_deploy():
    """deployment name with the most memory"""
    ctx = context()
    deploys = ctx.obj['values']['deployments']
    chosen = list(deploys.keys())[0]

    def mem_limits(deploy):
        mem_str = deploy.get('resources', {}).get('limits', {}).get('memory') or '1Gi'
        return parse_size(mem_str)

    for name, deploy in deploys.items():
        left = deploys[chosen]
        if mem_limits(deploy) > mem_limits(left):
            chosen = name

    return chosen


def deploy_toast(canary=False):
    ctx = context()
    ctx.obj.update(tell_cluster_info())
    if canary:
        template = template_env.get_template('canary-toast.txt.j2')
    else:
        ctx.obj['kibana_url'] = tell_kibana_url()
        template = template_env.get_template('deploy-toast.txt.j2')

    goodjob(template.render(**ctx.obj))


def tell_grafana_url():
    ctx = context()
    appname = ctx.obj['appname']
    cluster_info = tell_cluster_info()
    grafana_url = cluster_info.get('grafana_url')
    if grafana_url:
        return f'{grafana_url}?orgId=1&refresh=10s&var-label_app={appname}'


def open_kibana_url(appname=None, proc=None):
    url = tell_kibana_url(appname=appname, proc=proc)
    subprocess_run(['open', url])


def tell_kibana_url(appname=None, proc=None):
    if not appname:
        ctx = context()
        appname = ctx.obj['appname']

    cluster_info = tell_cluster_info()
    kibana_host = cluster_info.get('kibana')
    if not kibana_host:
        return
    q = f'{appname}-{proc}' if proc else appname
    url = f'http://{kibana_host}/app/logs/stream?logPosition=(end:now,start:now-30m,streamLive:!f)&logFilter=(expression:%27kubernetes.pod_name.keyword:{q}*%27,kind:kuery)'
    return url


too_much_logs_headsup_str = '''doesn't work for you, here's some tips:
    * use stern instead of kubectl logs, lain logs --stern
{%- if kibana %}
    * use kibana: {{ kibana_url }}
{%- endif %}
'''
too_much_logs_headsup_template = template_env.from_string(too_much_logs_headsup_str)


def too_much_logs_headsup():
    # kubectl cannot tail from more than 8 log streams, when that happens,
    # print a help message to redirect users to kibana, if applicable
    ctx = context()
    ctx.obj.update(tell_cluster_info())
    kibana_url = tell_kibana_url()
    headsup = too_much_logs_headsup_template.render(kibana_url=kibana_url, **ctx.obj)
    error(headsup)


init_done_str = f'''a helm chart is generated under the ./{CHART_DIR_NAME} directory. what's next?
* review ./{CHART_DIR_NAME}/values.yaml
* if this app needs cluster-specific secret files or env, you should create them:
    lain use [CLUSTER]
    # add env to Kubernetes Secret
    lain env edit
    # add secret files to Kubernetes Secret
    lain secret add [FILE]
* lain deploy
'''


def init_done_toast():
    goodjob(init_done_str)


template_update_done_str = '''helm chart template has been updated, commit the changes and get on with your life.'''


def template_update_toast():
    goodjob(template_update_done_str)


class RequestClientMixin:
    endpoint = None
    headers = {}
    timeout = 5

    def request(self, method, path=None, params=None, data=None, **kwargs):
        if not path:
            url = self.endpoint
        elif self.endpoint:
            url = self.endpoint + path
        else:
            raise ValueError('no endpoint specified')

        kwargs.setdefault('timeout', self.timeout)
        res = requests.request(
            method, url, headers=self.headers, params=params, data=data, **kwargs
        )
        return res

    def post(self, path=None, **kwargs):
        return self.request('POST', path, **kwargs)

    def get(self, path=None, **kwargs):
        return self.request('GET', path, **kwargs)

    def delete(self, path=None, **kwargs):
        return self.request('DELETE', path, **kwargs)

    def head(self, path=None, **kwargs):
        return self.request('HEAD', path, **kwargs)


class RegistryUtils:
    host = 'registry.fake/dev'

    @staticmethod
    def is_protected_repo(repo):
        for s in PROTECTED_REPO_KEYWORDS:
            if s in repo:
                return True
        return False

    @staticmethod
    def extra_image_timestamp(s):
        if s == 'latest':
            return sys.maxsize
        res = TIMESTAMP_PATTERN.search(s)
        ts = int(res.group()) if res else 0
        return ts

    @classmethod
    def sort_and_filter(cls, tags, n=RECENT_TAGS_COUNT):
        n = n or RECENT_TAGS_COUNT
        tags = [
            s for s in tags if not s.startswith('meta') and not s.startswith('prepare')
        ]
        sor = sorted(tags, reverse=True, key=cls.extra_image_timestamp)
        if n:
            return sor[:n]
        return sor

    def make_image(self, tag):
        ctx = context()
        repo = ctx.obj['appname']
        return f'{self.host}/{repo}:{tag}'


def tell_registry_client():
    cluster_info = tell_cluster_info()
    registry_type = cluster_info.get('registry_type') or 'registry'
    if registry_type == 'registry':
        from lain_cli.registry import Registry

        return Registry()
    if registry_type == 'aliyun':
        from lain_cli.aliyun import AliyunRegistry

        return AliyunRegistry()
    if registry_type == 'harbor':
        from lain_cli.harbor import HarborRegistry

        return HarborRegistry()
    if registry_type == 'tencent':
        from lain_cli.tencent import TencentRegistry

        return TencentRegistry()
    warn(f'unsupported registry type: {registry_type}')


def clean_kubernetes_manifests(yml):
    """remove irrelevant information from Kubernetes manifests"""
    yml.pop('status', '')
    metadata = yml.get('metadata', {})
    metadata.pop('creationTimestamp', '')
    metadata.pop('selfLink', '')
    metadata.pop('uid', '')
    metadata.pop('resourceVersion', '')
    metadata.pop('generation', '')
    metadata.pop('managedFields', '')
    annotations = metadata.get('annotations', {})
    annotations.pop('kubectl.kubernetes.io/last-applied-configuration', '')
    spec = yml.get('spec', {})
    spec.pop('clusterIP', None)


def dump_secret(secret_name, init='env'):
    """create a tempfile and dump plaintext secret into it"""
    secret_dic = tell_secret(secret_name, init=init)
    f = NamedTemporaryFile(suffix='.yaml')
    yadu(secret_dic, f)
    return f


def welcome_check(cluster=None):
    cluster_info = tell_cluster_info(cluster)
    checks = cluster_info.get('welcome_check', [])
    if callable(checks):
        checks = [checks]

    for check in checks:
        check()


def tell_cluster_info(cluster=None):
    ctx = context()
    if not cluster:
        cluster = ctx.obj['cluster']

    cluster_info = CLUSTERS.get(cluster)
    if not cluster_info:
        names = set(CLUSTERS.keys())
        error(f'unknown cluster {cluster}, choose from {names}', exit=True)

    ctx.obj['cluster_info'] = cluster_info
    return cluster_info


def tell_secret(secret_name, init='env'):
    """return Kubernetes secret object in python dict, all b64decoded.
    If secret doesn't exist, create one first, and with some example content"""

    res = kubectl(
        'get', 'secret', '-oyaml', secret_name, capture_output=True, check=False
    )
    if code := rc(res):
        stderr = ensure_str(res.stderr)
        if 'not found' in stderr:
            init_kubernetes_secret(secret_name, init=init)
            return tell_secret(secret_name, init=init)
        error(f'weird error: {stderr}', exit=code)

    dic = yalo(res.stdout)
    clean_kubernetes_manifests(dic)
    dic.setdefault('data', {})
    for fname, s in dic['data'].items():
        decoded = base64.b64decode(s).decode('utf-8') if s else ''
        # gotta do this so yaml.dump will print nicely
        dic['data'][fname] = literal(decoded) if '\n' in decoded else decoded

    return dic


def init_kubernetes_secret(secret_name, init='env'):
    d = TemporaryDirectory()
    if init == 'env':
        init_clause = '--from-literal=FOO=BAR'
    elif init == 'secret':
        example_file = 'topsecret.txt'
        example_file_path = join(d.name, example_file)
        with open(example_file_path, 'w') as f:
            f.write('I\nAM\nBATMAN')

        init_clause = f'--from-file={example_file_path}'
    else:
        raise ValueError(f'init style: env, secret. dont\'t know what this is: {init}')
    kubectl(
        'create',
        'secret',
        'generic',
        secret_name,
        init_clause,
        capture_output=True,
        check=True,
    )
    d.cleanup()  # don't wanna cleanup too early


def kubectl_edit(f, capture_output=False, notify_diff=True):
    webhook = None
    if notify_diff:
        from lain_cli.webhook import tell_webhook_client

        webhook = tell_webhook_client()
        if webhook:
            old = yalo(f)

    edit_file(f)
    try:
        secret_dic = yalo(f)
        if notify_diff:
            new = deepcopy(secret_dic)

        res = kubectl_apply(secret_dic, capture_output=capture_output)
    except (yaml.error.YAMLError, ValueError) as e:
        name = preserve_tempfile(f)
        err = f'''not a valid kubernetes secret file after edit:
            {e}

            don't worry, your work has been saved to: {name}'''
        error(err, exit=1)

    if rc(res):
        name = preserve_tempfile(f)
        err = f'''
        error during kubectl apply (read the above error).
        don't worry, your work has been saved to: {name}'''
        error(err, exit=1)

    if notify_diff and webhook:
        webhook.diff_k8s_secret(old, new)

    return res


def preserve_tempfile(f):
    name = f.name
    f.seek(0)
    content = f.read()
    f.close()
    with open(name, 'wb') as nf:
        nf.write(content)

    return name


def kubectl_apply(
    anything,
    validate=True,
    capture_output=False,
    check=True,
):
    """dump content into a temp yaml file, and then k apply.
    also if this thing is kubernetes secret, will try to b64encode"""
    if isinstance(anything, str):
        dic = yalo(anything)
    elif isinstance(anything, dict):
        dic = anything
    else:
        raise ValueError(
            f'argument must be dict or yaml / json string, got: {anything}'
        )
    if dic['kind'] == 'Secret':
        data = dic.get('data') or {}
        for k, s in data.items():
            try:
                dic['data'][k] = base64.b64encode(s.encode('utf-8')).decode('utf-8')
            except AttributeError as e:
                raise ValueError(
                    f'kubernetes secret data should be string, got {k}: {s}'
                ) from e

    debug('dumping kubernetes manifest:')
    debug(dic)
    f = NamedTemporaryFile(suffix='.yaml')
    yadu(dic, f)
    f.seek(0)
    validate = jadu(validate)
    res = kubectl(
        'apply',
        '-f',
        f.name,
        f'--validate={validate}',
        capture_output=capture_output,
        check=check,
    )
    return res


def tell_cluster_values_file(internal=False):
    """internal cluster values resides in lain4 package data, while app can
    define cluster values of their own"""
    cluster = tell_cluster()
    d = INTERNAL_CLUSTER_VALUES_DIR if internal else CHART_DIR_NAME
    values_file = join(d, f'values-{cluster}.yaml')
    if isfile(values_file):
        return values_file
    cluster_info = tell_cluster_info(cluster)
    default_values_file = join(d, cluster_info.get('default-values', ''))
    if isfile(default_values_file):
        return default_values_file


def tell_executor():
    exe = ENV.get('USER')
    if not exe:
        # gitlab ci job url, and the user who started this job
        gitlab_user_name = ENV.get('GITLAB_USER_NAME') or ''
        ci_job_url = ENV.get('CI_JOB_URL') or ''
        if gitlab_user_name:
            exe = '{} via {}'.format(gitlab_user_name, ci_job_url)
        else:
            exe = ci_job_url

    return exe


def tell_helm_options(kvpairs=None, deduce_image=True, canary=False, extra=()):
    """Sure you can override helm values, but I might not approve it"""
    kvpairs = kvpairs or ()
    ctx = context()
    cluster = ctx.obj['cluster']
    cluster_info = tell_cluster_info(cluster)
    registry = cluster_info.get('internal-registry') or cluster_info['registry']
    # 所有超载变量都塞进去, 除了 imageTag, 这玩意我得检查一下
    kvlist = [
        f'registry={registry}',
        f'cluster={cluster}',
        *[f'{k}={v}' for k, v in kvpairs if k != 'imageTag'],
    ]
    user = ENV.get('USER')
    if user:
        kvlist.append(f'user={user}')

    pair = next(((k, image_tag) for k, image_tag in kvpairs if k == 'imageTag'), None)
    if pair:
        _, image_tag = pair
        build_jit_challenge(image_tag)
    else:
        image_tag = None

    image_tag = tell_image_tag(image_tag) if deduce_image else image_tag
    if image_tag:
        kvlist.append(f'imageTag={image_tag}')
        ctx.obj['image_tag'] = image_tag
        if LAIN_META_PATTERN.match(image_tag):
            ctx.obj['git_revision'] = image_tag.split('-')[-1]

    def populate_from_cluster_info(field, dest=None, default=None):
        if not dest:
            dest = field

        if field in cluster_info:
            kvlist.extend([f'{dest}={cluster_info[field]}'])
        elif default is not None:
            kvlist.extend([f'{dest}={default}'])

    populate_from_cluster_info('namespace', 'k8s_namespace', default='default')
    populate_from_cluster_info('domain')

    set_clause = ','.join(kvlist)
    if isinstance(extra, str):
        extra = (extra,)
    else:
        extra = extra or ()

    options = ['--set', set_clause, *extra]

    internal_values_file = tell_cluster_values_file(internal=True)
    if internal_values_file:
        options.extend(['-f', internal_values_file])

    values_file = tell_cluster_values_file()
    if values_file:
        options.extend(['-f', values_file])

    extra_values_file = ctx.obj['extra_values_file']
    if extra_values_file:
        options.extend(['-f', extra_values_file.name])

    if canary:
        canary_values_file = create_canary_values()
        options.extend(['-f', canary_values_file])

    return options


def clean_canary_ingress_annotations(annotations):
    for k in INGRESS_CANARY_ANNOTATIONS:
        annotations.pop(k, None)


def make_canary_name(appname):
    return f'{appname}-canary'


def create_canary_values():
    template = template_env.get_template('values-canary.yaml.j2')
    canary_values_file = join(CHART_DIR_NAME, 'values-canary.yaml')
    ctx = context()
    with open(canary_values_file, 'w') as f:
        f.write(template.render(**ctx.obj))

    return canary_values_file


def delete_canary_values():
    canary_values_file = join(CHART_DIR_NAME, 'values-canary.yaml')
    ensure_absent(canary_values_file)


def tell_image_tag(image_tag=None):
    """really smart method to figure out which image_tag is the right one to deploy:
        1. if image_tag isn't provided, obtain from lain_meta
        2. check for existence against the specified registry
        3. if the provided image_tag doesn't exist, print helpful suggestions
        4. if no suggestions at all, give up and return None
    not applicable in ent clusters.
    """
    ctx = context()
    values = ctx.obj['values']
    use_lain_build = 'build' in values
    if not use_lain_build:
        # 如果压根不用 lain build, 那么也无法通过查询 registry 来推断镜像 tag
        return image_tag
    if not image_tag:
        image_tag = lain_meta()

    # 如果该集群的 registry 不支持查询, 那就没什么好检查的了
    registry = tell_registry_client()
    if not registry:
        return image_tag
    appname = ctx.obj['appname']
    existing_tags = registry.list_tags(appname) or []
    if image_tag not in existing_tags:
        # when using lain deploy --build without using --set imageTag=xxx, we
        # can build the requested image for the user
        if ctx.obj['build_jit'] and build_jit_challenge(image_tag):
            lain_('build', '--push')
            return image_tag

        recent_tags = RegistryUtils.sort_and_filter(existing_tags)[:RECENT_TAGS_COUNT]
        if not recent_tags:
            warn(f'no recent tags found in existing_tags: {existing_tags}')
            return image_tag
        latest_tag = recent_tags[0]
        recent_tags_str = '\n            '.join(recent_tags)
        caller_name = inspect.stack()[1].function
        if caller_name == 'update_image':
            amender = 'lain update-image --deduce'
        else:
            amender = f'lain deploy --set imageTag={latest_tag}'

        image = make_image_str(image_tag=image_tag)
        err = f'''
        Image not found: {image}.
        Did you forget to lain push? Try fix with lain deploy --build

        If you'd like to deploy the latest existing image:
            {amender}
        Or choose from a recent version:
            {recent_tags_str}
        See more using lain version
        '''
        error(err, exit=1)

    return image_tag


def lain_(*args, exit=None, **kwargs):
    ctx = context()
    extra_values_file = ctx.obj['extra_values_file']
    if extra_values_file:
        args = ['--values', extra_values_file.name, *args]

    cmd = ['lain', *args]
    kwargs.setdefault('check', True)
    if ctx.obj['ignore_lint']:
        kwargs.setdefault('env', ENV)
        kwargs['env']['LAIN_IGNORE_LINT'] = 'true'

    completed = subprocess_run(cmd, **kwargs)
    if exit:
        context().exit(rc(completed))

    return completed


def lain_image(stage='release'):
    if stage == 'prepare':
        return make_image_str(image_tag='prepare')
    if stage in BUILD_STAGES:
        image_tag = lain_meta()
        return make_image_str(image_tag=image_tag)
    raise ValueError(f'weird stage {stage}, choose from {BUILD_STAGES}')


def lain_meta():
    git_cmd = ['log', '-1', '--pretty=format:%ct-%H']
    res = git(*git_cmd, capture_output=True, silent=True, check=False)
    returncode = rc(res)
    if returncode:
        stderr = ensure_str(res.stderr)
        if 'not a git' in stderr.lower():
            return 'latest'
        error(stderr, exit=returncode)
        error('cannot calculate lain meta, using latest tag')

    stdout = ensure_str(res.stdout)
    image_tag = stdout.strip()
    ctx = context(silent=True)
    if ctx:
        ctx.obj['lain_meta'] = image_tag

    return image_tag


def ensure_resource_initiated(chart=False, secret=False):
    ctx = context()
    if chart:
        if not isdir(CHART_DIR_NAME):
            error(
                'helm chart not initialized yet, run `lain init --help` to learn how',
                exit=1,
            )

    if secret:
        # if volumeMounts are used in values.yaml but secret doesn't exists,
        # print error and then exit
        values = ctx.obj['values']
        subPaths = [
            m['subPath'] for m in values.get('volumeMounts') or [] if m.get('subPath')
        ]
        secret_name = ctx.obj['secret_name']
        # 如果 values 里边定制过了 volumes, 就绕过检查吧, 肯定是高级用户
        if subPaths and not values.get('volumes'):
            cluster = ctx.obj['cluster']
            res = kubectl('get', 'secret', secret_name, capture_output=True)
            code = rc(res)
            if code:
                tutorial = '\n'.join(f'lain secret add {f}' for f in subPaths)
                err = f'''
                Secret {subPaths} not found, you should create them:
                    lain use {cluster}
                    {tutorial}
                And if you ever need to add more files, env or edit them, do this:
                    lain secret edit
                '''
                error(err, exit=code)
        else:
            # don't mind me, just using this function to initiate a dummy secret
            tell_secret(secret_name)

    return True


def subprocess_run(*args, silent=None, dry_run=False, **kwargs):
    """Same in functionality, but better than subprocess.run

    Args:
        silent (bool): do not log subprocess commands.
        check (bool): will capture stderr, and print them on fail.
        abort_on_fail (bool): call ctx.exit on fail, but does not capture any standard output.
    """
    # 这一段代码行为上肯定是多余的, 但是 run 内部不允许 capture_output 和
    # stdout / stderr 俩参数混用, 因此在这里进行适配
    capture_output = kwargs.pop('capture_output', None)
    if capture_output:
        kwargs['stdout'] = subprocess.PIPE
        kwargs['stderr'] = subprocess.PIPE

    capture_error = kwargs.pop('capture_error', None)
    if capture_error:
        kwargs['stderr'] = subprocess.PIPE

    check = kwargs.pop('check', None)
    if check:
        kwargs['stderr'] = subprocess.PIPE

    if not silent:
        ctx = context(silent=True)
        silent = ctx and ctx.obj.get('silent')

    abort_on_fail = kwargs.pop('abort_on_fail', None)
    excall(*args, silent=silent)
    if dry_run:
        return
    try:
        res = subprocess.run(*args, **kwargs)
    except subprocess.TimeoutExpired:
        timeout = kwargs['timeout']
        stderr = (
            f'this command reached its {timeout}s timeout:\n '
            + subprocess.list2cmdline(args[0])
        )
        if not silent:
            error(stderr)

        res = subprocess.CompletedProcess(args[0], 1, stdout=stderr, stderr=stderr)

    code = rc(res)
    if code:
        if check:
            stdout = res.stdout
            stderr = res.stderr
            if stdout or stderr:
                echo(res.stdout)
                error(res.stderr, exit=code)
            else:
                error(
                    f'command did not end well, and has empty output: {args}', exit=code
                )
        elif abort_on_fail:
            context().exit(code)

    return res


@lru_cache(maxsize=None)
def stern_version_challenge():
    try:
        version_res = subprocess_run(
            ['stern', '--version'], capture_output=True, check=True, silent=True
        )
        version_str = version_res.stdout.decode('utf-8').split()[-1]
    except FileNotFoundError:
        download_stern()
        return stern_version_challenge()
    except PermissionError:
        error('Bad binary: stern, remove before use', exit=1)

    if version.parse(version_str) < STERN_MIN_VERSION:
        warn(f'your stern too old: {version_str}')
        download_stern()


def download_stern():
    platform = tell_platform()
    # download directly from https://github.com/wercker/stern/releases if you
    # have a better internet connection
    url = f'https://ghproxy.com/https://github.com/wercker/stern/releases/download/{STERN_MIN_VERSION_STR}/stern_{platform}_amd64'
    return download_binary(url, join(LAIN_EXBIN_PREFIX, 'stern'))


def stern(*args, check=True, **kwargs):
    stern_version_challenge()
    cmd = ['stern', *args]
    completed = subprocess_run(cmd, env=ENV, check=check, **kwargs)
    return completed


@lru_cache(maxsize=None)
def helm_version_challenge():
    try:
        version_res = subprocess_run(
            ['helm', 'version', '--short'], capture_output=True, check=True, silent=True
        )
        version_str = version_res.stdout.decode('utf-8')
    except FileNotFoundError:
        download_helm()
        return helm_version_challenge()
    except PermissionError:
        error('Bad binary: helm, remove before use', exit=1)

    if version.parse(version_str) < HELM_MIN_VERSION:
        warn(f'your helm too old: {version_str}')
        download_helm()


def download_helm():
    platform = tell_platform()
    # download directly from https://github.com/helm/helm/releases/ if you
    # have a better internet connection
    url = f'https://mirrors.huaweicloud.com/helm/{HELM_MIN_VERSION_STR}/helm-{HELM_MIN_VERSION_STR}-{platform}-amd64.tar.gz'
    return download_binary(url, join(LAIN_EXBIN_PREFIX, 'helm'), extract=f'{platform}-amd64/helm')


def helm(*args, check=True, exit=False, **kwargs):
    helm_version_challenge()
    cmd = ['helm', *args]
    completed = subprocess_run(cmd, env=ENV, check=check, **kwargs)
    if exit:
        context().exit(rc(completed))

    return completed


def helm_delete(*args, exit=False):
    for release_name in args:
        res = helm('delete', release_name, check=False, capture_output=True)
        code = rc(res)
        if code:
            stderr = ensure_str(res.stderr)
            if 'not found' in stderr or 'already deleted' in stderr:
                echo(stderr)
            else:
                error(f'weird error during helm delete: {stderr}', exit=code)
        else:
            echo(res.stdout)

    if exit:
        ctx = context()
        ctx.exit(0)


def tell_release_image(release_name, revision=None):
    revision_clause = [f'--revision={revision}'] if revision else []
    res = helm(
        'get', 'values', release_name, *revision_clause, '-ojson', capture_output=True
    )
    values = jalo(res.stdout)
    image_tag = values.get('imageTag')
    if image_tag:
        ctx = context()
        ctx.obj['image_tag'] = image_tag
        ctx.obj['git_revision'] = image_tag.split('-')[-1]

    return image_tag


def docker_images():
    res = docker('images', '--format', r'{{.Repository}}:{{.Tag}}', capture_output=True)
    local_images = ensure_str(res.stdout).splitlines()
    for image in local_images:
        repo, tag = image.split(':', 1)
        appname = repo.rsplit('/', 1)[-1]
        yield {
            'appname': appname,
            'image': image,
            'tag': tag,
        }


def docker(*args, exit=None, check=True, **kwargs):
    cmd = ['docker', *args]
    completed = subprocess_run(cmd, check=check, **kwargs)
    if exit:
        context().exit(rc(completed))

    return completed


def parse_image_tag(image):
    try:
        repo, tag = image.split(':', 1)
    except (ValueError, AttributeError):
        error(f'not a valid image tag: {image}', exit=1)

    return repo, tag


def banyun(image, registry=None, overwrite_latest_tag=False, pull=False, exit=None):
    """搬运镜像到别人家里"""
    if registry and not isinstance(registry, str):
        loop = asyncio.new_event_loop()
        tasks = []
        for r in registry:
            future = loop.run_in_executor(
                None, banyun, image, r, overwrite_latest_tag, pull
            )
            tasks.append(future)

        loop.run_until_complete(asyncio.gather(*tasks))
        loop.close()
        return

    if isfile(image):
        res = docker('load', '-i', image, capture_output=True)
        image = ensure_str(res.stdout).strip().split()[-1]

    repo, tag = parse_image_tag(image)
    tag = tag.replace('release-', '')
    appname = repo.rsplit('/', 1)[-1]
    if not registry:
        cluster_info = tell_cluster_info()
        registry = cluster_info['registry']

    new_image = make_image_str(registry, appname, tag)
    if pull:
        docker('pull', image)

    docker('tag', image, new_image)
    docker('push', new_image, exit=exit)
    if overwrite_latest_tag:
        latest_image = make_image_str(registry, appname, 'latest')
        docker('tag', image, latest_image)
        docker('push', latest_image, exit=exit)

    if tag != 'prepare':
        echo(f' lain deploy --set imageTag={tag}', clean=False)

    return new_image


def docker_save(image, output_dir, pull=False, exit=False):
    if pull:
        docker('pull', image, capture_output=True)

    repo, tag = parse_image_tag(image)
    repo = repo.rsplit('/', 1)[-1]
    fname = f'{repo}_{tag}.tar.gz'
    output_path = join(output_dir, fname)
    cmd = f'docker save {image} | gzip -c > {output_path}'
    res = subprocess_run(cmd, shell=True, check=True)
    stderr = ensure_str(res.stderr)
    if stderr:
        error(stderr, exit=True)

    return res


def git(*args, exit=None, check=True, **kwargs):
    cmd = ['git', *args]
    completed = subprocess_run(cmd, env=ENV, check=check, **kwargs)
    if exit:
        context().exit(rc(completed))

    return completed


def try_to_label_nodes():
    ctx = context()
    appname = ctx.obj['appname']
    deploys = ctx.obj['values']['deployments']
    for deploy_name, deploy in deploys.items():
        nodes = deploy.get('nodes')
        if not nodes:
            continue
        label_name = f'{appname}-{deploy_name}'
        kubectl('label', 'node', '--all', f'{label_name}-', '--overwrite')
        for node in nodes:
            kubectl('label', 'node', f'{node}', f'{label_name}=true', '--overwrite')


def tell_job_names(appname_prefix=True):
    values = load_helm_values()
    appname = values['appname']
    job_names = []
    for proc_name in values.get('jobs') or {}:
        job_name = f'{appname}-{proc_name}' if appname_prefix else proc_name
        job_names.append(job_name)

    return job_names


def try_to_print_job_logs():
    if job_names := tell_job_names(appname_prefix=False):
        for jn in job_names:
            lain_('logs', jn)


def try_to_cleanup_job(job_name=None):
    """when lain deploy, job may not be cleanup yet, so we cleanup manually"""
    if job_name:
        job_names = [job_name]
    else:
        job_names = tell_job_names()

    for jn in job_names:
        res = kubectl('delete', 'job', jn, capture_output=True, check=False)
        if rc(res):
            stderr = ensure_str(res.stderr)
            if 'not found' not in stderr:
                error(f'weird error when deleting job {jn}:')
                error(stderr, exit=1)


@lru_cache(maxsize=None)
def kubectl_version_challenge():
    try:
        version_res = subprocess_run(
            ['kubectl', 'version', '--short', '--client=true'],
            capture_output=True,
            silent=True,
        )
        version_str = version_res.stdout.decode('utf-8').strip().split()[-1]
    except FileNotFoundError:
        download_kubectl()
        return kubectl_version_challenge()
    except PermissionError:
        error('Bad binary: kubectl, remove before use', exit=1)

    if version.parse(version_str) < KUBECTL_MIN_VERSION:
        warn(f'your kubectl version too old: {version_str}')
        download_kubectl()


def download_kubectl():
    platform = tell_platform()
    # download directly from https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/#install-kubectl-binary-with-curl-on-linux if you
    # have a bettern internet connection
    url = f'https://static.yashihq.com/lain4/kubectl-{platform}'
    return download_binary(url, join(LAIN_EXBIN_PREFIX, 'kubectl'))


def kubectl(*args, exit=None, check=True, dry_run=False, **kwargs):
    kubectl_version_challenge()
    cmd = ['kubectl', *args]
    kwargs.setdefault('timeout', 10)
    completed = subprocess_run(cmd, env=ENV, check=check, dry_run=dry_run, **kwargs)
    if exit:
        context().exit(rc(completed))

    return completed


def get_pod_rc(pod_name, tries=5):
    while tries:
        tries -= 1
        res = kubectl(
            'get', 'po', pod_name, '-o=jsonpath={..exitCode}', capture_output=True
        )
        rc_str = ensure_str(res.stdout)
        if not rc_str:
            sleep(2)
            continue
        codes = [int(s) for s in rc_str.split()]
        return max(codes)

    error(f'cannot get exitCode for {pod_name}', exit=True)


def tell_release_name():
    ctx = context()
    obj = ctx.obj
    return obj.get('release_name') or obj['appname']


def is_inside_cluster():
    return bool(ENV.get('KUBERNETES_SERVICE_HOST'))


def wait_for_svc_up(tries=20):
    release_name = tell_release_name()
    selector = f'helm.sh/chart={release_name}'
    res = kubectl(
        'get',
        'svc',
        '-l',
        selector,
        '--no-headers=true',
        capture_output=True,
        check=False,
    )
    svc_urls = []
    for line in ensure_str(res.stdout).splitlines():
        svc_name, _, _, _, port, _ = line.split()
        portnum = int(port.split('/', 1)[0])
        svc_urls.append(f'http://{svc_name}:{portnum}')

    def test_urls(urls):
        for url in svc_urls:
            try:
                requests.get(url, timeout=1)
            except Exception as e:
                warn(f'{url} not up due to {e}')
                return False

        return True

    while tries:
        tries -= 1
        sleep(3)
        if test_urls(svc_urls):
            return True

    return False


def wait_for_pod_up(selector=None, tries=40):
    if not selector:
        ctx = context()
        appname = ctx.obj['appname']
        selector = f'app.kubernetes.io/name={appname}'

    waiting_state = frozenset(
        ('pending', 'containercreating', 'notready', 'terminating')
    )
    while tries:
        tries -= 1
        sleep(3)
        res = kubectl(
            'get',
            'po',
            '-l',
            selector,
            '--no-headers=true',
            capture_output=True,
            check=False,
        )
        stdout = ensure_str(res.stdout)
        pod_lines = stdout.splitlines()
        current_states = set()
        pod_names = []
        for line in pod_lines:
            debug(line)
            pod_name, ready_pair, state, *_ = line.split()
            state = state.lower()
            if state == 'running':
                n_ready, n_all = ready_pair.split('/')
                if n_ready != n_all:
                    state = 'notready'

            current_states.add(state.lower())
            pod_names.append(pod_name)

        if not current_states.intersection(waiting_state):
            return pod_names
        debug(stdout)
        continue
    error('job container never got up, use these commands to see what\'s wrong:')
    error(f'k describe po {pod_name}')
    error(f'k logs {pod_name}')


def wait_for_cluster_up(tries=1):
    context().obj['silent'] = True
    cluster_info = tell_cluster_info()
    url = f'http://default-backend.{cluster_info["domain"]}'
    probe_result = None
    forgive_error_substrings = (
        'timeout',
        'unable to connect',
        'was refused',
        'context deadline exceeded',
    )
    while tries:
        tries -= 1
        res = kubectl('version', capture_output=True, timeout=2, check=False)
        stderr = ensure_str(res.stderr).lower()
        if not stderr:
            # 如果是托管集群的话, master 始终在线, k version 的输出并不足以证明该集群
            # worker 节点都启动了, 所以还得验证下 ingress controller 是不是在线
            with suppress(RequestException):
                probe_result = requests.get(url)

            if probe_result is not None and probe_result.status_code == 404:
                return 'on'
            probe_msg = probe_result is not None and probe_result.text
            debug(f'cluster not up due to probe failed: {probe_msg}')
            sleep(3)
            continue
        if any(s in stderr for s in forgive_error_substrings):
            # 等开机, 多等会吧
            sleep(5)
            continue
        error(f'weird error {stderr}', exit=True)


def tell_cluster():
    """
    有这样一个副作用, 就是写好了 ctx.obj['cluster']
    helm values 必须要在一个 lain4 项目 repo 下才会有
    但是少数功能不需要在 repo 下也可以执行
    """
    link = expanduser('~/.kube/config')
    try:
        kubeconfig_file = readlink(link)
    except FileNotFoundError:
        error(f'{link} not found, you should first `lain use [CLUSTER]`')
        raise
    except OSError:
        error(f'{link} is not a symlink or does not exist')
        raise

    name = basename(kubeconfig_file)
    cluster_name = name.split('-', 1)[-1]
    ctx = context()
    ctx.obj['cluster'] = cluster_name
    return cluster_name


def tell_platform():
    platform = sys.platform
    if platform.startswith('darwin'):
        return 'darwin'
    if platform.startswith('linux'):
        return 'linux'
    raise ValueError(
        f'Sorry, never seen this platform: {platform}. Use a Mac or Linux for lain'
    )


def download_binary(url, dest, extract=None):
    headsup = f'''Don\'t mind me, just gonna download {url} into {dest}.
    If you want to use different path other than {dest}, export LAIN_EXBIN_PREFIX to customize.
    Or you can simply install them yourself (for example using homebrew).
    '''
    click.echo(headsup, err=True)
    if extract:
        download_path = '/tmp/{}'.format(basename(url))
    else:
        download_path = dest

    try:
        with requests.get(url, stream=True) as res:
            with open(download_path, 'wb') as f:
                shutil.copyfileobj(res.raw, f)
    except KeyboardInterrupt:
        ensure_absent(download_path)
        error(f'Download did not complete, {download_path} is cleaned up', exit=1)

    if extract:
        with tarfile.open(download_path) as tarf:
            for member in tarf.getmembers():
                if member.name == extract:
                    binary_name = basename(dest)
                    member.name = binary_name
                    tarf.extract(member, path=dirname(dest))

        ensure_absent(download_path)

    # do a `chmod +x` on this thing
    st = os.stat(dest)
    os.chmod(dest, st.st_mode | stat.S_IEXEC)


def ensure_absent(path):
    if not isinstance(path, str):
        for p in path:
            ensure_absent(p)

        return

    if isdir(path):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            pass
    else:
        try:
            remove(path)
            return True
        except FileNotFoundError:
            pass


def find(path):
    """mimic to GNU find"""
    for currentpath, _, files in os.walk(path):
        for file in files:
            abs_path = join(currentpath, file)
            relpath = os.path.relpath(abs_path, path)
            yield relpath


def edit_file(f):
    f.seek(0)
    subprocess.call([ENV.get('EDITOR', 'vim'), f.name], env=os.environ)
    f.seek(0)


def yalo(f, many=False):
    """tired of yaml bitching about unsafe loaders"""
    if hasattr(f, 'read'):
        f.seek(0)
        f = f.read()
    # tempfile buffer content could be different from the actual hard disk
    # content
    elif hasattr(f, 'name'):
        with open(f.name) as file_again:
            f = file_again.read()

    load = yaml.safe_load_all if many else yaml.safe_load
    return load(f)


def yadu(dic, f=None):
    yaml.scalarstring.walk_tree(dic)
    s = yaml.round_trip_dump(dic, allow_unicode=True)
    if not f:
        return s
    if hasattr(f, 'read'):
        f.write(s.encode('utf-8'))
    elif isinstance(f, str):
        with open(f, 'wb') as dest:
            dest.write(s.encode('utf-8'))
    else:
        raise ValueError(f'f must be a file or path, got {f}')


def jadu(dic):
    return json.dumps(dic, separators=(',', ':'))


def jalo(s):
    """stupid json doesn't even tell you why anything fails"""
    try:
        return json.loads(s)
    except ValueError as e:
        raise ValueError('cannot decode this shit: {}'.format(ensure_str(s))) from e


def brief(s):
    r"""
    >>> a = 'a' * 89
    >>> brief(a)
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa...'
    >>> a = '''
    ... foo
    ... bar
    ... '''
    >>> brief(a)
    '\\nfoo\\nbar\\n'
    """
    try:
        single_line = s.replace('\n', '\\n')
    except AttributeError:
        return single_line
    if len(single_line) > 88:
        return single_line[:88] + '...'
    return single_line


class LenientSchema(Schema):
    class Meta:
        unknown = INCLUDE


class PrepareSchema(LenientSchema):
    script = List(Str, required=True)
    keep = List(Str, missing=[])


class BuildSchema(LenientSchema):
    base = Str(required=True)
    prepare = Nested(PrepareSchema, required=False, allow_none=True)
    script = List(Str, missing=[])
    workdir = Str(missing=DEFAULT_WORKDIR)


def parse_copy(stuff):
    """
    >>> parse_copy('/path')
    {'src': '/path', 'dest': '/path'}
    >>> parse_copy({'src': '/path'})
    {'src': '/path', 'dest': '/path'}
    >>> parse_copy({'src': '/path', 'dest': '/another'})
    {'src': '/path', 'dest': '/another'}
    """
    if isinstance(stuff, str):
        return {'src': stuff, 'dest': stuff}
    if isinstance(stuff, dict):
        if 'src' not in stuff:
            raise ValidationError('if copy clause is a dict, it must contain src')
        if 'dest' not in stuff:
            stuff['dest'] = stuff['src']

        return stuff
    raise ValidationError(f'copy clause must be str or dict, got {stuff}')


class ReleaseSchema(LenientSchema):
    script = List(Str, missing=[])
    workdir = Str(missing=DEFAULT_WORKDIR)
    dest_base = Str()
    copy = List(Function(deserialize=parse_copy), missing=[])


class VolumeMountSchema(LenientSchema):
    mountPath = Str(required=True)
    subPath = Str(required=False)

    @validates("subPath")
    def validate_subPath(self, value):
        bn = basename(value)
        if bn != value:
            raise ValidationError(f'subPath should be {bn}, not {value}')


class HPASchema(LenientSchema):
    @post_load
    def finalize(self, data, **kwargs):
        if 'targetCPUUtilizationPercentage' in data:
            raise ValidationError(
                'you should remove targetCPUUtilizationPercentage from hpa, and use hpa.metrics'
            )
        return data


class ResourceSchema(Schema):
    cpu = Raw(required=True)
    memory = Raw(required=True)


class ResourcesSchema(Schema):
    requests = Nested(ResourceSchema, required=True)
    limits = Nested(ResourceSchema, required=True)


# env 的 key, value 必须是字符串, 否则 helm 会转为科学记数法
# https://github.com/helm/helm/issues/6867
env_schema = Dict(keys=Str(), values=Str(), allow_none=True)


class InitContainerSchema(LenientSchema):
    env = env_schema


class DeploymentSchema(LenientSchema):
    env = env_schema
    hpa = Nested(HPASchema, required=False)
    containerPort = Int(required=False)
    readinessProbe = Raw(missing={})
    replicaCount = Int(required=True)
    resources = Nested(ResourcesSchema, required=True)

    @post_load
    def finalize(self, data, **kwargs):
        if 'containerPort' in data and 'readinessProbe' not in data:
            raise ValidationError(
                'when containerPort is defined, you must use readinessProbe as well'
            )
        return data


class JobSchema(LenientSchema):
    env = env_schema
    initContainers = List(Nested(InitContainerSchema))


class CronjobSchema(LenientSchema):
    resources = Nested(ResourcesSchema, required=False)
    env = env_schema

    @post_load
    def finalize(self, data, **kwargs):
        resources = data.get('resources')
        if resources:
            limits = resources.get('limits', {})
            requests = resources.get('requests', {})
            for n in ['cpu', 'memory']:
                if limits.get(n) != requests.get(n):
                    raise ValidationError(
                        f'for cronjobs, limits and requests must be equal, got: {resources}'
                    )
        return data


class IngressSchema(LenientSchema):
    host = Str(required=True)
    deployName = Str(required=True)
    paths = List(Str, required=True)


class HelmValuesSchema(LenientSchema):
    appname = Str(required=True)
    env = env_schema
    volumeMounts = List(Nested(VolumeMountSchema), allow_none=True)
    deployments = Dict(
        keys=Str(), values=Nested(DeploymentSchema), required=False, allow_none=True
    )
    jobs = Dict(keys=Str(), values=Nested(JobSchema), required=False, allow_none=True)
    cronjobs = Dict(
        keys=Str(), values=Nested(CronjobSchema), required=False, allow_none=True
    )
    tests = Raw(missing={}, allow_none=True)
    ingresses = List(Nested(IngressSchema), required=False)
    externalIngresses = List(Nested(IngressSchema), required=False)
    canaryGroups = Dict(
        keys=Str(),
        values=Dict(keys=Str(validate=OneOf(INGRESS_CANARY_ANNOTATIONS)), values=Str()),
        required=False,
        allow_none=True,
        missing=None,
    )
    publish_to = List(Str)
    build = Nested(BuildSchema, required=False)
    release = Nested(ReleaseSchema, required=False)

    @post_load
    def finalize(self, data, **kwargs):
        for k in ['deployments', 'cronjobs']:
            if not data.get(k):
                data[k] = {}

        data['procs'] = data['deployments'].copy()
        data['procs'].update(data['cronjobs'])
        # check for duplicate proc names
        deploy_names = set(data['deployments'] or [])
        cronjob_names = set(data['cronjobs'] or [])
        duplicated_names = deploy_names.intersection(cronjob_names)
        if duplicated_names:
            raise ValidationError(
                f'proc names should not duplicate: {duplicated_names}'
            )
        online_clusters = [
            name for name, cluster in CLUSTERS.items() if not cluster.get('offline')
        ]
        data.setdefault('publish_to', online_clusters)
        data['publish_to'] = set(data['publish_to'])
        data['publish_to_registries'] = set(
            c['registry'] for n, c in CLUSTERS.items() if n in data['publish_to']
        )
        release_clause = data.get('release')
        if release_clause:
            build_clause = data.get('build')
            if not build_clause:
                raise ValidationError('release defined, but not build')
            release_clause.setdefault('dest_base', build_clause['base'])

        return data


def validate_proc_name(ctx, param, value):
    if not value:
        return value
    ctx = context()
    procs = ctx.obj['values']['procs']
    if value not in procs:
        proc_names = list(procs)
        raise BadParameter(f'{value} not found in procs, choose from {proc_names}')
    return value


def load_helm_values(values_yaml=f'./{CHART_DIR_NAME}/values.yaml'):
    if hasattr(values_yaml, 'read'):
        values = yalo(values_yaml)
    else:
        with open(values_yaml) as f:
            values = yalo(f)

    internal_values_file = tell_cluster_values_file(internal=True)
    if internal_values_file:
        recursive_update(values, yalo(open(internal_values_file)))

    cluster_values_file = tell_cluster_values_file()
    if cluster_values_file:
        dic = yalo(open(cluster_values_file))
        if not isinstance(dic, dict):
            # 调用 gitlab 接口对 link 类型的文件处理有问题, 下载下来以后只是一个普通的文本文件
            # 只好在代码里实现一下 link 咯
            if isinstance(dic, str) and isfile(join(CHART_DIR_NAME, dic)):
                linked_file = join(CHART_DIR_NAME, dic)
                dic = yalo(open(linked_file))
            else:
                error(
                    f'content of cluster values file {cluster_values_file} is neither a dict or a valid values path, got: {dic}',
                    exit=1,
                )

        recursive_update(values, dic)

    ctx = context()
    extra_values_file = ctx.obj['extra_values_file']
    if extra_values_file:
        recursive_update(values, yalo(extra_values_file))

    schema = HelmValuesSchema()
    try:
        loaded = schema.load(values)
    except ValidationError as e:
        error('your values.yaml did not pass schema check:')
        error(e, exit=1)

    return loaded


def ensure_helm_initiated():
    """gather basic information about the current app.
    If cluster info is provided, will try to fetch app status from Kubernetes"""
    with suppress(FileNotFoundError, OSError):
        tell_cluster()

    lookout_env = LOOKOUT_ENV.intersection(ENV)
    if lookout_env:
        warn(f'you better unset these variables: {lookout_env}')

    ctx = context()
    obj = ctx.obj
    obj['chart_name'] = CHART_DIR_NAME
    obj['chart_version'] = CHART_VERSION
    values_yaml = f'./{CHART_DIR_NAME}/values.yaml'
    try:
        values = load_helm_values(values_yaml)
        appname = obj['appname'] = values['appname']
        obj['values'] = values
        obj['secret_name'] = f'{appname}-secret'
        obj['env_name'] = f'{appname}-env'
    except FileNotFoundError:
        warn('not in a lain4 app repo')
        raise
    except KeyError:
        error(
            f'{values_yaml} doesn\'t look like a valid lain4 yaml, if you want to use lain4 for this app, use `lain inif -f`'
        )
        raise
    # collect all uppercase consts
    for k, v in globals().items():
        if k.isupper() and not k.startswith('_'):
            if k in obj:
                continue
            obj[k] = v

    obj['urls'] = tell_ingress_urls()


def get_app_status(appname):
    res = helm('status', appname, '-o', 'json', capture_output=True, check=False)
    code = rc(res)
    if not code:
        son = jalo(res.stdout)
        if son['info']['status'] == 'uninstalled':
            return
        return son
    stderr = res.stderr.decode('utf-8')
    # 'not found' is the only error we can safely ignore
    if 'not found' not in stderr:
        error('helm error during getting app status:')
        error(stderr, exit=code)


template_env.filters['basename'] = basename
template_env.filters['quote'] = quote
template_env.filters['to_yaml'] = yadu
template_env.filters['brief'] = brief


class literal(str):
    pass


def literal_presenter(dumper, data):
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')


yaml.add_representer(literal, literal_presenter)


class KVPairType(click.ParamType):
    name = "kvpair"

    def convert(self, value, param, ctx):
        try:
            k, v = value.split('=')
            return (k, v)
        except (AttributeError, ValueError):
            self.fail(
                "expected something like FOO=BAR, got "
                f"{value!r} of type {type(value).__name__}",
                param,
                ctx,
            )


def is_values_file(fname):
    """
    >>> is_values_file('foo/bar/values.yaml')
    True
    >>> is_values_file('values.yaml.j2')
    True
    >>> is_values_file('values-future.yaml')
    True
    >>> is_values_file('values-future.yml')
    True
    >>> is_values_file('deployment.yml.j2')
    False
    """
    fname = basename(fname)
    fname = re.sub(r'\.j2', '', fname)
    fname = re.sub(r'.yml', '.yaml', fname)
    is_yaml = fname.endswith('yaml')
    is_values = fname.startswith('values')
    return is_yaml and is_values


def top_procs(appname):
    """use memory data from prometheus as memory_top"""
    result = {}
    cluster_info = tell_cluster_info()
    values = context().obj['values']
    if 'prometheus' in cluster_info:
        from lain_cli.prometheus import Prometheus

        prometheus = Prometheus()
    else:
        return result
    for proc_name, proc in values['procs'].items():
        memory_top = prometheus.memory_p95(appname, proc_name)
        if not memory_top:
            continue
        # container memory shoudn't be lower than 4Mi
        memory_top = max(memory_top, KUBERNETES_MIN_MEMORY)
        memory_top_str = format_kubernetes_memory(memory_top)
        cpu_top = prometheus.cpu_p95(appname, proc_name)
        proc.update(
            {
                'memory_top': memory_top,
                'memory_top_str': memory_top_str,
                'cpu_top': cpu_top,
            }
        )
        result[proc_name] = proc

    return result


KUBERNETES_DISK_SIZE_UNITS = (
    CombinedUnit(
        SizeUnit(1000 ** 2, 'MB', 'megabyte'), SizeUnit(1024 ** 2, 'Mi', 'mebibyte')
    ),
)


def pluralize_compact(count, singular, plural=None):
    if not plural:
        plural = singular + 's'
    return '%s%s' % (count, singular if math.floor(float(count)) == 1 else plural)


def format_kubernetes_memory(num_bytes):
    for unit in reversed(KUBERNETES_DISK_SIZE_UNITS):
        if num_bytes >= unit.binary.divider:
            number = round_number(
                math.ceil(float(num_bytes) / unit.binary.divider), keep_width=False
            )
            return pluralize_compact(number, unit.binary.symbol, unit.binary.symbol)
    debug(f'value too small, format as 50M instead: {num_bytes}')
    return '50M'


def parse_kubernetes_cpu(s):
    """
    https://kubernetes.io/docs/concepts/configuration/manage-compute-resources-container/#meaning-of-cpu
    >>> parse_kubernetes_cpu('1000m')
    1000
    >>> parse_kubernetes_cpu('1')
    1000
    >>> parse_kubernetes_cpu(0.5)
    500
    >>> parse_kubernetes_cpu(1)
    1000
    """
    if isinstance(s, Number):
        return int(s * 1000)
    if isinstance(s, str) and s.endswith('m'):
        return int(s.replace('m', ''))
    if isinstance(s, str) and s.isdigit():
        return parse_kubernetes_cpu(float(s))
    raise ValueError(f'weird cpu value: {s}')


@contextmanager
def change_dir(d):
    saved_dir = cwd()
    try:
        os.chdir(d or '.')
        yield
    finally:
        os.chdir(saved_dir)


def try_lain_prepare(keep_dockerfile=False):
    """想尽办法拿到 prepare 镜像, 先 pull, 没有的话看本地,
    本地有的话还要顺手搬运过去"""
    ctx = context()
    values = ctx.obj['values']
    build_clause = values['build']
    prepare_clause = build_clause.get('prepare')
    if not prepare_clause:
        return

    appname = ctx.obj['appname']
    local_prepare_image = ''
    for image_info in docker_images():
        if image_info['appname'] == appname and image_info['tag'] == 'prepare':
            local_prepare_image = image_info['image']
            break

    prepare_image = lain_image(stage='prepare')
    res = docker('pull', prepare_image, capture_error=True, check=False)
    returncode = rc(res)
    if returncode:
        stderr = ensure_str(res.stderr)
        if 'not found' in stderr:
            if local_prepare_image:
                echo(
                    f'{prepare_image} not found, will publish {local_prepare_image} to {prepare_image}'
                )
                banyun(local_prepare_image)
            else:
                lain_build(stage='prepare', push=True, keep_dockerfile=keep_dockerfile)
        else:
            error(stderr, exit=returncode)


def lain_build(stage='build', push=True, keep_dockerfile=False):
    ctx = context()
    ctx.obj['current_build_stage'] = stage
    values = ctx.obj['values']
    if 'build' not in values:
        warn('build not defined in {CHART_DIR_NAME}/values.yaml', exit=0)

    build_clause = values['build']
    prepare_clause = build_clause.get('prepare')
    if stage == 'prepare' and not prepare_clause:
        build_yaml = yadu(build_clause)
        warn(f'empty prepare clause:\n\n{build_yaml}', exit=0)

    image = lain_image(stage)
    template = template_env.get_template(f'{DOCKERFILE_NAME}.j2')
    if isfile(DOCKERFILE_NAME):
        error(
            f'{DOCKERFILE_NAME} already exists, remove if you want to use lain build',
            exit=True,
        )

    if isfile(DOCKERIGNORE_NAME):
        dockerignore_created = False
        warn(f'you have your own {DOCKERIGNORE_NAME}, fine')
    else:
        extra_ignore_file = join(TEMPLATE_DIR, DOCKERIGNORE_NAME)
        with open(DOCKERIGNORE_NAME, 'w') as docker_ignore:
            with open(extra_ignore_file) as extra_ignore:
                docker_ignore.write(extra_ignore.read())

            with open('.gitignore') as git_ignore:
                docker_ignore.write(git_ignore.read())

        dockerignore_created = True

    with open(DOCKERFILE_NAME, 'w') as f:
        f.write(template.render(**ctx.obj))

    try:
        docker(
            'build',
            '--pull',
            '-t',
            image,
            '--target',
            stage,
            '-f',
            DOCKERFILE_NAME,
            '.',
            check=False,
            abort_on_fail=True,
        )
    finally:
        if not keep_dockerfile:
            ensure_absent(DOCKERFILE_NAME)

        if dockerignore_created:
            ensure_absent(DOCKERIGNORE_NAME)

    if push:
        banyun(image)

    return image


def make_wildcard_domain(d):
    """
    >>> make_wildcard_domain('foo-bar.example.com')
    ['*.example.com', 'example.com']
    """
    if d.count('.') == 1:
        without_star = d
        with_star = f'*.{without_star}'
    else:
        with_star = re.sub(r'^([^.]+)(?=.)', '*', d, 1)
        without_star = with_star.replace('*.', '')

    return [with_star, without_star]


def make_image_str(registry=None, appname=None, image_tag=None):
    ctx = context()
    cluster_info = tell_cluster_info()
    if not registry:
        registry = cluster_info['registry']

    if not image_tag:
        image_tag = lain_meta()

    if not appname:
        appname = ctx.obj['appname']

    image = f'{registry}/{appname}:{image_tag}'
    return image


def tell_image():
    ctx = context()
    appname = ctx.obj.get('appname')
    meta = lain_meta()
    for image_info in docker_images():
        if image_info['appname'] == appname and image_info['tag'] == meta:
            return image_info['image']


def tell_domain_tls_name(d):
    """
    >>> tell_domain_tls_name('*.example.com')
    'example-com'
    >>> tell_domain_tls_name('prometheus.example.com')
    'prometheus-example-com'
    """
    parts = d.split('.')
    if parts[0] == '*':
        parts = parts[1:]

    return '-'.join(parts)


def rc(res):
    try:
        return res.exit_code
    except AttributeError:
        return res.returncode


def stable_hash(s):
    h = blake2b(digest_size=8, key=b'lain')
    h.update(s.encode('utf-8'))
    return h.hexdigest()


def make_job_name(command):
    if not command:
        command = ''

    if not isinstance(command, str):
        command = ''.join(command)

    ctx = context()
    appname = ctx.obj['appname']
    h = stable_hash(command)
    job_name = f'{appname}-{h}'
    return job_name


def version_challenge():
    ctx = context()
    if ctx.obj['ignore_lint']:
        return
    session = PipSession()
    session.timeout = 2
    cluster_info = tell_cluster_info()
    pypi_index = cluster_info['pypi_index']
    search_scope = SearchScope.create(find_links=[], index_urls=[pypi_index])
    link_collector = LinkCollector(session=session, search_scope=search_scope)
    selection_prefs = SelectionPreferences(
        allow_yanked=False,
        allow_all_prereleases=False,
    )
    finder = PackageFinder.create(
        link_collector=link_collector,
        selection_prefs=selection_prefs,
    )
    best_candidate = finder.find_best_candidate('lain_cli').best_candidate
    debug(f'best candidate: {best_candidate}')
    if not best_candidate:
        warn(f'fail to lookup latest version from {pypi_index}')
        return
    now = version.parse(__version__)
    new = best_candidate.version
    if any([now.major > new.major, now.minor > new.minor]):
        return
    if not all(
        [now.major == new.major, now.minor == new.minor, new.micro - now.micro <= 2]
    ):
        error(f'you are using lain_cli=={__version__}, upgrade before use:')
        extra_index = cluster_info.get('pypi_extra_index')
        if extra_index:
            extra_clause = f'--extra-index-url {extra_index}'
        else:
            extra_clause = ''

        error('workon lain-cli')
        error(f'pip install -U lain_cli=={new} -i {pypi_index} {extra_clause}')
        error('you can use --ignore-lint to bypass this check', exit=1)


def user_challenge(release_name):
    """用户必须与 helm values 记载的 user 匹配, 才能继续"""
    res = helm('get', 'values', release_name, '-ojson', capture_output=True)
    values = jalo(res.stdout)
    written_user = values.get('user')
    if not written_user:
        return
    user = tell_executor()
    if written_user != user:
        error(
            f'{release_name} was deployed by {written_user}, not to be tampered by {user}',
            exit=1,
        )


def build_jit_challenge(image_tag):
    if image_tag == 'latest':
        return True
    ctx = context()
    if not ctx.obj['build_jit']:
        return True
    lain_meta_ = lain_meta()
    if image_tag == lain_meta_:
        return True
    error('when using lain deploy, do not use --build with --set imageTag=xxx', exit=1)
