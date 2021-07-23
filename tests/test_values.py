import pytest
from marshmallow import ValidationError

from lain_cli.utils import (
    HelmValuesSchema,
    IngressSchema,
    context,
    load_helm_values,
    make_wildcard_domain,
    tell_domain_tls_name,
    yadu,
)
from tests.conftest import (
    BUILD_TREASURE_NAME,
    DUMMY_APPNAME,
    DUMMY_VALUES_PATH,
    RANDOM_STRING,
    TEST_CLUSTER,
    TEST_CLUSTER_INFO,
    dic_contains,
    load_dummy_values,
    render_k8s_specs,
    run_under_click_context,
    tell_ing_name,
)


@pytest.mark.usefixtures('dummy_helm_chart')
def test_values():
    values = load_dummy_values()
    domain = TEST_CLUSTER_INFO['domain']
    values['env'] = {'SOMETHING': 'ELSE'}
    ing_anno = {'fake-annotations': 'bar'}
    values['ingresses'] = [
        {'host': 'dummy', 'deployName': 'web', 'paths': ['/'], 'annotations': ing_anno},
        {'host': f'dummy.{domain}', 'deployName': 'web', 'paths': ['/']},
    ]
    values['externalIngresses'] = [
        {
            'host': 'dummy.public.com',
            'deployName': 'web',
            'paths': ['/'],
            'annotations': ing_anno,
        },
        {'host': 'public.com', 'deployName': 'web', 'paths': ['/']},
    ]
    values['labels'] = {'foo': 'bar'}
    web_proc = values['deployments']['web']
    nodePort = 32333
    web_proc.update(
        {
            'podAnnotations': {'prometheus.io/scrape': 'true'},
            'workingDir': RANDOM_STRING,
            'hostNetwork': True,
            'nodePort': nodePort,
            'nodes': ['node-1'],
        }
    )
    yadu(values, DUMMY_VALUES_PATH)
    k8s_specs = render_k8s_specs()
    ingresses = [spec for spec in k8s_specs if spec['kind'] == 'Ingress']
    domain = TEST_CLUSTER_INFO['domain']
    internal_ing = next(
        ing
        for ing in ingresses
        if ing['metadata']['name']
        == tell_ing_name(DUMMY_APPNAME, DUMMY_APPNAME, domain, 'web')
    )
    dic_contains(internal_ing['metadata']['annotations'], ing_anno)
    dummy_public_com = next(
        ing
        for ing in ingresses
        if ing['metadata']['name'] == 'dummy-public-com-dummy-web'
    )
    dic_contains(dummy_public_com['metadata']['annotations'], ing_anno)
    for ing in ingresses:
        spec = ing['spec']
        rule = spec['rules'][0]
        domain = rule['host']
        tls = ing['spec']['tls']
        tls_name = tls[0]['secretName']
        tls_hosts = tls[0]['hosts']
        assert set(tls_hosts) == set(make_wildcard_domain(domain))
        assert (
            rule['http']['paths'][0]['backend']['service']['port']['number'] == nodePort
        )
        assert tls_name == tell_domain_tls_name(tls_hosts[0])

    deployment = next(spec for spec in k8s_specs if spec['kind'] == 'Deployment')
    # check if podAnnotations work
    assert (
        deployment['spec']['template']['metadata']['annotations'][
            'prometheus.io/scrape'
        ]
        == 'true'
    )
    container_spec = deployment['spec']['template']['spec']
    assert container_spec['hostNetwork'] is True
    assert container_spec['containers'][0]['workingDir'] == RANDOM_STRING
    env_dic = {}
    for pair in container_spec['containers'][0]['env']:
        env_dic[pair['name']] = pair['value']

    assert env_dic == {
        'LAIN_CLUSTER': TEST_CLUSTER,
        'K8S_NAMESPACE': TEST_CLUSTER_INFO['namespace'],
        'IMAGE_TAG': 'overridden-during-deploy',
        'SOMETHING': 'ELSE',
        'FOO': 'BAR',
    }
    assert container_spec['affinity']['nodeAffinity']
    assert deployment['metadata']['labels']['foo'] == 'bar'
    match_expression = container_spec['affinity']['nodeAffinity'][
        'requiredDuringSchedulingIgnoredDuringExecution'
    ]['nodeSelectorTerms'][0]['matchExpressions'][0]
    assert match_expression['key'] == f'{DUMMY_APPNAME}-web'

    service = next(spec for spec in k8s_specs if spec['kind'] == 'Service')
    service_spec = service['spec']
    port = service_spec['ports'][0]
    assert port['nodePort'] == port['port'] == nodePort
    assert port['targetPort'] == 5000


@pytest.mark.usefixtures('dummy_helm_chart')
def test_publish_to():
    values = load_dummy_values()
    values['publish_to'] = ['test']
    yadu(values, DUMMY_VALUES_PATH)

    def _get_publish_to_registries():
        obj = context().obj
        return obj['values']['publish_to_registries']

    _, registries = run_under_click_context(_get_publish_to_registries)
    assert registries == {TEST_CLUSTER_INFO['registry']}


@pytest.mark.usefixtures('dummy_helm_chart')
def test_duplicate_proc_names():
    values = load_dummy_values()
    web = values['deployments']['web'].copy()
    del web['resources']
    values['cronjobs'] = {'web': web}
    with pytest.raises(ValidationError) as e:
        HelmValuesSchema().load(values)

    assert 'proc names should not duplicate' in str(e)


@pytest.mark.usefixtures('dummy_helm_chart')
def test_schemas():
    bare_values = load_dummy_values()
    yadu(bare_values, DUMMY_VALUES_PATH)
    _, values = run_under_click_context(load_helm_values, (DUMMY_VALUES_PATH,))
    assert values['cronjobs'] == {}
    build = values['build']
    assert build['prepare']['keep'] == [BUILD_TREASURE_NAME]

    bare_values['volumeMounts'][0]['subPath'] = 'foo/bar'  # should be basename
    with pytest.raises(ValidationError) as e:
        HelmValuesSchema().load(bare_values)

    assert 'subPath should be' in str(e)

    false_ing = {'host': 'dummy', 'deployName': 'web'}
    with pytest.raises(ValidationError):
        IngressSchema().load(false_ing)

    bad_web = {'containerPort': 8000}
    with pytest.raises(ValidationError):
        IngressSchema().load(bad_web)
