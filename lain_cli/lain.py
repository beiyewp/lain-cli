#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
from copy import deepcopy
import shutil
import sys
from functools import partial
from os import getcwd as cwd
from os.path import basename, dirname, expanduser, isfile, join

import click
import packaging
import sentry_sdk
from click import BadParameter
from humanfriendly import InvalidTimespan, parse_size, parse_timespan

from lain_cli import __version__
from lain_cli.clusters import SENTRY_DSN
from lain_cli.gitlab import fetch_chart as fetch_chart_
from lain_cli.gitlab import validate_repo_name
from lain_cli.kibana import Kibana
from lain_cli.lint import (
    suggest_cpu_limits,
    suggest_cpu_requests,
    suggest_memory_limits,
    suggest_memory_requests,
)
from lain_cli.prometheus import Prometheus
from lain_cli.prompt import (
    build_app_status_command,
    display_app_status,
    display_cluster_status,
    ingress_text,
    pod_text,
    top_text,
)
from lain_cli.tencent import TencentClient
from lain_cli.utils import (
    get_pods,
    CHART_DIR_NAME,
    CHART_TEMPLATE_DIR,
    CHART_VERSION,
    CLUSTERS,
    DOCKER_COMPOSE_FILE_PATH,
    HELM_WEIRD_STATE,
    RECENT_TAGS_COUNT,
    KVPairType,
    banyun,
    clean_canary_ingress_annotations,
    click_parse_timespan,
    debug,
    deploy_toast,
    docker,
    docker_images,
    docker_save,
    dump_secret,
    echo,
    ensure_absent,
    ensure_helm_initiated,
    ensure_resource_initiated,
    ensure_str,
    error,
    find,
    get_app_status,
    get_pod_rc,
    git,
    goodjob,
    helm,
    helm_delete,
    init_done_toast,
    is_inside_cluster,
    is_values_file,
    jalo,
    kubectl,
    kubectl_apply,
    kubectl_edit,
    lain_,
    lain_build,
    lain_meta,
    make_canary_name,
    make_image_str,
    make_job_name,
    parse_kubernetes_cpu,
    pick_pod,
    rc,
    stern,
    tell_best_deploy,
    tell_cluster,
    tell_cluster_info,
    tell_grafana_url,
    tell_helm_options,
    tell_image,
    tell_image_tag,
    open_kibana_url,
    tell_kibana_url,
    tell_pod_deploy_name,
    tell_registry_client,
    tell_release_image,
    tell_secret,
    template_env,
    template_update_toast,
    too_much_logs_headsup,
    top_procs,
    try_lain_prepare,
    try_to_cleanup_job,
    try_to_label_nodes,
    try_to_print_job_logs,
    user_challenge,
    validate_proc_name,
    version_challenge,
    wait_for_cluster_up,
    wait_for_pod_up,
    wait_for_svc_up,
    warn,
    welcome_check,
    yadu,
    yalo,
)
from lain_cli.webhook import tell_webhook_client


@click.group()
@click.option('--silent', '-s', is_flag=True, help='log as little text as possible')
@click.option('--verbose', '-v', is_flag=True)
@click.option(
    '--ignore-lint',
    is_flag=True,
    envvar='LAIN_IGNORE_LINT',
    help='do not run lain lint before deploy',
)
@click.option(
    '--values', '-f', type=click.File('r'), help='specify one extra helm values file'
)
@click.option(
    '--use',
    type=click.Choice(CLUSTERS),
    help='run lain use first, and then proceed. use this if you are afraid of accidentally execute command towards the wrong cluster',
)
@click.pass_context
def lain(ctx, silent, verbose, ignore_lint, values, use):
    """DevOps with minimal effort"""
    ctx.obj['silent'] = silent
    ctx.obj['verbose'] = verbose
    ctx.obj['ignore_lint'] = ignore_lint
    ctx.obj['extra_values_file'] = values
    SENTRY_DSN and sentry_sdk.init(SENTRY_DSN, traces_sample_rate=0)
    try:
        ensure_helm_initiated()
        version_challenge()
    except (OSError, KeyError):
        pass
    if use:
        if use == tell_cluster():
            echo(f'you are already here: {use}')
        else:
            lain_('use', use)


@lain.group()
def admin():
    """admin functionalities, stay away"""


@admin.command()
@click.option(
    '--dry-run',
    is_flag=True,
)
def delete_bad_pod(dry_run):
    jobs = kubectl(
        'get',
        'job',
        '--no-headers',
        '-ojsonpath={..metadata.name}',
        capture_output=True,
    )
    job_names = tuple(ensure_str(jobs.stdout).split())
    _, pods = get_pods(show_only_bad_pods=True, check=True)

    def is_job(pod_name):
        for job_name in job_names:
            if pod_name.startswith(job_name):
                return job_name

    for line in pods:
        pod_name, _, state, *_ = line.split()
        job_name = is_job(pod_name)
        if job_name:
            resource_type = 'job'
            resource_name = job_name
        else:
            resource_type = 'pod'
            resource_name = pod_name

        kubectl('delete', resource_type, resource_name, check=False, dry_run=dry_run)


@admin.command()
def cleanup_registry():
    res = kubectl('get', 'po', '-ojsonpath={..image}', capture_output=True)
    running_image_tags = frozenset(
        [image.split(':', 1)[-1] for image in ensure_str(res.stdout).split()]
    )
    protected_tags = {'prepare', 'latest'}
    registry = tell_registry_client()
    repos = registry.list_repos()
    for repo in repos:
        if registry.is_protected_repo(repo):
            continue
        tags = set(registry.list_tags(repo))
        recent_tags = frozenset(registry.sort_and_filter(tags)[:20])
        ancient_tags = tags - recent_tags - protected_tags - running_image_tags
        for tag in ancient_tags:
            res = registry.delete_image(repo, tag)
            debug(f'delete {repo}:{tag}, {res}')


@admin.command()
@click.pass_context
def status(ctx):
    ctx.obj['silent'] = True
    display_cluster_status()


@admin.command()
@click.argument('command', nargs=-1)
@click.pass_context
def x(ctx, command):
    """run command on all containers (one for each deployment) within current
    namespace.  only show output when command succeeds

    \b
    examples:
    \b
        lain admin x -- bash -c 'pip3 freeze | grep -i requests'
    """
    res = kubectl('get', 'po', '--no-headers', capture_output=True)
    ctx.obj['silent'] = True
    deploy_names = set()
    for line in ensure_str(res.stdout).splitlines():
        podname, *_ = line.split()
        deploy_name = tell_pod_deploy_name(podname)
        if deploy_name in deploy_names:
            continue
        deploy_names.add(deploy_name)
        res = kubectl(
            'exec',
            '-it',
            podname,
            '--',
            *command,
            check=False,
            timeout=None,
            capture_output=True,
        )
        if rc(res):
            stderr = ensure_str(res.stderr)
            # abort execution in the case of network error
            if 'unable to connect' in stderr.lower() or 'timeout' in stderr:
                error(stderr, exit=1)
            continue
        echo(f'command succeeds for {podname}')
        echo(res.stdout)


