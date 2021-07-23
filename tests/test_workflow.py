from copy import deepcopy
from time import sleep

import pytest
import requests
from tenacity import retry, stop_after_attempt, wait_fixed

from lain_cli.lain import lain
from lain_cli.utils import (
    jalo,
    DEFAULT_WORKDIR,
    context,
    docker,
    ensure_str,
    kubectl,
    lain_build,
    lain_image,
    lain_meta,
    make_job_name,
    pick_pod,
    tell_pod_deploy_name,
    yadu,
)
from tests.conftest import (
    BUILD_TREASURE_NAME,
    DUMMY_CANARY_NAME,
    CHART_DIR_NAME,
    DUMMY_APPNAME,
    DUMMY_DEV_URL,
    DUMMY_IMAGE_TAG,
    DUMMY_URL,
    DUMMY_VALUES_PATH,
    RANDOM_STRING,
    TEST_CLUSTER,
    TEST_CLUSTER_INFO,
    load_dummy_values,
    run,
    run_under_click_context,
    tell_deployed_images,
    tell_ing_name,
)


@pytest.mark.usefixtures('dummy_helm_chart')
def test_build(registry):
    stage = 'prepare'

    def _prepare():
        obj = context().obj
        values = obj['values']
        build_clause = values['build']
        build_clause['prepare']['env'] = {'prepare_env': BUILD_TREASURE_NAME}
        build_clause['prepare']['script'].append(
            f'echo {RANDOM_STRING} > {BUILD_TREASURE_NAME}'
        )
        lain_build(stage=stage)

    run_under_click_context(_prepare)
    _, prepare_image = run_under_click_context(lain_image, kwargs={'stage': stage})
    res = docker_run(prepare_image, ['ls'])
    ls_result = parse_ls(res.stdout)
    # ensure keep clause works as expected
    assert ls_result == {BUILD_TREASURE_NAME}
    res = docker_run(prepare_image, ['env'])
    assert f'prepare_env={BUILD_TREASURE_NAME}' in ensure_str(res.stdout)

    stage = 'build'

    def _build_without_prepare():
        obj = context().obj
        values = obj['values']
        build_clause = values['build']
        build_clause['env'] = {'build_env': BUILD_TREASURE_NAME}
        del build_clause['prepare']
        lain_build(stage=stage, push=False)

    run_under_click_context(_build_without_prepare)
    _, build_image = run_under_click_context(lain_image, kwargs={'stage': stage})
    # 这次 build 是虚假的, 没有经过 prepare 步骤, 所以肯定不会有 treasure.txt
    res = docker_run(build_image, ['ls'])
    ls_result = parse_ls(res.stdout)
    assert 'run.py' in ls_result
    assert BUILD_TREASURE_NAME not in ls_result
    res = docker_run(build_image, ['env'])
    assert f'build_env={BUILD_TREASURE_NAME}' in ensure_str(res.stdout)

    def _build():
        obj = context().obj
        values = obj['values']
        build_clause = values['build']
        build_clause['script'].append(f'echo {RANDOM_STRING} >> {BUILD_TREASURE_NAME}')
        lain_build(stage=stage)

    run_under_click_context(_build)
    _, build_image = run_under_click_context(lain_image, kwargs={'stage': stage})
    res = docker_run(build_image, ['env'])
    env_lines = ensure_str(res.stdout).splitlines()
    _, meta = run_under_click_context(lain_meta)
    assert f'LAIN_META={meta}' in env_lines
    res = docker_run(build_image, ['cat', BUILD_TREASURE_NAME])
    treasure = ensure_str(res.stdout).strip()
    # 这个文件被我打印了两次随机串进去, 因此应该就两行...无聊的测试
    assert treasure == f'{RANDOM_STRING}\n{RANDOM_STRING}'
    res = docker_run(build_image, ['ls'])
    ls_result = parse_ls(res.stdout)
    assert 'run.py' in ls_result
    run(lain, args=['push'])
    recent_tags = registry.list_tags(DUMMY_APPNAME)
    latest_tag = next(t for t in recent_tags if t != 'latest')
    assert build_image.rsplit(':', 1)[-1] == latest_tag

    stage = 'release'

    def _release():
        obj = context().obj
        values = obj['values']
        values['release'] = {
            'env': {'release_env': BUILD_TREASURE_NAME},
            'dest_base': 'ccr.ccs.tencentyun.com/yashi/ubuntu-python:latest',
            'workdir': DEFAULT_WORKDIR,
            'script': [],
            'copy': [
                {'src': '/lain/app/treasure.txt', 'dest': '/lain/app/treasure.txt'},
                {'src': '/lain/app/treasure.txt', 'dest': '/etc'},
            ],
        }
        lain_build(stage=stage, push=False)

    run_under_click_context(_release)
    _, release_image = run_under_click_context(lain_image, kwargs={'stage': stage})
    res = docker_run(release_image, ['ls'])
    ls_result = parse_ls(res.stdout)
    assert ls_result == {BUILD_TREASURE_NAME}
    res = docker_run(release_image, ['ls', '-alh', f'/etc/{BUILD_TREASURE_NAME}'])
    ls_result = ensure_str(res.stdout).strip()
    # check file permission
    assert '1001 1001' in ls_result
    assert ls_result.endswith(f'/etc/{BUILD_TREASURE_NAME}')
    res = docker_run(release_image, ['cat', BUILD_TREASURE_NAME])
    treasure = ensure_str(res.stdout).strip()
    # 构建 release 镜像的时候, 由于并没有超载 build.script, 因此 treasure
    # 里只有一行
    assert treasure == f'{RANDOM_STRING}'
    res = docker_run(release_image, ['env'])
    assert f'release_env={BUILD_TREASURE_NAME}' in ensure_str(res.stdout)


