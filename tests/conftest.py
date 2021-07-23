import sys
from os import chdir, environ, getcwd
from os.path import abspath, dirname, join
from random import choice
from string import ascii_letters
from typing import Any, Tuple

import click
import pytest
from click.testing import CliRunner

from lain_cli.lain import lain
from lain_cli.tencent import TencentRegistry
from lain_cli.utils import (
    CHART_DIR_NAME,
    CLUSTERS,
    DOCKERFILE_NAME,
    change_dir,
    ensure_absent,
    ensure_helm_initiated,
    error,
    kubectl,
    lain_meta,
    rc,
    yalo,
    make_canary_name,
)

TESTS_BASE_DIR = dirname(abspath(__file__))
DUMMY_APPNAME = 'dummy'
DUMMY_CANARY_NAME = make_canary_name(DUMMY_APPNAME)
DUMMY_REPO = f'tests/{DUMMY_APPNAME}'
DUMMY_VALUES_PATH = join(CHART_DIR_NAME, 'values.yaml')
with change_dir(DUMMY_REPO):
    DUMMY_IMAGE_TAG = lain_meta()

TEST_CLUSTER = 'test'
TEST_CLUSTER_INFO = CLUSTERS[TEST_CLUSTER]
DUMMY_URL = f'http://{DUMMY_APPNAME}.{TEST_CLUSTER_INFO["domain"]}'
# this url will point to proc.web-dev in example_lain_yaml
DUMMY_DEV_URL = f'http://{DUMMY_APPNAME}-dev.{TEST_CLUSTER_INFO["domain"]}'
RANDOM_STRING = ''.join([choice(ascii_letters) for n in range(9)])
BUILD_TREASURE_NAME = 'treasure.txt'


def render_k8s_specs():
    res = run(lain, args=['-s', 'template'])
    return list(yalo(res.stdout, many=True))


def load_dummy_values():
    with open(DUMMY_VALUES_PATH) as f:
        values = yalo(f)

    return values


def tell_ing_name(host, appname, domain, proc):
    host_flat = host.replace('.', '-')
    domain_flat = domain.replace('.', '-')
    if '.' in host:
        return f'{host_flat}-{appname}-{proc}'
    return f'{host_flat}-{domain_flat}-{appname}-{proc}'


def tell_deployed_images(appname):
    res = kubectl(
        'get',
        'deploy',
        '-ojsonpath={..image}',
        '-l',
        f'app.kubernetes.io/name={appname}',
        capture_output=True,
    )
    if rc(res):
        error(res.stdout, exit=1)

    images = set(res.stdout.decode('utf-8').split())
    return images


def run(*args, returncode=0, obj=None, **kwargs):
    """run cli command in a click context"""
    runner = CliRunner()
    env = environ.copy()
    env['LAIN_IGNORE_LINT'] = 'false'
    obj = obj or {}
    res = runner.invoke(*args, obj=obj, env=env, **kwargs)
    if returncode is not None:
        if rc(res) != returncode:
            print(res.output)
            print(res.exception)

        assert rc(res) == returncode

    return res


def run_under_click_context(
    f, args=(), returncode=0, obj=None, kwargs=None
) -> Tuple[click.testing.Result, Any]:
    """to test functions that use click context internally, we must invoke them
    under a active click context, and the only way to do that currently is to
    wrap the function call in a click command"""
    cache = {'func_result': None}
    obj = obj or {}

    @lain.command()
    @click.pass_context
    def wrapper_command(ctx):
        ensure_helm_initiated()
        func_result = f(*args, **(kwargs or {}))
        cache['func_result'] = func_result

    runner = CliRunner()

    res = runner.invoke(lain, args=['wrapper-command'], obj=obj, env=environ)
    if returncode is not None:
        if rc(res) != returncode:
            print(res.output)
            print(res.exception)

        assert rc(res) == returncode

    return res, cache['func_result']


@pytest.fixture()
def dummy_helm_chart(request):
    def tear_down():
        ensure_absent([CHART_DIR_NAME, DOCKERFILE_NAME])

    if not getcwd().endswith(DUMMY_REPO):
        sys.path.append(TESTS_BASE_DIR)
        chdir(DUMMY_REPO)

    tear_down()
    run(lain, args=['init', '-f'])
    request.addfinalizer(tear_down)


@pytest.fixture()
def dummy(request):
    def tear_down():
        # 拆除测试的结果就不要要求这么高了, 因为有时候会打断点手动调试
        # 跑这段拆除代码的时候, 可能东西已经被拆干净了
        run(lain, args=['delete', '--purge'], returncode=None)
        ensure_absent([CHART_DIR_NAME, DOCKERFILE_NAME])

    if not getcwd().endswith(DUMMY_REPO):
        sys.path.append(TESTS_BASE_DIR)
        chdir(DUMMY_REPO)

    tear_down()
    run(lain, args=['init'])
    # `lain secret show` will create a dummy secret
    run(lain, args=['secret', 'show'])
    request.addfinalizer(tear_down)


@pytest.fixture()
def registry(request):
    cluster_info = dict(CLUSTERS[TEST_CLUSTER])
    return TencentRegistry(
        cluster_info['registry'],
        cluster_info['access_key_id'],
        cluster_info['access_key_secret'],
    )


def dic_contains(big, small):
    left = big.copy()
    left.update(small)
    assert left == big


run(lain, args=['use', TEST_CLUSTER])