@admin.command()
@click.argument('instance_ids', nargs=-1)
def stop_cvm(instance_ids):
    client = TencentClient()
    client.turn_(InstanceIds=instance_ids, state='off')


@admin.command()
@click.argument('instance_ids', nargs=-1)
def start_cvm(instance_ids):
    client = TencentClient()
    client.turn_(InstanceIds=instance_ids)


@admin.command()
@click.argument('state', nargs=1, type=click.Choice(TencentClient.VM_STATES))
@click.pass_context
def turn(ctx, state):
    """\b
    turn off currently used cluster, to save money"""
    current_state = wait_for_cluster_up()
    if current_state != state:
        cluster = ctx.obj['cluster']
        client = TencentClient()
        client.turn_(cluster=cluster, state=state)

    if state == 'on':
        final_state = wait_for_cluster_up(tries=120)
        if final_state != 'on':
            error(f'cluster {cluster} still not up')


@admin.command()
def list_waste():
    deploy_list = ensure_str(
        kubectl('get', 'deploy', '--no-headers', capture_output=True).stdout
    ).splitlines()
    helm_release_names = set(
        ensure_str(helm('list', '--short', capture_output=True).stdout).split()
    )
    prometheus = Prometheus()
    for line in deploy_list:
        name, actual_desired, *_ = line.split()
        desired = int(actual_desired.split('/')[-1])
        if desired < 2:
            continue
        appname, proc_name = name.rsplit('-', 1)
        if appname not in helm_release_names:
            continue
        cpu_top = prometheus.cpu_p95(appname, proc_name)
        if not cpu_top:
            warn(f'skipping {appname} because cpu data is not available')
            continue

        error(f'{appname}-{proc_name} has {desired} pods, cpu P90: {cpu_top}')


@admin.command()
@click.option(
    '--count-below',
    default=1,
    help='list ingress that has been requested fewer than this amount',
)
@click.option('--period', default='7d', help='query timespan')
@click.pass_context
def list_unused_ingress(ctx, count_below, period):
    ctx.obj['silent'] = True
    WEEK = parse_timespan('7d')
    ing_list = ensure_str(
        kubectl(
            'get',
            'ing',
            '--no-headers',
            r'-o=custom-columns=NAME:.metadata.name,HOST:..rules[*].host,CLASS:..annotations.kubernetes\.io/ingress\.class',
            capture_output=True,
        ).stdout
    ).splitlines()
    kibana = Kibana()
    svcs = set()
    for line in ing_list:
        ing_name, host, ingress_class = line.split()
        if host.endswith('.lain'):
            continue
        query_count = kibana.count_records_for_host(
            host, ingress_class=ingress_class, period=period
        )
        if query_count < count_below:
            stdout = ensure_str(
                kubectl(
                    'get',
                    'ing',
                    ing_name,
                    '-ocustom-columns=FOO:..serviceName,BAR:..service.name',
                    '--no-headers',
                    capture_output=True,
                ).stdout
            )
            svc_name = [s for s in stdout.split() if s != '<none>'][0]
            if svc_name in svcs:
                debug(f'svc already seen, skip: {svc_name}')
                continue
            svc_res = kubectl(
                'get', 'svc', svc_name, '-ojson', capture_output=True, check=False
            )
            if rc(svc_res):
                stderr = ensure_str(svc_res.stderr)
                if 'not found' in stderr:
                    debug(f'{ing_name} had bad svc: {svc_name}')
                    echo(f'k delete ing {ing_name}')
                    continue
                error(f'weird error during getting svc: {stderr}', exit=1)
            else:
                svcs.add(svc_name)

            svc = ensure_str(svc_res.stdout)
            svc_dic = jalo(svc)
            selectors = ','.join(
                [f'{k}={v}' for k, v in svc_dic['spec']['selector'].items()]
            )
            pods = ensure_str(
                kubectl(
                    'get', 'po', '--no-headers', '-l', selectors, capture_output=True
                ).stdout
            ).replace('\n', '')
            try:
                age = parse_timespan(pods.rsplit(' ', 1)[-1])
                if age < WEEK:
                    debug(f'{ing_name} has young pods, skip')
                    continue
            except InvalidTimespan:
                pass
            if not pods:
                debug(f'{ing_name} has no pods')
                echo(f'k delete ing {ing_name}')
                continue

            pod_name = pods.split(None, 1)[0]
            period_s = int(parse_timespan(period))
            log_res = kubectl(
                'logs', f'--since={period_s}s', pod_name, capture_output=True
            )
            if log_res.stdout:
                debug(f'pod {pod_name} is still printing logs, skip')
                continue
            echo(f'{host}\t{query_count}\t{pods}')


@lain.command()
@click.option(
    '--simple',
    is_flag=True,
    help='skip more complicated lint checks, like resources suggestions',
)
@click.pass_context
def lint(ctx, simple):
    """offers suggestions on writing helm values"""
    if ctx.obj['ignore_lint']:
        goodjob(
            'you just ran lain lint using --ignore-lint, what a great way to use this command',
            exit=True,
        )

    options = tell_helm_options((), deduce_image=False)
    helm('lint', f'./{CHART_DIR_NAME}', *options, capture_output=True)
    # check chart version
    chart_yaml = f'./{CHART_DIR_NAME}/Chart.yaml'
    chart = yalo(open(chart_yaml))
    current_version_str = chart.get('version') or '0.1.0'
    current_version = packaging.version.parse(current_version_str)
    if current_version < CHART_VERSION:
        error(f'chart version too low: {current_version}')
        error(
            f'to fix this, run lain init --template-only --commit, or change {chart_yaml}:version to a larger value, to prove that you don\'t need the built-in helm chart anymore',
            exit=True,
        )

    if simple:
        ctx.exit(0)

    appname = ctx.obj.get('appname')
    if not appname:
        return
    tops = top_procs(appname)
    for proc_name, proc in tops.items():
        resources = proc['resources']
        requests, limits = resources['requests'], resources['limits']
        cpu_limit = parse_kubernetes_cpu(limits['cpu'])
        if cpu_limits_suggest_str := suggest_cpu_limits(cpu_limit):
            warn(
                f'{proc_name} cpu limits: current {cpu_limit}, suggestion {cpu_limits_suggest_str}'
            )

        memory_top = proc['memory_top']
        if not memory_top:
            # lack of memory_top indicates data loss
            continue
        cpu_requests_str = requests['cpu']
        cpu_requests = parse_kubernetes_cpu(cpu_requests_str)
        if cpu_requests_suggest_str := suggest_cpu_requests(
            cpu_requests, proc['cpu_top']
        ):
            error(
                f'{proc_name} cpu requests: current {cpu_requests_str}, suggestion {cpu_requests_suggest_str}'
            )

        memory_requests_str, memory_limits_str = requests['memory'], limits['memory']
        memory_requests = parse_size(memory_requests_str, binary=True)
        if memory_requests_suggest_str := suggest_memory_requests(
            memory_requests, memory_top
        ):
            error(
                f'{proc_name} memory requests: current {memory_requests_str}, suggestion: {memory_requests_suggest_str}'
            )

        memory_limits = parse_size(memory_limits_str, binary=True)
        if memory_limits_suggest_str := suggest_memory_limits(
            memory_limits, memory_top
        ):
            error(
                f'{proc_name} memory limits: current {memory_limits_str}, suggestion {memory_limits_suggest_str}'
            )

    last_error = ctx.obj.get('last_error')
    if last_error:
        debug(f'last error: {last_error}')
        echo('', exit=1)