@pytest.mark.first
@pytest.mark.usefixtures('dummy')
def test_workflow(registry):
    # lain init should failed when chart directory already exists
    run(lain, args=['init'], returncode=1)
    # use -f to remove chart directory and redo
    run(lain, args=['init', '-f'])
    # lain use will switch current context switch to [TEST_CLUSTER]
    run(lain, args=['use', TEST_CLUSTER])
    # lain use will print current cluster
    res = run(lain, args=['use'])
    assert res.stdout == f'currently on {TEST_CLUSTER}\n'
    # see if this image is actually present on registry
    res = run(lain, args=['meta'])
    image_tag = res.stdout.strip()
    # try to lain deploy using a bad image tag
    run(lain, args=['deploy', '--set', 'imageTag=noway'], returncode=1)
    init_container_name = 'proof'
    override_values = {
        # 随便加一个 job, 目的是为了看第二次部署的时候能否顺利先清理掉这个 job
        'jobs': {
            'init': {
                'initContainers': [
                    {
                        'name': init_container_name,
                        'command': ['echo', RANDOM_STRING],
                    }
                ],
                'imagePullPolicy': 'Always',
                'command': ['bash', '-c', 'echo nothing >> README.md'],
            },
        },
    }
    yadu(override_values, f'{CHART_DIR_NAME}/values-{TEST_CLUSTER}.yaml')
    # use a built image to deploy
    run(lain, args=['--ignore-lint', 'deploy', '--set', f'imageTag={image_tag}'])
    # check service is up
    dummy_response = url_get_json(DUMMY_URL)
    assert dummy_response['env']['FOO'] == 'BAR'
    assert dummy_response['secretfile'] == 'I\nAM\nBATMAN'
    # check if hostAliases is working
    assert 'ccs.tencent-cloud.com' in dummy_response['hosts']
    assert 'dead.end' in dummy_response['hosts']
    # check imageTag is correct
    deployed_images = tell_deployed_images(DUMMY_APPNAME)
    assert len(deployed_images) == 1
    deployed_image = deployed_images.pop()
    assert deployed_image.endswith(image_tag)

    # check if init job succeeded
    wait_for_job_success()
    # run a extra job, to test lain job functionalities
    command = 'env'
    res = run(lain, args=['job', '--force', command])
    _, job_name = run_under_click_context(make_job_name, args=(command,))
    wait_for_job_success(job_name)
    _, pod_name = run_under_click_context(
        pick_pod, kwargs={'containerStatuses': 'Completed'}
    )
    logs_res = kubectl('logs', pod_name, capture_output=True)
    logs = ensure_str(logs_res.stdout)
    assert 'FOO=BAR' in logs
    # 跑第二次只是为了看看清理过程能否顺利执行, 保证不会报错
    run(lain, args=['job', '--force', 'env'])

    values = load_dummy_values()
    # example 里用的是 IfNotPresent, 这是合理的默认值
    # 但是对于测试来说, 肯定希望每次都拉一下, 因为 dummy 这个应用会反复构建
    # push, 需要隔离构建错误带来的问题
    web_proc = values['deployments']['web']
    web_proc.update(
        {
            'imagePullPolicy': 'Always',
            'terminationGracePeriodSeconds': 1,
        }
    )
    # add one extra ingress rule to values.yaml
    dev_host = f'{DUMMY_APPNAME}-dev'
    full_host = 'dummy.full.domain'
    values['ingresses'].extend(
        [
            {'host': dev_host, 'deployName': 'web-dev', 'paths': ['/']},
            {'host': full_host, 'deployName': 'web', 'paths': ['/']},
        ]
    )
    values['jobs'] = {'init': {'command': ['echo', 'migrate']}}
    yadu(values, DUMMY_VALUES_PATH)
    overrideReplicaCount = 3
    overrideImageTag = 'latest'
    # add another env
    run(lain, args=['env', 'add', 'SCALE=BANANA'])
    web_dev_proc = deepcopy(web_proc)
    web_dev_proc.update(
        {
            'replicaCount': overrideReplicaCount,
            'imageTag': overrideImageTag,
        }
    )
    # adjust replicaCount and imageTag in override values file
    override_values = {
        'deployments': {
            'web-dev': web_dev_proc,
        },
        # this is just used to ensure helm template rendering
        'ingressAnnotations': {
            'nginx.ingress.kubernetes.io/proxy-next-upstream-timeout': 1,
        },
        'externalIngresses': [
            {'host': 'dummy-public.foo.cn', 'deployName': 'web', 'paths': ['/']},
            {'host': 'dummy-public.bar.cn', 'deployName': 'web', 'paths': ['/']},
        ],
    }
    yadu(override_values, f'{CHART_DIR_NAME}/values-{TEST_CLUSTER}.yaml')

    def get_helm_values():
        ctx = context()
        helm_values = ctx.obj['values']
        return helm_values

    # check if values-[TEST_CLUSTER].yaml currectly overrides helm context
    _, helm_values = run_under_click_context(get_helm_values)
    assert helm_values['deployments']['web-dev']['replicaCount'] == overrideReplicaCount

    # deploy again to create newly added ingress rule
    run(lain, args=['deploy', '--set', f'imageTag={DUMMY_IMAGE_TAG}'])
    # check if the new ingress rule is created
    res = kubectl(
        'get',
        'ing',
        '-l',
        f'app.kubernetes.io/name={DUMMY_APPNAME}',
        '-o=jsonpath={..name}',
        capture_output=True,
    )
    assert not res.returncode
    domain = TEST_CLUSTER_INFO['domain']
    assert set(res.stdout.decode('utf-8').split()) == {
        tell_ing_name(full_host, DUMMY_APPNAME, domain, 'web'),
        tell_ing_name(DUMMY_APPNAME, DUMMY_APPNAME, domain, 'web'),
        f'dummy-public-foo-cn-{DUMMY_APPNAME}-web',
        tell_ing_name(dev_host, DUMMY_APPNAME, domain, 'web-dev'),
        f'dummy-public-bar-cn-{DUMMY_APPNAME}-web',
    }
    # check pod name match its corresponding deploy name
    dummy_response = url_get_json(DUMMY_URL)
    assert (
        tell_pod_deploy_name(dummy_response['env']['HOSTNAME'])
        == f'{DUMMY_APPNAME}-web'
    )
    dummy_dev_env = url_get_json(DUMMY_DEV_URL)
    assert (
        tell_pod_deploy_name(dummy_dev_env['env']['HOSTNAME'])
        == f'{DUMMY_APPNAME}-web-dev'
    )
    # env is overriden in dummy-dev, see default values.yaml
    assert dummy_dev_env['env']['FOO'] == 'BAR'
    assert dummy_dev_env['env']['SCALE'] == 'BANANA'
    assert dummy_dev_env['env']['LAIN_CLUSTER'] == TEST_CLUSTER
    assert dummy_dev_env['env']['K8S_NAMESPACE'] == TEST_CLUSTER_INFO.get(
        'namespace', 'default'
    )
    assert dummy_dev_env['env']['IMAGE_TAG'] == DUMMY_IMAGE_TAG
    # check if replicaCount is correctly overriden
    res = kubectl(
        'get',
        'deploy',
        f'{DUMMY_APPNAME}-web-dev',
        '-o=jsonpath={.spec.replicas}',
        capture_output=True,
    )
    assert res.stdout.decode('utf-8').strip() == str(overrideReplicaCount)
    # check if imageTag is correctly overriden
    web_image = get_deploy_image(f'{DUMMY_APPNAME}-web')
    assert web_image.endswith(DUMMY_IMAGE_TAG)
    web_dev_image = get_deploy_image(f'{DUMMY_APPNAME}-web-dev')
    assert web_dev_image.endswith(overrideImageTag)
    # rollback imageTag for web-dev using `lain update_image`
    run(lain, args=['update-image', 'web-dev'])


