from os.path import basename, join
from tempfile import NamedTemporaryFile, TemporaryDirectory

import click
import pytest

from lain_cli.aliyun import AliyunRegistry
from lain_cli.harbor import HarborRegistry
from lain_cli.utils import (
    CLUSTERS,
    INTERNAL_CLUSTER_VALUES_DIR,
    banyun,
    change_dir,
    context,
    ensure_str,
    lain_meta,
    load_helm_values,
    make_job_name,
    subprocess_run,
    tell_cluster,
    tell_cluster_values_file,
    tell_helm_options,
    yadu,
    yalo,
)
from tests.conftest import (
    CHART_DIR_NAME,
    DUMMY_APPNAME,
    DUMMY_REPO,
    TEST_CLUSTER,
    TEST_CLUSTER_INFO,
    run_under_click_context,
)

BULLSHIT = '不过我倒不在乎做什么工作,只要没人认识我,我也不认识他们就行了。我还会装作自己是个又聋又哑的人。这样我就可以不必跟任何人讲些他妈的没意思的废话。'


@pytest.mark.usefixtures('dummy_helm_chart')
def test_make_job_name():
    _, res = run_under_click_context(make_job_name, args=[''])
    assert res == 'dummy-5562bd9d33e0c6ce'  # this is a stable hash value


def test_ya():
    dic = {'slogan': BULLSHIT}
    f = NamedTemporaryFile()
    yadu(dic, f)
    f.seek(0)
    assert yalo(f) == dic


@pytest.mark.usefixtures('dummy_helm_chart')
def test_subprocess_run():
    cmd = ['helm', 'version', '--bad-flag']
    cmd_result, func_result = run_under_click_context(
        subprocess_run,
        args=[cmd],
        kwargs={'check': True},
        returncode=1,
    )
    # sensible output in stderr, rather than python traceback
    assert 'unknown flag: --bad-flag' in cmd_result.output

    cmd_result, func_result = run_under_click_context(
        subprocess_run,
        args=[cmd],
        kwargs={'abort_on_fail': True},
        returncode=1,
    )
    # abort_on_fail will not capture std
    assert 'unknown flag: --bad-flag' not in cmd_result.output

    cmd = ['helm', 'version']
    cmd_result, func_result = run_under_click_context(
        subprocess_run,
        args=[cmd],
        kwargs={'check': True, 'capture_output': True},
    )
    assert 'version' in ensure_str(func_result.stdout)

    cmd = 'pwd | cat'
    _, func_result = run_under_click_context(
        subprocess_run,
        args=[cmd],
        kwargs={'shell': True, 'capture_output': True, 'check': True},
    )
    wd = ensure_str(func_result.stdout).strip()
    assert wd.endswith(DUMMY_REPO)


@pytest.mark.usefixtures('dummy_helm_chart')
def test_tell_cluster():
    _, func_result = run_under_click_context(tell_cluster)
    assert func_result == TEST_CLUSTER


def test_lain_meta():
    not_a_git_dir = TemporaryDirectory()
    with change_dir(not_a_git_dir.name):
        assert lain_meta() == 'latest'


@pytest.mark.usefixtures('dummy_helm_chart')
def test_banyun():
    cli_result, _ = run_under_click_context(banyun, ('not-a-image',), returncode=1)
    assert 'not a valid image tag' in cli_result.stdout


@pytest.mark.usefixtures('dummy_helm_chart')
def test_load_helm_values():
    # test internal cluster values are correctly loaded
    _, values = run_under_click_context(
        load_helm_values,
    )
    assert values['ingressClass'] == 'lain-internal'
    assert values['externalIngressClass'] == 'lain-external'
    dummy_jobs = {
        'init': {'command': ['echo', 'nothing']},
    }
    override_values = {
        'jobs': dummy_jobs,
    }
    yadu(override_values, f'{CHART_DIR_NAME}/values-{TEST_CLUSTER}.yaml')
    _, values = run_under_click_context(
        load_helm_values,
    )
    assert values['jobs'] == dummy_jobs


@pytest.mark.usefixtures('dummy_helm_chart')
def test_tell_helm_options():
    _, options = run_under_click_context(
        tell_helm_options,
    )
    internal_values_file = join(
        INTERNAL_CLUSTER_VALUES_DIR, f'values-{TEST_CLUSTER}.yaml'
    )
    assert internal_values_file in set(options)
    set_values = parse_helm_set_clause_from_options(options)
    assert set_values['registry'] == TEST_CLUSTER_INFO['registry']
    assert set_values['cluster'] == 'test'
    assert set_values['k8s_namespace'] == TEST_CLUSTER_INFO['namespace']
    assert set_values['domain'] == TEST_CLUSTER_INFO['domain']
    assert set_values.get('imageTag')

    def without_build():
        obj = context().obj
        values = obj['values']
        del values['build']
        return tell_helm_options()

    _, options = run_under_click_context(
        without_build,
    )
    set_values_without_build = parse_helm_set_clause_from_options(options)
    assert 'imageTag' not in set_values_without_build
    del set_values['imageTag']
    assert set_values_without_build == set_values

    def with_extra_values_file():
        obj = context().obj
        dic = {'labels': {'foo': 'bar'}}
        f = NamedTemporaryFile(prefix='values-extra', suffix='.yaml')
        yadu(dic, f)
        f.seek(0)
        obj['extra_values_file'] = f
        try:
            return tell_helm_options()
        finally:
            del f

    _, options = run_under_click_context(
        with_extra_values_file,
    )
    extra_values_file_name = basename(options[-1])
    assert extra_values_file_name.startswith('values-')
    assert extra_values_file_name.endswith('.yaml')


@pytest.mark.usefixtures('dummy_helm_chart')
def test_tell_cluster_values_file(mocker):
    cluster_info = dict(CLUSTERS[TEST_CLUSTER])
    default_values = 'values-ent.yaml'
    cluster_info['default-values'] = default_values
    clusters = {TEST_CLUSTER: cluster_info}
    ctx = click.Context(click.Command('cmd'), obj={'prop': 'A Context'})
    with ctx:
        values_file = tell_cluster_values_file()
        assert not values_file
        with open(join(CHART_DIR_NAME, default_values), 'w') as f:
            f.write('')

        mocker.patch('lain_cli.utils.CLUSTERS', clusters)
        values_file = tell_cluster_values_file()
        assert values_file.endswith(default_values)


def parse_helm_set_clause_from_options(options):
    set_clause = options[options.index('--set') + 1]
    pair_list = set_clause.split(',')
    res = {}
    for pair in pair_list:
        k, v = pair.split('=')
        res[k] = v

    return res


@pytest.mark.usefixtures('dummy_helm_chart')
def test_registry():
    region_id = 'cn-hangzhou'
    repo_ns = 'big-company'
    aliyun_registry = AliyunRegistry(
        access_key_id='hh',
        access_key_secret='hh',
        region_id=region_id,
        repo_namespace=repo_ns,
    )
    tag = 'noway'
    _, image = run_under_click_context(
        aliyun_registry.make_image,
        args=[tag],
    )
    assert image == f'registry.{region_id}.aliyuncs.com/{repo_ns}/{DUMMY_APPNAME}:{tag}'
    project = 'foo'
    registry_url = f'harbor.fake/{project}'
    harbor_registry = HarborRegistry(registry_url, 'fake-token')
    tag = 'noway'
    _, image = run_under_click_context(
        harbor_registry.make_image,
        args=[tag],
    )
    assert harbor_registry.host == registry_url
    assert image == f'{registry_url}/{DUMMY_APPNAME}:{tag}'