@lain.command()
@click.argument('project_name', nargs=1, callback=validate_repo_name)
@click.option(
    '--dir',
    'output_dir',
    default=cwd(),
    help='directory name in which chart will be saved',
)
def fetch_chart(project_name, output_dir):
    """download the helm chart of a project.

    \b
    to manage apps using lain, you should have the corresponding git repo.
    but when you don't, you can download the helm chart using this command,
    and then you'll be able to work with the basic lain commands:

    \b
        lain fetch-chart dev/avln-server
        cd dev/avln-server
        lain logs
    """
    fetch_chart_(project_name, output_dir=output_dir)


@lain.command()
@click.option(
    '--appname',
    default=lambda: basename(cwd()),
    help='name of the app, default to dirname of cwd',
)
@click.option(
    '--force', '-f', is_flag=True, help=f'delete {CHART_DIR_NAME} before proceed'
)
@click.option(
    '--template-only',
    is_flag=True,
    help='only upgrade helm templates, and do not modify values*.yaml',
)
@click.option(
    '--commit',
    is_flag=True,
    help='git commit directly after helm template is upgraded',
)
@click.pass_context
def init(ctx, appname, force, template_only, commit):
    """generate a helm chart for your app."""
    # just using this command to ensure kubectl is downloaded
    kubectl('version', '--client=true', capture_output=True)
    ctx.obj['appname'] = appname
    if force:
        ensure_absent(CHART_DIR_NAME)

    try:
        os.mkdir(CHART_DIR_NAME)
    except FileExistsError:
        if not template_only:
            err = f'''Cannot render helm chart because Directory ./{CHART_DIR_NAME} already exists.
            If you really wanna do this again, use the -f option'''
            error(err)
            ctx.exit(1)

    for f in find(CHART_TEMPLATE_DIR):
        if is_values_file(f) and template_only:
            continue
        render_dest = join(CHART_DIR_NAME, f.replace('.j2', '', 1))
        if f.endswith('.j2'):
            template = template_env.get_template(basename(f))
            with open(render_dest, 'w') as f:
                f.write(template.render(**ctx.obj))
        else:
            src = join(CHART_TEMPLATE_DIR, f)
            os.makedirs(dirname(render_dest), exist_ok=True)
            shutil.copyfile(src, render_dest)

    if commit:
        git('add', 'chart')
        git('restore', '--staged', 'chart/values*.yaml')
        git('commit', '-m', f'[skip ci] upgrade helm chart to {CHART_VERSION}')

    if not ctx.obj['ignore_lint']:
        lain_('lint', '--simple')

    if template_only:
        template_update_toast()
    else:
        init_done_toast()


@lain.command()
@click.option('--simple', '-s', is_flag=True, help='the brief version')
@click.pass_context
def status(ctx, simple):
    """view app status"""
    # we don't want stderr outputs to mess with our full screen application
    ctx.obj['silent'] = True
    if simple:
        grafana_url = tell_grafana_url()
        if grafana_url:
            echo(f'grafana url: {grafana_url}')

        build_app_status_command()
        report = [pod_text(too_many_pods=False)]
        report.append(top_text(too_many_pods=False))
        report.append(ingress_text())
        echo('\n'.join(report))
        ctx.exit(0)

    display_app_status()


@lain.command()
@click.argument('proc_or_appname', nargs=-1)
@click.option(
    '--tail',
    default=200,
    type=click.INT,
    help='defaults to 200, use -1 to print all logs',
)
@click.option(
    '--stern',
    'use_stern',
    is_flag=True,
    help='use stern instead of kubectl, which is better looking',
)
@click.option(
    '--kibana',
    'use_kibana',
    is_flag=True,
    help='use stern instead of kubectl, which is better looking',
)
@click.pass_context
def logs(ctx, proc_or_appname, tail, use_stern, use_kibana):
    """
    print container logs.

    \b
    examples:
    \b
        lain logs
        lain logs web
        lain logs ANOTHER-APP
        lain logs ANOTHER-APP web
    """
    if use_kibana and use_stern:
        raise BadParameter('cannot use --stern with --kibana')

    appname = ctx.obj.get('appname')
    values = ctx.obj.get('values', {})
    proc = None
    proc_or_appname = list(proc_or_appname)
    selector = None
    if not all([appname, values]):
        if not proc_or_appname:
            error(
                'appname and helm values are not loaded, this probably isn\'t a lain repo'
            )
            error(
                'lain logs --help to see how you can still use this command in a non-lain-repo',
                exit=True,
            )
        else:
            appname = ctx.obj['appname'] = proc_or_appname.pop(0)
            proc = proc_or_appname and proc_or_appname.pop(0)
            if proc_or_appname:
                error(
                    f'cannot interpret {proc_or_appname}, see lain logs --help',
                    exit=True,
                )

            if not proc:
                selector = f'app.kubernetes.io/name={appname}'
            else:
                selector = f'app.kubernetes.io/instance={appname}-{proc}'

    else:
        deploy_names = set(values.get('deployments') or [])
        cronjob_names = set(values.get('cronjobs') or [])
        job_names = set(values.get('jobs') or [])
        if proc_or_appname:
            proc = proc_or_appname.pop()
            appname = ctx.obj['appname'] = (
                proc_or_appname.pop() if proc_or_appname else appname
            )
            if proc_or_appname:
                error(
                    f'cannot interpret {proc_or_appname}, see lain logs --help',
                    exit=True,
                )

        if proc in deploy_names:
            selector = f'app.kubernetes.io/instance={appname}-{proc}'
        elif proc in cronjob_names | job_names:
            kibana_url = tell_kibana_url(proc)
            warn(
                f'kubernetes jobs are cleaned up fast, consider heading to kibana for complete logs:\n {kibana_url}'
            )
            selector = f'app.kubernetes.io/instance={appname}-{proc}'
        elif not proc:
            selector = f'app.kubernetes.io/name={appname}'
        else:
            proc_names = deploy_names | cronjob_names | job_names
            error(f'proc {proc} not found, choose from {proc_names}', exit=1)

    if use_kibana:
        return open_kibana_url(appname=appname, proc=proc)
    if not selector:
        error(
            'pod is already gone, you should try out kibana. use `lain status -s` to print kibana url'
        )
    else:
        if use_stern:
            stern(f'--selector={selector}', f'--tail={tail}', check=False)
        else:
            res = kubectl(
                'logs',
                '-f',
                f'--tail={tail}',
                '--max-log-requests=70',
                '-l',
                selector,
                timeout=None,
                check=False,
            )
            if rc(res):
                too_much_logs_headsup()