@pytest.mark.second
@pytest.mark.usefixtures('dummy')
def test_canary():
    res = run(lain, args=['deploy', '--canary'], returncode=1)
    assert 'cannot initiate canary deploy' in ensure_str(res.output)
    run(lain, args=['deploy'])
    res = run(lain, args=['deploy', '--canary'])
    assert 'canary version has been deployed' in ensure_str(res.output)
    res = run(lain, args=['deploy'], returncode=1)
    assert 'cannot proceed due to on-going canary deploy' in ensure_str(res.output)
    resp = url_get_json(DUMMY_URL)
    assert resp['env']['HOSTNAME'].startswith(f'{DUMMY_APPNAME}-web')
    res = run(lain, args=['set-canary-group', 'internal'], returncode=1)
    assert 'canaryGroups not defined in values' in ensure_str(res.output)
    # inject canary annotations for test purpose
    values = load_dummy_values()
    canary_header_name = 'canary'
    values['canaryGroups'] = {
        'internal': {
            'nginx.ingress.kubernetes.io/canary-by-header': canary_header_name
        },
    }
    yadu(values, DUMMY_VALUES_PATH)
    run(lain, args=['set-canary-group', 'internal'])
    ings_res = kubectl(
        'get',
        'ing',
        '-ojson',
        '-l',
        f'helm.sh/chart={DUMMY_CANARY_NAME}',
        capture_output=True,
    )
    ings = jalo(ings_res.stdout)
    for ing in ings['items']:
        annotations = ing['metadata']['annotations']
        assert (
            annotations['nginx.ingress.kubernetes.io/canary-by-header']
            == canary_header_name
        )

    canary_header = {canary_header_name: 'always'}
    resp = url_get_json(DUMMY_URL, headers=canary_header)
    assert resp['env']['HOSTNAME'].startswith(f'{DUMMY_CANARY_NAME}-web')
    run(lain, args=['set-canary-group', '--abort'])
    run(lain, args=['wait'])
    assert f'{DUMMY_CANARY_NAME}-web' not in get_dummy_pod_names()
    registry = TEST_CLUSTER_INFO['registry']
    values['tests'] = {
        'simple-test': {
            'image': f'{registry}/lain:latest',
            'command': [
                'bash',
                '-ec',
                '''
                lain -v wait dummy
                ''',
            ],
        },
    }
    yadu(values, DUMMY_VALUES_PATH)
    tag = 'latest'
    run(lain, args=['deploy', '--set', f'imageTag={tag}', '--canary'])
    run(lain, args=['set-canary-group', '--final'])
    assert f'{DUMMY_CANARY_NAME}-web' not in get_dummy_pod_names()
    image = get_deploy_image(f'{DUMMY_APPNAME}-web')
    assert image.endswith(f':{tag}')


