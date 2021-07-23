应用管理 / 应用工作流
=====================

应用的生命周期里要做的事情, 在这里按照时间顺序进行详述.

.. _helm-values:

撰写 Helm Values
----------------

每一个 lain 应用都是一个合法的 Helm App, 因此应用上平台的第一件事就是使用 :code:`lain init` 初始化出一份默认的 Helm Chart. 作为开发者, 你需要关心的只有 :code:`chart/values.yaml`, 请仔细阅读注释, 然后参考示范来为你的应用撰写配置:

.. literalinclude:: ../lain_cli/chart_template/values.yaml.j2
   :language: yaml

.. _values-cluster:

多集群配置
----------

团队面对的肯定不止一个集群, 应用在不同集群需要书写不同的配置, 这也是天经地义. 幸好 helm 早就帮我们考虑好了这类需求, 可以书写多份 :code:`values.yaml`, 在其中对需要的地方进行(递归地)超载. 用一个过度简化的例子来说明:

.. code-block:: yaml

    # values.yaml
    deployments:
      web:
        command: ["/lain/app/run.py"]

    # values-test.yaml
    deployments:
      web:
        command: ["/lain/app/run.py", "--debug"]

上方示范的写法, 作用就是: 在 test 集群里修改 web 容器的启动命令, 加上 :code:`--debug` 参数. 用类似这样的递归超载写法, 我们便可以随心所欲地为不同的集群定制任何配置, 比如:

* 希望 a 进程仅在 test 集群运行: 把 :code:`deployments.a` 的配置块整个挪到 :code:`values-test.yaml`
* 希望在不同集群用不同的构建命令: 没问题, 在 :code:`values-[CLUSTER].yaml` 里书写定制的 :code:`build.script`, 这样一来, 当你把 lain 指向该集群, 然后运行 :code:`lain build` 的时候, 便会以递归覆盖后的 :code:`build` 来渲染 Dockerfile, 这样一来就实现了不同集群的定制化构建.

用 docker-compose 进行本地调试
------------------------------

写好了 values 之后, 可以用 :code:`lain compose` 生成一份 :code:`docker-compose.yaml` 样例, 方便你使用 docker-compose 来进行本地开发调试:

.. code-block:: yaml

    version: '3'
    services:

      web:
        # lain push will overwrite the latest tag every time
        image: ccr.ccs.tencentyun.com/yashi/dummy:latest
        command:
          - /lain/app/run.py
        volumes:
          - .:/lain/app
        environment:
          FOO: BAR
        working_dir: lain/app
        # depends_on:
        #   - redis
        #   - mysql

      # redis:
      #   image: "redis:3.2.7"
      #   command: --databases 64

      # mysql:
      #   image: "mysql:8"
      #   command: --character-set-server=utf8mb4 --collation-server=utf8mb4_general_ci
      #   environment:
      #     MYSQL_ROOT_PASSWORD: root
      #     MYSQL_DATABASE: dummy

配置文件虽然渲染出来了, 但由于 lain 并不清楚你本地的调试过程, 比如基础设施 (mysql, redis 等), 或者调试用的环境变量, 因此这些都需要你对 :code:`docker-compose.yaml` 进行仔细 review 修改. 完成这步以后, 自行使用 docker-compose 进行本地调试即可. 在本地开发环境的事情上, lain 目前只能帮你到这一步了, 抱歉.

.. _lain-build:

构建镜像
--------

:code:`lain build` 无非就是用 :code:`values.build` 的内容渲染出 Dockerfile, 然后直接为你执行相应的 :code:`docker build` 命令. 以上边的 values 示范, 生成的 Dockerfile 如下:

.. code-block:: dockerfile

    FROM ccr.ccs.tencentyun.com/yashi/dummy:prepare AS build
    WORKDIR /lain/app
    ENV LAIN_META=1619925143-97f3d5f810a61823de72ea0a6f3fdd06f9f3cce9
    ADD --chown=1001:1001 . /lain/app
    RUN (pip3 install -r requirements.txt)
    USER 1001

懂 Dockerfile 的人肯定一看就能明白这里做了些什么, 仅仅是把代码仓库拷贝到镜像里, 然后安装好 Python 依赖, 便是一个可以运行的镜像了. 不过这里的 :code:`FROM dummy:prepare` 镜像是个啥? 是怎么来的呢? 那么再来介绍下 :code:`lain prepare`:

应用的生命周期里要不停地修改代码, 重新构建镜像. 为了节约资源, 可以先把不常变动的部分做成一个 "prepare 镜像", 再以该镜像为 base, 构建最终用于上线的镜像. 我们仍以上边的 dummy  values 为例, prepare 镜像对应的 Dockerfile 如下:

.. code-block:: dockerfile

    FROM ccr.ccs.tencentyun.com/yashi/ubuntu-python:latest AS prepare
    WORKDIR /lain/app
    ADD --chown=1001:1001 . /lain/app
    RUN (pip3 install -r requirements.txt) && (echo "treasure" > treasure.txt)
    RUN rm -rf /tmp/* && mv treasure.txt /tmp/ &&  ls -A1 | xargs rm -rf && mv /tmp/treasure.txt .

构建完成以后, 如果不放心, 还可以用 :code:`lain run` 来启动一个调试容器, 看看构建的结果是否正确:

.. code-block:: bash

    $ lain run -- cat treasure.txt
    docker run -it ccr.ccs.tencentyun.com/yashi/dummy:xxx cat treasure.txt
    qxWGsNOpT

特别地, 由于 lain 支持集群定制配置, 因此应用当然也可以在不同集群使用不同的构建策略, 举个例子, 一个 python3.8 的应用想要在 test 集群使用 python3.9 镜像, 其他集群保持不变, 则可以这样超载:

.. code-block:: yaml

    # values-test.yaml
    build:
      # 这样一来, 在 test 集群做 lain build, 用的就是 python:3.9
      base: python:3.9
      # 特意将 prepare 覆写为空, 否则在 values.yaml 里的 prepare 镜像仍会生效, 让最终构建得到的仍是 python3.8
      prepare:
      script:
        - apt-get update
        - pip3 install -r requirements.txt

.. warning::

   如果你修改了 base, 请务必记得重新 :code:`lain prepare`, 否则缓存一直不更新, 你的新 base 也不会生效. 当然, 如果你没有用 :code:`build.prepare`, 则可绕过此提示.

.. _lain-env:

ENV (环境变量) 管理
-------------------

按照心智负担顺序, 最简单办法便是把环境变量写到 :code:`values.yaml`:

.. code-block:: yaml

    # global level
    env:
      FOO: "bar"

在 global level 定义的 env, 将会应用于该应用的所有容器, 如果有定制需求, 那么也可以在 proc 级别定义 env:

.. code-block:: yaml

    # global level
    env:
      FOO: "bar"

    deployments:
      web:
        env:
          SPAM: egg

继续往下想, 一个 lain app 往往要部署在若干个不同的集群上, 如果不同的集群希望使用不同的配置, 可以考虑创建出 :code:`values-[CLUSTER].yaml`, 把需要超载的配置(递归地)写进去. 这点在 :ref:`values-cluster` 有更详细的介绍. 比方说在 prod 集群超载 :code:`deployments.web` 的环境变量, 可以这样写:

.. code-block:: yaml

    # chart/values-prod.yaml
    deployments:
      web:
        env:
          SPAM: EGG

方便是很方便没错, 但密码类的配置可不能这么随便写在 :code:`values.yaml` 里, 毕竟代码仓库不应该包含敏感信息, 这时候就需要借助于 :code:`lain env` 了:

.. code-block:: bash

    $ lain env show
    apiVersion: v1
    data:
      MYSQL_PASSWORD: ***
    kind: Secret
    metadata:
      name: dummy-env
      namespace: default
    type: Opaque
    $ lain env edit  # 打开编辑器, 修改 data 下的内容, 就能编辑环境变量

:code:`lain env` 中面对的 yaml, 是解密过的 Kubernetes Secret, 因此有很多开发者并不关心的字段, 如果你不熟悉 Kubernetes, 那么只修改 data 下边的内容即可, 不要乱动别的内容.

.. _lain-secret:

配置文件管理
------------

与 :code:`lain env` 相仿, 我们也有配套的功能来管理配置文件, 也就是 :code:`lain secret`, 使用方法非常相似:

.. code-block:: bash

    $ lain secret show
    apiVersion: v1
    data:
      topsecret.txt: |-
        I
        AM
        BATMAN
    kind: Secret
    metadata:
      name: dummy-secret
      namespace: default
    type: Opaque
    $ lain secret edit  # 编辑流程与 lain env 一样, 但要注意 yaml 语法, 别忘了冒号后边的竖线 |

在你初次使用 :code:`lain secret` 的时候, 他会贴心地帮你生成一份默认的配置文件 :code:`topsecret.txt`, 主要是为了防呆, 同时向大家示范 secret file 的书写语法.

总之, 现在配置文件已经写入集群, 那么如何挂载到容器内啊? 请看 :code:`values.yaml` 示范:

.. code-block:: yaml

    # global level
    volumeMounts:
      - mountPath: /lain/app/deploy/topsecret.txt
        subPath: topsecret.txt

    deployments:
      web:
        # 如果你真的需要, 当然也可以在 proc 级别定义 volumeMounts, 但一般而言为了方便管理, 请尽量都放在 global level
        volumeMounts:
          - mountPath: /lain/app/deploy/topsecret.txt
            subPath: topsecret.txt

上边示范的写法, 就是在把 :code:`topsecret.txt` 挂载到 :code:`/lain/app/deploy/topsecret.txt` 这个目录. 至于 volumeMounts, subPath 等名词, 都是 `底层的 Kubernetes 配置块 <https://kubernetes.io/docs/concepts/storage/volumes/>`_, 如果你感到费解, 其实也不必深究, 按照这个示范格式来书写就不会有问题.

.. warning::
   修改 env / secret 以后, 容器内的配置并不会立刻生效! 你需要重建 pod (:code:`lain restart`) 或者重新部署 (:code:`lain redeploy`) 才能生效.

   详见 https://kubernetes.io/docs/concepts/configuration/secret/#mounted-secrets-are-updated-automatically

部署上线, 以及生产化梳理
------------------------

如果之前的步骤都没做错, 那么 :code:`lain deploy` 就能把你的应用部署到 Kubernetes 集群了, 以下是一些对新手的建议:

* 第一次上线时, 建议以单实例部署(:code:`replicaCount: 1`), 否则万一出了啥问题, 实例太多了怕不好排查.
* 出问题的时候, :code:`lain status` 会是你最好的朋友, 他会在命令行里打开一个综合的信息面板, 呈现出容器状态, 异常容器日志, 以及 ingress endpoint 的 HTTP 可访问性.
* :code:`lain status` 里也有显示日志的板块, 但很可能因为面板大小显示不全, 这时候就要用 :code:`lain logs` 来阅读完整日志.
* 同样为了方便排查, 可以考虑先删去 :code:`livenessProbe` 配置, 否则应用不健康的时候, Kubernetes 会无限重启你的应用, 不太方便用 :code:`lain x` 钻进容器排查.
* 上线成功以后, 最好安排给应用做"生产化梳理", 根据线上情况调整应用资源需求, 或者增加实例数.

  但毕竟你才刚上线, 没法立刻弄清 workload, 因此建议刚开始的时候, 把 memory limits 做宽松一些, 并且在稳定前, 时常运行 :code:`lain lint`, 从监控系统获取数据, 让 lain 来帮你书写 resources. :code:`lain lint` 会努力做到贴心智能, 让你不需要拍脑袋, 或者手动查询监控, 才能写好应用资源声明. 具体可以阅读 :ref:`lain-resource-design`.

日志和监控
----------

:code:`lain logs` 会调用 kubectl (或者更易用的 stern) 来为你实时地打印日志, 但若你想看历史日志, 那就需要你的集群搭建日志收集系统了. 在 :code:`lain_cli/clusters.py::CLUSTERS` 下配置好 kibana, lain 就会在合适的时候, 提示用户使用 kibana 来看日志了.

* :code:`lain status -s` 会附上看日志的 kibana url (注意! 一定要完整复制 url, 不要漏了最后的括号, kibana 的链接很奇怪)
* 对于 job 或者 cronjob 进程, :code:`lain logs` 可能没那么好用了, job 容器转瞬即逝, 而 :code:`lain logs` 是实时日志, 运行的时候, 很可能容器早就回收了. 这种情况你只好去看 kibana (或者你们自己的日志收集系统) 了.
* 在容器启动失败的情况下, stern 未必能获取到容器日志, 因为此时容器 stdout 还没来得及 attach 吧, 这种情况必须用 :code:`kubectl logs` 才能顺利获取日志了. 这也是为什么 :code:`lain logs` 默认调用的是 kubectl.

至于监控, lain 本身并不是监控系统, 能做的事情都是调用已有的监控功能. 比如 Prometheus 相关, 就需要你在 :code:`lain_cli/clusters.py::CLUSTERS` 下配置好对应的 API url. 总而言之, 在监控方面, lain 提供如下功能:

* :code:`lain status` 里调用了 :code:`kubectl top pod`, 打印出容器的资源占用.
* :code:`lain lint` 会帮你查询 Prometheus, 用实际资源占用, 对比你在 :code:`chart/values.yaml` 里写的资源声明, 给出合适的修改建议. 详见 :ref:`lain-resource-design`.
* 如果你想让 Prometheus 来抓取你的应用自己的 metrics, 可以在 :ref:`podAnnotations <helm-values>`, 里做相应的配置声明. 当然啦, 这需要集群里已经部署好 Prometheus, 并且启用 `Service Discovery <https://prometheus.io/docs/prometheus/latest/configuration/configuration/#kubernetes_sd_config>`_).
* 如果你是管理员, lain 和监控系统的集成能让你完成许多集群维护管理工作, 比如 :code:`lain admin list-waste` 能查出哪些应用在浪费集群资源, 详见 :ref:`lain-admin-list-waste`.

回滚
----

回滚有很多种姿势, 每一种的适用场景略微有所不同:

* 如果没什么特别需要, 只是线上应用版本错了, 需要回滚镜像, 那其实推荐直接再做一次上线 (:code:`lain deploy --set imageTag=xxx`), 如果你不清楚 imageTag 是多少, 可以用 :code:`lain version` 查看.
* :code:`lain rollback` 会直接调用 :code:`helm rollback`, 所有 helm 管理的资源都会回滚.

  什么是"helm 管理的资源"? 简单来说就是除了 lain secret / env 以外, 你的应用的所有配置. 因此要注意, 如果你修改了 lain secret / env, 则应该先操作这部分配置回滚, 再重新上线, 才能让配置生效.

  可是这样做又和 :code:`lain deploy` 有何不同呢? 区别如下:

  * :code:`helm rollback` 会整体打包回滚 (lain secret / env 除外), 也就是说, 你对 :code:`chart` 下所做的修改也会一并回滚, 比如 :code:`values.yaml`.
  * :code:`lain deploy --set imageTag=xxx` 会使用当前代码仓库下的 helm chart, 仅仅把 imageTag 用参数进行超载, 你在 :code:`values.yaml` 里书写的配置, 都会生效, 不会被重置成旧版的状态.
* 要知道, :code:`lain rollback` 为了防呆和易用, 设计上只允许回滚一个版本. 如果你要回滚到多个版本, 可以这样做:

  * 先 :code:`git checkout` 到代码仓库中希望上线的版本.
  * 正常用 :code:`lain deploy` 命令进行上线.

  你也可以直接用 :code:`helm rollback`, 但请务必提前熟悉 helm 的使用.

运行时的应用管理
----------------

runtime 期间也有许许多多的事情需要开发者处理, 比如:

* :code:`lain update-image` 用来部署单个进程, 比如说你的应用有 web, worker 两个进程, 但只希望更新 worker 容器, 便可以用该命令实现.
* :code:`lain restart` 重启所有容器. 虽说是重启, 但其实是调用 :code:`kubectl delete pod` 来删除容器, 然后 Kubernetes 会进行重建.
* :code:`lain x` 进入容器内执行命令, 该功能仅用于调试, 原则上不鼓励用于进行生产环境操作, 因为运行资源难以保证.
* :code:`lain job` 会启动一个 Kubernetes Job 容器, 来执行你给定的命令. 如果未给定命令, 则进入容器内, 打开 shell 进行交互操作.

以上也仅仅是对 lain 比较常用的功能做简单介绍. 需求千奇百怪, 在文档里也很难覆盖全, 建议你时不时阅读 :code:`lain --help`, 来探索还有什么别的好用的功能.

与 CI 协同工作
--------------

lain 的命令行属性使其天然适合于在 CI 里使用. 由于我们团队目前使用 GitLab CI, 下方的例子也都基于 GitLab CI.

构建 lain 镜像, 作为 GitLab CI Image
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

下方就是用于构建 lain 镜像的 Dockerfile, 如你所见, 除了 :code:`lain` 本人外, 还有各种乱七八糟的好东西, 比如 git, docker, mysql-client. 这个镜像是一个"瑞士军刀", 尽可能去覆盖各种常用的 CI 需求, 这一切都是为了业务大哥们使用 CI 更方便.

.. literalinclude:: ../Dockerfile
   :language: dockerfile

lain 镜像构建好了, 接下来需要在 GitLab CI Runner 配置里执行成为默认 image, 你不这么做也没事, 但那就要每一个 CI Job 都单独声明 Image 了.

.. code-block:: toml

    concurrent = 4
    check_interval = 0

    [session_server]
      session_timeout = 1800

    [[runners]]
      name = "ci-1"
      output_limit = 99999
      url = "https://gitlab.example.com"
      token = "xxx"
      executor = "docker"
      environment = ["DOCKER_AUTH_CONFIG={}"]
      # 这是为了在镜像里能顺利 docker push
      pre_build_script = "  mkdir -p $HOME/.docker\n  echo $DOCKER_AUTH_CONFIG > $HOME/.docker/config.json\n  "
      [runners.custom_build_dir]
      [runners.cache]
        Type = "s3"
        Path = "gitlab-runner"
        [runners.cache.s3]
          ServerAddress = "xxx"
          AccessKey = "xxx"
          SecretKey = "xxx"
          BucketName = "gitlab"
          BucketLocation = "xxx"
        [runners.cache.gcs]
        [runners.cache.azure]
      [runners.docker]
        tls_verify = false
        image = "lain:latest"
        privileged = true
        disable_entrypoint_overwrite = false
        oom_kill_disable = false
        disable_cache = false
        volumes = ["/var/run/docker.sock:/var/run/docker.sock", "/jfs:/jfs:rw", "/cache"]
        shm_size = 0
        helper_image = "gitlab-runner-helper:x86_64-bleeding"

在 GitLab CI 里使用 lain
^^^^^^^^^^^^^^^^^^^^^^^^

当你按照类似上边的步骤配置好 Runner 以后, 所有的 Job 都默认在用 lain image 来执行了. 镜像里已经安装了如此多工具, 因此书写 :code:`.gitlab-ci.yml` 的时候, 内容相当简洁:

.. code-block:: yaml

    stages:
      - prepare
      - test
      - deploy

    prepare_job:
      stage: prepare
      only:
        changes:
          - requirements*
      script:
        - lain use test
        - lain prepare

    test_job:
      image: [APPNAME]:prepare
      stage: test
      script:
        - pytest tests

    deploy_job:
      stage: deploy
      script:
        - lain use test
        - lain deploy --build

上边三个 Job, 每一个都仅有短短几行配置, 便完成了 CI 构建, 测试和上线的工作. 如果你能想到 CI 里还有什么 lain 的妙用, 也可以轻松仿照上边的配置文件来书写.

lain 镜像的其他用途
^^^^^^^^^^^^^^^^^^^

lain image 的存在几乎完全是为了简化 CI 的使用, 但如果你的程序要以某种方式来使用 lain, 也可以考虑直接采用 lain image 作为你的 base 镜像.

同时, 由于 lain image 里边富集了如此多的生产力工具, 你甚至可以直接在 lain 容器里使用 :code:`lain`, 只需要类似这样的小脚本就够了:

.. code-block:: bash

    #!/usr/bin/env bash
    set -euo pipefail
    IFS=$'\n\t'

    IMAGE='lain:latest'
    CONTAINER_NAME=lain

    if [[ -z "$@" ]]; then
      cmd=zsh
    else
      cmd="$@"
    fi

    docker pull $IMAGE

    set +e
    docker run -it --rm \
      --name lain \
      --net host \
      -v "$PWD:/src" \
      -v "/var/run/docker.sock:/var/run/docker.sock" \
      -v /tmp:/tmp \
      -w /src \
      -e TERM=xterm \
      "${IMAGE}" zsh -c $cmd

正常情况下还是不推荐在本地用 lain image 的, 最好还是乖乖用 pip 安装, 毕竟这样更快捷.

Review 与审计
-------------

团队做事情, 肯定少不了 Review 和审计, 这里介绍 lain 体系下的一些实践:

项目配置的维护和 Review
^^^^^^^^^^^^^^^^^^^^^^^

这点在 :ref:`lain-env`, :ref:`lain-secret` 里也有介绍到, 非敏感信息尽量在代码库里维护, 方便团队 Review. 当然, 若你是 One Man Project, 自己看着办即可, 不一定要遵循最佳实践.

因为都是敏感信息, :code:`lain (secret|env)` 里的内容修改了, 自然没办法在公开场合进行 Review 了, 但如果你的项目设置了 webhook, 那么 lain 会将修改的部分 (只有 keys, 没有 values) 发送到 webhook notification, 让团队知悉你的改动.

查看某次部署是谁操作的
^^^^^^^^^^^^^^^^^^^^^^

生产性质的项目都应该声明 webhook, 每次 :code:`lain deploy`, :code:`lain (secret|env) edit` 都会发送通知, 记录操作者和其他信息, 但如果要追溯历史操作, 可以直接用 helm:

.. code-block:: bash

    # 查看最近的一次部署是谁操作的
    helm get values avln-server | ag user
    # 查看某一次历史部署是谁操作的
    helm history avln-server
    helm get values --revision=15 avln-server | ag user