@lain.command()
@click.option(
    '--image-tag', help='specify image tag, default to currently deployed image'
)
@click.option(
    '--head',
    is_flag=True,
    help='use current git HEAD as imageTag, same as --imageTag=$(lain meta)',
)
@click.option('--wait', is_flag=True, help='wait until job exits')
@click.option(
    '--timeout',
    default=86400,
    callback=click_parse_timespan,
    help='timeout default to 1d',
)
@click.option(
    '--force',
    is_flag=True,
    help='if a job with a same command exists, delete it before proceed',
)
@click.option('--context', is_flag=True, help='copy all files under $CWD to container')
@click.argument('command', nargs=-1)
@click.pass_context
def job(ctx, image_tag, head, force, wait, timeout, context, command):
    """creates a Kubernetes Job to run desired command.

    \b
    examples:
    \b
        # start a container using the same environment
        lain job -- ./manage.py migrate

    \b
    if not in a lain4 app, will start using the lain docker image instead:
    \b
        # use -- to avoid click confusion on cli options
        lain job -- echo me so tired
        # omit command to run interactive shell, this implies --wait
        lain job
    """
    if image_tag and head:
        raise BadParameter('cannot use --image-tag with --head')

    template = template_env.get_template('job.yaml.j2')
    appname = ctx.obj.get('appname')
    if not appname:
        warn('not in a lain4 app repo, interpreting job name as "lain"')
        appname = ctx.obj['appname'] = 'lain'

    job_name = make_job_name(command)
    if force:
        try_to_cleanup_job(job_name)
    else:
        res = kubectl('get', 'job', job_name, check=False, capture_output=True)
        if not rc(res):
            error(f'{job_name} already exists, maybe someone else is using lain job:')
            error(f'    k logs -f -l job-name={job_name}', clean=False)
            error('if you\'d like to continue anyway, use --force', exit=1)

    ctx.obj['timeout'] = timeout
    ctx.obj['job_name'] = job_name
    if appname == 'lain':
        # 如果没有在任何 app 内运行 lain job, 则会用 lain 镜像启动一个容器
        ctx.obj['image'] = make_image_str(appname='lain', image_tag='latest')
        ctx.obj['volumeMounts'] = [{'name': 'jfs', 'mountPath': '/jfs'}]
        ctx.obj['volumes'] = [
            {
                'name': 'jfs',
                'hostPath': {
                    'path': '/jfs',
                    'type': 'Directory',
                },
            }
        ]
    else:
        # 如果发现是在 lain app 目录内运行 lain job, 就选取一个 deploy,
        # 拿出各种 spec 里的信息, 来渲染 job.yaml
        deploy = tell_best_deploy()
        res = kubectl(
            'get', 'deploy', f'{appname}-{deploy}', '-ojson', capture_output=True
        )
        deploy_spec = jalo(res.stdout)
        spec = deploy_spec['spec']['template']['spec']
        hostAliases = spec.get('hostAliases')
        if hostAliases:
            ctx.obj['hostAliases'] = hostAliases

        ctx.obj['volumes'] = spec['volumes']
        container = spec['containers'][0]
        ctx.obj['env'] = container['env']
        ctx.obj['volumeMounts'] = container['volumeMounts']
        current_image = container['image']
        if image_tag:
            parts = current_image.split(':')
            parts[-1] = image_tag
            ctx.obj['image'] = ':'.join(parts)
        elif head:
            image_tag = lain_meta()
            repo = current_image.split(':', 1)[0]
            ctx.obj['image'] = f'{repo}:{image_tag}'
        else:
            ctx.obj['image'] = current_image

    if not command:
        wait = True
        if appname == 'lain':
            cluster = ctx.obj['cluster']
            ctx.obj['command'] = ['bash', '-c', f'lain use {cluster} && sleep 3600']
        else:
            ctx.obj['command'] = ['sleep', '3600']
    else:
        ctx.obj['command'] = list(command)

    job_spec = template.render(**ctx.obj)
    kubectl_apply(job_spec, validate=False)
    goodjob('job has been created, here\'s some useful command:')
    echo(f' k logs -f -l job-name={job_name}', clean=False)
    echo(f' k delete job {job_name}', clean=False)
    if wait or sys.stdout.isatty():
        echo('waiting for job container up, you can leave if you like')
        pod_name = wait_for_pod_up(selector=f'job-name={job_name}')[0]
        if context:
            src = cwd()
            remote_dirname = basename(src)
            goodjob(
                f'copying {src} to remote container, you can access them under /tmp/{remote_dirname}'
            )
            kubectl('cp', src, f'{pod_name}:/tmp/{remote_dirname}', timeout=None)

        if command:
            kubectl('logs', '-f', '-l', f'job-name={job_name}', timeout=None)
            pod_rc = get_pod_rc(pod_name)
            ctx.exit(pod_rc)
        else:
            echo('created a container to sleep 1h, you must finish your work within')
            sh = 'zsh' if appname == 'lain' else 'sh'
            kubectl('exec', '-it', pod_name, sh, timeout=None)
            kubectl('delete', 'job', job_name)


@lain.command()
@click.argument('deploy_and_command', nargs=-1)
@click.pass_context
def x(ctx, deploy_and_command):
    """
    enter running container and run commands.

    \b
    examples:
    \b
        lain x
        lain x web
        lain x worker bash
        lain x web bash -c "ls | grep foo"
        lain x bash -c "ls | grep foo"
        # use -- to avoid click confusion on cli options
        lain x -- python3 manage.py foo --bar

    \b
    do not use when network isn't stable! instead, use lain behind mosh, for example:

    \b
        mosh relay
        tmux attach
        lain.sh
        lain use test
        export GITLAB_API_TOKEN=xxx  # your personal gitlab api token
        lain fetch-chart dev/avln-server
        cd dev/avln-server
        lain x  # this is a stable session due to mosh and tmux, you can safely work here
    """
    deploy_names = set(ctx.obj['values']['deployments'])
    if deploy_and_command:
        deploy, *cmd = deploy_and_command
        if deploy not in deploy_names:
            cmd = deploy_and_command
            warn(
                f'{deploy} is not a deploy name, thus interpreting the command as `{cmd}`'
            )
            deploy = tell_best_deploy()

        cmd = cmd or ['bash']
    else:
        deploy = tell_best_deploy()
        cmd = ['bash']

    podname = pick_pod(deploy_name=deploy)
    if not podname:
        # if user specified no arguments at all
        # we'll pick any pod to do this exec
        if not deploy_and_command:
            podname = pick_pod()
            appname = ctx.obj['appname']
            if not podname:
                error(f'no pod found for app {appname}', exit=1)
        else:
            error(f'no pod found for deploy {deploy}', exit=1)

    res = kubectl('exec', '-it', podname, '--', *cmd, check=False, timeout=None)
    ctx.exit(rc(res))