@retry(reraise=True, wait=wait_fixed(2), stop=stop_after_attempt(6))
def url_get_json(url, **kwargs):
    sleep(4)
    res = requests.get(url, **kwargs)
    res.raise_for_status()
    return res.json()


def get_dummy_pod_names():
    res = kubectl(
        'get',
        'po',
        '-ocustom-columns=:metadata.name',
        '-l',
        f'app.kubernetes.io/name={DUMMY_APPNAME}',
        capture_output=True,
    )
    return ensure_str(res.stdout)


def get_deploy_image(deploy_name):
    res = kubectl(
        'get',
        'deploy',
        deploy_name,
        '-o=jsonpath={.spec.template.spec..image}',
        capture_output=True,
    )
    return res.stdout.decode('utf-8').strip()


@retry(reraise=True, wait=wait_fixed(1), stop=stop_after_attempt(6))
def wait_for_job_success(job_name=None):
    if not job_name:
        job_name = f'{DUMMY_APPNAME}-init'

    sleep(2)
    res = kubectl(
        'get',
        'po',
        '-o=jsonpath={..phase}',
        '-l',
        f'job-name={job_name}',
        capture_output=True,
    )
    assert ensure_str(res.stdout).startswith('Succeeded')


def docker_run(image, cmd, name=None):
    name_clause = ('--name', name) if name else []
    return docker('run', '--rm', *name_clause, image, *cmd, capture_output=True)


def parse_ls(s):
    return set(ensure_str(s).strip().split())