@lain.command()
@click.argument('cluster', nargs=-1, type=click.Choice(CLUSTERS))
@click.option('--turn', is_flag=True, help='if shut down, try to boot up this cluster')
@click.pass_context
def use(ctx, cluster, turn):
    """\b
    point to specified cluster.

    this command will link kubeconfig of specified CLUSTER to ~/.kube/config,
    so that you don\'t have to type --kubeconfig when using kubectl, or helm"""
    if not cluster:
        cluster = ctx.obj.get('cluster')
        if cluster:
            welcome_check()
            goodjob(f'currently on {cluster}', exit=True)
        else:
            error('you\'re nowhere, see lain use --help', exit=True)
    else:
        if len(cluster) != 1:
            error(f'provide one cluster only, got {cluster}', exit=True)
        else:
            cluster = cluster[0]

    kubeconfig_file = f'~/.kube/kubeconfig-{cluster}'
    src = expanduser(kubeconfig_file)
    if not isfile(src):
        error(
            f'{kubeconfig_file} not found, go fetch it from 1pw, under the "kubeconfig" item',
            exit=1,
        )

    dest = expanduser('~/.kube/config')
    ensure_absent(dest)
    os.symlink(src, dest)
    cluster_info = tell_cluster_info(cluster)
    ns = cluster_info.get('namespace', 'default')
    kubectl(
        'config', 'set-context', '--current', f'--namespace={ns}', capture_output=True
    )
    welcome_check(cluster)
    goodjob(
        f'You did good, next time you use lain / helm / kubectl, it\'ll point to cluster {cluster}'
    )
    if turn and cluster_info.get('instance_ids'):
        echo('wait for cluster up...')
        lain_('admin', 'turn', 'on', exit=True)


@lain.command()
@click.argument('procs_or_appname', nargs=-1)
@click.pass_context
def restart(ctx, procs_or_appname):
    """restart your app using kubectl delete po.

    \b
    examples:
    \b
        # delete all pods
        lain restart
        # delete pods for a single proc
        lain restart web
        # delete pods of some-other-app, note that name must not collide with proc names
        lain restart some-other-app
    """
    appname = ctx.obj.get('appname')
    procs = procs_or_appname
    if not appname:
        if len(procs_or_appname) == 1:
            appname = procs_or_appname[0]
            procs = []
        else:
            error('you should run this command in a lain app directory', exit=1)

    if procs:
        selectors = [f'app.kubernetes.io/instance={appname}-{proc}' for proc in procs]
    else:
        selectors = [f'app.kubernetes.io/name={appname}']

    for selector in selectors:
        kubectl('delete', 'po', '-l', selector, timeout=None)


@lain.command()
@click.argument('procs', nargs=-1)
@click.option(
    '--deduce',
    is_flag=True,
    help='use the most recent imageTag from registry rather than `lain meta`',
)
@click.pass_context
def update_image(ctx, procs, deduce):
    """update, and only update image for some proc"""
    values = ctx.obj['values']
    choices = set(values['procs'].keys())
    if not procs:
        error(f'specify at least one proc, choose from: {choices}', exit=1)

    procs = set(procs)
    if not procs.issubset(choices):
        wrong_procs = procs.difference(choices)
        error(f'unknown proc {wrong_procs}, choose from: {choices}', exit=1)

    registry = tell_registry_client()
    appname = ctx.obj['appname']
    if deduce:
        recent_tags = registry.list_tags(appname)
        if not recent_tags:
            error('wow, there\'s no pushed image at all', exit=1)

        image_tag = recent_tags[0]
    else:
        image_tag = tell_image_tag()

    image = registry.make_image(image_tag)
    for proc in procs:
        resource_type = 'deployment' if proc in values['deployments'] else 'cronjob'
        res = kubectl(
            'set',
            'image',
            f'{resource_type}/{appname}-{proc}',
            f'{proc}={image}',
            '--all',
        )
        if rc(res):
            error(
                'abort due to kubectl failure, if you don\'t understand the above error output, seek help from SA'
            )


@lain.command()
@click.argument('msg', nargs=1, type=str)
def send_msg(msg):
    """send webhook message, if applicable."""
    webhook = tell_webhook_client()
    webhook and webhook.send_msg(msg)


@lain.command()
@click.option(
    '--set',
    'pairs',
    multiple=True,
    type=KVPairType(),
    help='override values in values.yaml, same as helm template --set',
)
@click.option('--debug', is_flag=True)
@click.pass_context
def template(ctx, pairs, debug):
    """wrapper for helm template."""
    extra = ('--debug',) if debug else None
    options = tell_helm_options(pairs, deduce_image=False, extra=extra)
    appname = ctx.obj['appname']
    res = helm(
        'template', *options, appname, f'./{CHART_DIR_NAME}', capture_output=True
    )
    echo(res.stdout)


@lain.command()
@click.pass_context
def rollback(ctx):
    """rollback by exactly one revision, and one revision only."""
    appname = ctx.obj['appname']
    res = helm('history', appname, '-ojson', capture_output=True)
    history = jalo(res.stdout)
    current = history[-1]
    if 'rollback' in current['description'].lower():
        error(
            'already rolled back, use helm rollback manually if you want to go even further',
            exit=1,
        )

    try:
        revision = history[-2]['revision']
    except IndexError:
        error(f'cannot rollback, history too short: {history}', exit=1)

    res = helm('rollback', appname, str(revision), check=False)
    webhook = tell_webhook_client()
    tell_release_image(appname, revision)
    if code := rc(res):
        stderr = ensure_str(res.stderr)
        webhook and webhook.send_deploy_message(
            rollback_revision=revision, stderr=stderr
        )
        error(stderr)
        ctx.exit(code)

    webhook and webhook.send_deploy_message(rollback_revision=revision)
    isatty = sys.stdout.isatty()
    if isatty:
        lain_('status')


@admin.command()
@click.argument('resource')
@click.option(
    '--annotations',
    required=True,
    multiple=True,
    type=KVPairType(),
    help='query by annotations',
)
@click.pass_context
def get(ctx, resource, annotations):
    """Like kubectl get, but support filtering by annotations.

    \b
    examples:
    \b
        lain admin get pod --annotations prometheus.io/scrape=true
    """
    ctx.obj['silent'] = True
    items = kubectl(
        'get',
        '--all-namespaces',
        resource,
        '-o=custom-columns=NS:.metadata.namespace,NAME:.metadata.name,:.metadata.annotations',
        '--no-headers',
        check=True,
        capture_output=True,
    )

    def parse_annotations(s):
        bracketed = s.strip().removeprefix('map').strip('[]')
        if bracketed == '<none>':
            return
        pairs = bracketed.split()
        dic = dict([pair.split(':', 1) for pair in pairs])
        return dic

    for line in ensure_str(items.stdout).splitlines():
        try:
            _, _, annotations_part = line.split(None, 2)
        except ValueError:
            continue
        annotations_dic = parse_annotations(annotations_part)
        if not annotations_dic:
            continue
        for k, v in annotations:
            if annotations_dic.get(k) == v:
                echo(line)
            else:
                debug(line)


@lain.command()
@click.pass_context
def cherry(ctx):
    """git cherry between deployed version and HEAD."""
    release_name = ctx.obj['appname']
    deployed_image = tell_release_image(release_name)
    git_revision = ctx.obj.get('git_revision')
    if not git_revision:
        error(f'could not infer git revision from imageTag: {deployed_image}', exit=1)

    git('cherry', '-v', git_revision)


@lain.command()
@click.option(
    '--set',
    'pairs',
    multiple=True,
    type=KVPairType(),
    help='override values in values.yaml, same as helm upgrade --set',
)
@click.option(
    '--delete-after', type=str, help='same as the --after option in lain delete --help'
)
@click.option('--build', is_flag=True, help='run lain build if image does\'t exist')
@click.option('--canary', is_flag=True, help='deploy as canary version')
@click.pass_context
def deploy(ctx, pairs, delete_after, build, canary):
    """deploy this app.

    \b
    examples:
    \b
        lain use [CLUSTER]
        lain deploy

    \b
    canary workflow:
    \b
        lain deploy --canary
        lain set-canary-group xxx
        # to accept canary version
        lain set-canary-group --final
        # to rollback (delete) canary version
        lain set-canary-group --abort
    """
    if not ctx.obj['ignore_lint']:
        res = lain_('lint', check=False)
        if rc(res):
            error(res.stdout)
            echo(
                'fix above errors, if you insist, use lain --ignore-lint, or export LAIN_IGNORE_LINT=true',
                exit=1,
            )

    # no big deal, just using this line to initialized env first
    # otherwise this deploy may fail because envFrom is referencing a
    # non-existent secret
    tell_secret(ctx.obj['env_name'])
    ensure_resource_initiated(chart=True, secret=True)
    appname = release_name = ctx.obj['appname']
    canary_name = make_canary_name(appname)
    if canary:
        release_name = ctx.obj['release_name'] = canary_name
    elif get_app_status(canary_name):
        error('cannot proceed due to on-going canary deploy', exit=1)

    status_dic = get_app_status(appname)
    if status_dic:
        status = status_dic['info']['status']
        if status in HELM_WEIRD_STATE:
            warn(
                f'\n\nChart deployed but in a weird state: {status}\nif this problem persists, use lain delete'
            )

    elif canary:
        error(f'cannot initiate canary deploy when {appname} is not deployed', exit=1)

    try_to_cleanup_job()
    try_to_label_nodes()
    ctx.obj['build_jit'] = build
    options = tell_helm_options(pairs, extra='--install', canary=canary)
    headsup = '''
    While being deployed, you can check the status of you app:
        lain status
        lain logs
    '''
    echo(headsup, err=True)
    res = helm(
        'upgrade',
        *options,
        release_name,
        f'./{CHART_DIR_NAME}',
        capture_output=True,
        check=False,
    )
    webhook = tell_webhook_client()
    if code := rc(res):
        stderr = ensure_str(res.stderr)
        if 'job fail' in stderr:
            try_to_print_job_logs()
        else:
            webhook and webhook.send_deploy_message(stderr=stderr)
            error(stderr)

        ctx.exit(code)

    webhook and webhook.send_deploy_message()
    tests = ctx.obj['values'].get('tests')
    if tests:
        helm('test', '--logs', release_name, check=True)
    else:
        isatty = sys.stdout.isatty()
        if isatty and not delete_after and not canary:
            lain_('status')

    deploy_toast(canary=canary)
    try_to_print_job_logs()
    if delete_after:
        lain_('delete', f'--after={delete_after}')


@lain.command()
@click.argument('canary_group_name', nargs=-1)
@click.option(
    '--abort',
    is_flag=True,
    help='abort this canary deploy',
)
@click.option(
    '--final',
    is_flag=True,
    help='accept this canary deploy',
)
@click.pass_context
def set_canary_group(ctx, canary_group_name, abort, final):
    """modify ingress canary annotations to change canary state.

    to use canary features, you must define values.canaryGroups.

    \b
    examples:
    \b
        lain set-canary-group internal
        # to accept this canary version
        lain set-canary-group --final
        # to abort this canary version
        lain set-canary-group --abort
    """
    if (abort or final) and canary_group_name:
        error(
            f'must not specify canary_group_name when using --abort or --final, got {canary_group_name}',
            exit=1,
        )

    appname = ctx.obj['appname']
    canary_name = make_canary_name(appname)
    user_challenge(canary_name)
    if abort:
        helm('delete', canary_name, exit=True)

    if final:
        image_tag = tell_release_image(canary_name)
        pairs = [('imageTag', image_tag)]
        options = tell_helm_options(pairs, extra='--install')
        helm('upgrade', *options, appname, f'./{CHART_DIR_NAME}')
        selector = f'app.kubernetes.io/name={appname}'
        wait_for_pod_up(selector)
        helm('delete', canary_name)
        wait_res = wait_for_pod_up(selector)
        webhook = tell_webhook_client()
        webhook and webhook.send_deploy_message()
        ctx.exit(int(not bool(wait_res)))

    if canary_group_name and len(canary_group_name) == 1:
        canary_group_name = canary_group_name[0]
    else:
        error(f'must provide single canary_group_name, got {canary_group_name}', exit=1)

    canary_groups = ctx.obj['values']['canaryGroups']
    if not canary_groups:
        error('canaryGroups not defined in values', exit=1)

    if canary_group_name not in canary_groups:
        error(
            f'choose canary_group_name from {list(canary_groups)}, got {canary_group_name}',
            exit=1,
        )

    canary_dic = canary_groups[canary_group_name]
    ings_res = kubectl(
        'get',
        'ing',
        '-ojson',
        '-l',
        f'helm.sh/chart={canary_name}',
        capture_output=True,
    )
    ings = jalo(ings_res.stdout)
    for ing in ings['items']:
        annotations = ing['metadata']['annotations']
        clean_canary_ingress_annotations(annotations)
        annotations.update(canary_dic)

    kubectl_apply(ings, validate=False)


@lain.command()
@click.argument('appname', nargs=-1)
@click.option(
    '--tries',
    default=40,
    help='tries before giving up (will sleep 3s after each try).',
)
@click.pass_context
def wait(ctx, appname, tries):
    """wait until pods are up and running.

    this command is designed to run in helm tests, if used inside a pod, it will wait for services as well."""
    if appname:
        if len(appname) > 1:
            error(f'specify only one appname at a time, got {appname}', exit=1)

        ctx.obj['appname'] = appname = appname[0]
    else:
        appname = ctx.obj['appname']

    selector = f'app.kubernetes.io/name={appname}'
    wait_for_pod_up(selector, tries=tries)
    if is_inside_cluster():
        up = wait_for_svc_up(tries=tries)
        if not up:
            error('svc not up, check `lain logs` or `lain status` for clues', exit=1)


@lain.command()
@click.pass_context
def redeploy(ctx):
    """redeploy your app using current helm chart.

    if you modified anything under ./chart, use this command to take immediate effect
    """
    release_name = ctx.obj['appname']
    image_tag = tell_release_image(release_name)
    if image_tag:
        args = ('--set', f'imageTag={image_tag}')
    else:
        args = ()

    lain_('deploy', *args)


@lain.command()
@click.option(
    '--purge',
    is_flag=True,
    help='also deletes env and secrets, everything will be gone, please don\'t use this',
)
@click.option(
    '--after',
    callback=click_parse_timespan,
    help='start a countdown job that delete this app at exit, can be an int or string (humanfriendly timespan, like 5h, 1d)',
)
@click.argument('appname', nargs=-1)
@click.pass_context
def delete(ctx, purge, after, appname):
    """delete this app."""
    if appname:
        if len(appname) > 1:
            error(f'do not provide more than one appname, got {appname}', exit=1)
        else:
            appname = ctx.obj['appname'] = appname[0]
    else:
        appname = ctx.obj['appname']

    job_name = f'{appname}-delete-job'
    try_to_cleanup_job(job_name)
    canary_name = make_canary_name(appname)
    if not after:
        if purge:
            kubectl(
                'delete', 'secret', f'{appname}-env', f'{appname}-secret', check=False
            )

        helm_delete(appname, canary_name, exit=True)

    if purge:
        error('cannot use --purge with --after', exit=1)

    template = template_env.get_template('job.yaml.j2')
    ctx.obj['job_name'] = job_name
    cluster = ctx.obj['cluster']
    sh = f'lain use {cluster} && sleep {after} && lain delete {appname}'
    ctx.obj['image'] = make_image_str(appname='lain', image_tag='latest')
    ctx.obj['command'] = ['bash', '-c', sh]
    job_spec = template.render(**ctx.obj)
    kubectl_apply(job_spec, validate=False)
    goodjob('job has been created, here\'s some useful command:')
    echo(f' k logs -f -l job-name={job_name}', clean=False)


@lain.command()
@click.option('--skip-push', is_flag=True, help='skip docker push')
@click.option(
    '--keep-dockerfile',
    is_flag=True,
    help='preserve automatically generated dockerfile',
)
@click.pass_context
def prepare(ctx, skip_push, keep_dockerfile):
    """\b
    build prepare image and push current registry.
    to use lain prepare, you must define values.build.prepare"""
    stage = 'prepare'
    prepare_image = lain_build(stage=stage, push=False, keep_dockerfile=keep_dockerfile)
    if skip_push:
        return
    banyun(prepare_image)


@lain.command()
@click.option('--push', is_flag=True, help='push immediately')
@click.option('--deploy', is_flag=True, help='push and deploy immediately')
@click.option(
    '--publish',
    is_flag=True,
    help='if provided, lain will push to all possible registries defined in CLUSTERS',
)
@click.option(
    '--keep-dockerfile',
    is_flag=True,
    help='preserve automatically generated dockerfile',
)
@click.pass_context
def build(ctx, push, deploy, publish, keep_dockerfile):
    """\b
    build docker image for your app.
    to use lain build, you must define values.build."""
    try_lain_prepare(keep_dockerfile=keep_dockerfile)
    values = ctx.obj['values']
    stage = 'release' if 'release' in values else 'build'
    lain_build(stage=stage, push=False, keep_dockerfile=keep_dockerfile)
    if push or deploy:
        opts = ['--publish'] if publish else []
        lain_('push', *opts)

    if deploy:
        lain_('deploy', exit=True)


@lain.command()
@click.option(
    '--force',
    '-f',
    is_flag=True,
    help=f'overwrite {DOCKER_COMPOSE_FILE_PATH} if exists',
)
@click.pass_context
def compose(ctx, force):
    template = template_env.get_template('docker-compose.yaml.j2')
    if not force and isfile(DOCKER_COMPOSE_FILE_PATH):
        error(
            f'{DOCKER_COMPOSE_FILE_PATH} already exists, delete before proceed', exit=1
        )

    with open(DOCKER_COMPOSE_FILE_PATH, 'w') as f:
        f.write(template.render(**ctx.obj))

    goodjob(f'{DOCKER_COMPOSE_FILE_PATH} generated, review and edit before use')


@lain.command()
@click.option(
    '--proc',
    '-c',
    'proc_name',
    required=False,
    callback=validate_proc_name,
    help='specify proc name, if you use different image for each proc',
)
@click.option(
    '--prepare',
    is_flag=True,
    help='use :prepare image instead',
)
@click.argument('command', nargs=-1)
@click.pass_context
def run(ctx, proc_name, prepare, command):
    """docker run the image for this app.

    \b
    examples:
    \b
        # start a docker container using [APPNAME]:[LAIN_META]
        lain run
        # specify proc name if you use different image for each proc
        lain run web
    """
    if proc_name and prepare:
        raise BadParameter('cannot use --proc with --prepare')
    if proc_name:
        procs = ctx.obj['values']['procs']
        proc = procs[proc_name]
        try:
            image = proc['image']
        except KeyError:
            image_tag = proc['imageTag']
            image = make_image_str(image_tag=image_tag)
    if prepare:
        image = make_image_str(image_tag='prepare')
    else:
        meta = lain_meta()
        image = make_image_str(image_tag=meta)

    command = command or ['bash']
    docker('run', '-it', image, *command)


@lain.command()
@click.argument('images', nargs=-1)
@click.option('--pull', is_flag=True, help='pull image before save')
@click.option(
    '--dir',
    'output_dir',
    default=cwd(),
    help='directory name in which image will be saved to',
)
@click.pass_context
def save(ctx, images, pull, output_dir):
    """save docker images to [image-tag].tar.gz.

    \b
    examples:
    \b
        lain save --dir /jfs/backup
        lain save alpine:latest --dir /jfs/backup
    """
    save = partial(docker_save, output_dir=output_dir, pull=pull)

    if images:
        for image in images:
            save(image)

        ctx.exit(0)

    appname = ctx.obj['appname']
    meta = lain_meta()
    for image_info in docker_images():
        if image_info['appname'] == appname and image_info['tag'] == meta:
            save(image_info['image'])
            ctx.exit(0)

    error(f'image not found for {appname}', exit=True)


@lain.command()
@click.argument('images', nargs=-1)
@click.option('--pull', is_flag=True, help='pull image before retag and push')
@click.option(
    '--publish',
    is_flag=True,
    help='if provided, lain will push to all possible registries',
)
@click.option(
    '--overwrite-latest',
    is_flag=True,
    help='if provided, lain will also retag this image into :latest. (default to True when called with no arguments)',
)
@click.pass_context
def push(ctx, images, pull, publish, overwrite_latest):
    """
    push app image to current registry.

    \b
    examples:
    \b
        lain use [CLUSTER]
        lain push
        # push to all registries (controlled using values.publish_to)
        lain push --publish

    \b
    also, you can use this command to retag and transfer an image to another registry:

    \b
        lain use foo
        lain push dummy:***
        # docker push registry.foo/namespace/dummy:***
        lain push dummy:*** --overwrite-latest
        # docker push registry.foo/namespace/dummy:***
        # docker push registry.foo/namespace/dummy:latest
    """
    if publish:
        if tell_cluster_info().get('offline'):
            registries = None
        else:
            registries = ctx.obj['values']['publish_to_registries']
    else:
        cluster = ctx.obj['cluster']
        registries = CLUSTERS[cluster]['registry']

    if images:
        for image in images:
            banyun(
                image,
                pull=pull,
                registry=registries,
                overwrite_latest_tag=overwrite_latest,
            )

        ctx.exit(0)

    appname = ctx.obj.get('appname')
    if not appname:
        return
    image = tell_image()
    if not image:
        error(f'image not found for {appname}', exit=True)

    theirs = banyun(image, pull=pull, registry=registries, overwrite_latest_tag=True)
    echo(theirs)
    ctx.exit(0)


@lain.group()
@click.pass_context
def env(ctx):
    """env management. after edit, you must redeploy to take effect.

    \b
    lain restart  # delete all pods, k8s will re-create them
    lain redeploy  # safer way, k8s will perform a rolling update
    """


@env.command()
@click.argument('f', type=click.File('r'), nargs=1)
@click.option(
    '--overwrite', is_flag=True, help='overwrite env data using file provided'
)
@click.pass_context
def addfile(ctx, f, overwrite):
    """add environment variable using a flat json or yaml file.

    \b
    examples:
    \b
        lain secret add foo.json
        lain secret add bar.yml
    """
    ext = f.name.rsplit('.', 1)[-1]
    if ext not in {'yml', 'yaml', 'json'}:
        error('not a json / yaml file, abort', exit=1)

    content = f.read()
    env_dic = tell_secret(ctx.obj['env_name'], init='env')
    old = env_dic.copy()
    if overwrite:
        env_dic['data'] = {}

    if ext == 'json':
        data = jalo(content)
    else:
        data = yalo(content)

    for k, v in data.items():
        s = str(v)  # Kubernetes env values must be string
        env_dic['data'][k] = s
        if s != v:
            warn(f'type cast happened for {k}')

    new = deepcopy(env_dic)
    kubectl_apply(env_dic)
    webhook = tell_webhook_client()
    webhook and webhook.diff_k8s_secret(old, new)


@env.command()
@click.argument('pairs', type=KVPairType(), nargs=-1)
@click.pass_context
def add(ctx, pairs):
    """add environment variable.

    \b
    examples:
    \b
        lain secret add FOO=BAR EGG=SPAM
    """
    if not pairs:
        goodjob(
            'You just added nothing, what a great way to use this command', exit=True
        )

    env_dic = tell_secret(ctx.obj['env_name'], init='env')
    old = env_dic.copy()
    for k, v in pairs:
        env_dic['data'][k] = v

    new = deepcopy(env_dic)
    kubectl_apply(env_dic, capture_output=True)
    webhook = tell_webhook_client()
    webhook and webhook.diff_k8s_secret(old, new)


@env.command()
@click.pass_context
def show(ctx):
    """print environment variables"""
    secret_dic = tell_secret(ctx.obj['env_name'], init='env')
    echo(yadu(secret_dic))


@env.command()
@click.pass_context
def edit(ctx):
    """edit environment variables using $EDITOR"""
    f = dump_secret(ctx.obj['env_name'], init='env')
    kubectl_edit(f, notify_diff=True)


@lain.group()
def secret():
    """secret file management.

    on lain4 clusters, secrets are managed by Kubernetes Secret, this set of
    commands help you with kubectl edit secret.

    after edit, you must redeploy to take effect.

    \b
    lain restart  # delete all pods, k8s will re-create them
    lain redeploy  # safer way, k8s will perform a rolling update
    """


@secret.command()
@click.argument('files', nargs=-1)
@click.pass_context
def add(ctx, files):
    """upload secret file to Kubernetes.

    \b
    examples:
    \b
        lain secret add [FILE]

    after upload, make sure volumeMounts is properly defined in values.yaml
    """
    if not files:
        goodjob(
            'You just added nothing, what a great way to use this command', exit=True
        )

    secret_name = ctx.obj['secret_name']
    secret_dic = tell_secret(secret_name, init='secret')
    old = secret_dic.copy()
    for f in files:
        fname = basename(f)
        secret_dic['data'][fname] = open(f).read()

    new = deepcopy(secret_dic)
    kubectl_apply(secret_dic)
    webhook = tell_webhook_client()
    webhook and webhook.diff_k8s_secret(old, new)


@secret.command()
@click.option(
    '--filename',
    '-f',
    'secretfile',
    type=click.File('r'),
    help='Kubernetes Secret yaml file (but data is plain text, not b64 encoded)',
)
@click.pass_context
def apply(ctx, secretfile):
    """upload the whole Kubernetes Secret yaml file.

    \b
    examples:
    \b
        lain secret show > secret.yml
        lain secret apply -f ./secret.yml
    """
    if not secretfile:
        error('must specify filename using -f')

    secret_dic = yalo(secretfile)
    kubectl_apply(secret_dic)


@secret.command()
@click.argument('name', nargs=-1)
@click.pass_context
def show(ctx, name):
    """show lain secret.

    \b
    examples:
    \b
        lain secret show
        lain secret show some-other-kubernetes-secret
    """
    if not name:
        name = ctx.obj['secret_name']
    else:
        if len(name) == 1:
            name = name[0]
        else:
            error('one secret at a time')

    secret_dic = tell_secret(name, init='secret')
    echo(yadu(secret_dic))


@secret.command()
@click.argument('name', nargs=-1)
@click.pass_context
def edit(ctx, name):
    """edit lain secret.

    \b
    examples:
    \b
        lain secret edit
        lain secret edit some-other-kubernetes-secret
    """
    if not name:
        name = ctx.obj['secret_name']
    else:
        if len(name) == 1:
            name = name[0]
        else:
            error('one secret at a time')

    f = dump_secret(name, init='secret')
    kubectl_edit(f, notify_diff=True)


@lain.command()
@click.option(
    '--images-count',
    '-n',
    type=int,
    default=RECENT_TAGS_COUNT,
    help='how many recent images to print, default to 10',
)
@click.pass_context
def version(ctx, images_count):
    """print version for lain and current lain app."""
    echo(f'lain: {__version__}')
    appname = ctx.obj.get('appname')
    if appname:
        kubectl(
            'get',
            'deploy',
            '-l',
            f'app.kubernetes.io/name={appname}',
            '-o=custom-columns=NAME:.metadata.name,IMAGE:..image',
            check=False,
        )
        kubectl(
            'get',
            'cronjob',
            '-l',
            f'app.kubernetes.io/name={appname}',
            '-o=custom-columns=NAME:.metadata.name,IMAGE:..image',
            check=False,
        )
        registry = tell_registry_client()
        if registry:
            tags_list = registry.list_tags(appname, timeout=2) or []
            click.echo(click.style('recent image tags', fg='bright_yellow'), err=True)
            for tag in tags_list[:images_count]:
                # 多打印一个空格, 这样复制粘贴的命令不会进入 bash history
                echo(f' lain deploy --set imageTag={tag}', clean=False)


@lain.command()
@click.pass_context
def meta(ctx):
    echo(lain_meta())


def main():
    lain(obj={})


if __name__ == '__main__':
    main()
