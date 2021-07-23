最佳实践
========

值得一提的最佳实践和窍门, 在这里进行罗列.

标准化操作流程 (SOP)
--------------------

作为业务方, 肯定希望自己的上线流程既方便又安全, 这就要求操作要落实成为 `SOP <https://en.wikipedia.org/wiki/Standard_operating_procedure>`_, 并且需要具备可发现性, 同时可 review / rollback. 以下是 lain 推荐的实践:

* 变更应用配置之前, 往往希望对操作进行 review, 因此建议将集群的(非机密)配置放在代码库里, 方便跟踪变更和 review. 只有敏感信息才存在 :code:`lain [env|secret]` 内.
* 也正因为 :code:`lain [env|secret]` 里的内容不方便 review, 因此每次修改这些内容时, lain 会发送提示消息到 webhook 里, 提醒开发者及时 review.
* 如果你的应用需要执行 migration 操作, 建议将 migration 固化为 :code:`values.jobs` (参考 :ref:`auto-migration`), 这样一来, 每次执行 :code:`lain deploy` 都会运行 migration job, 免除了忘记执行的问题.
* 如果你的应用流量巨大, 实例数众多, 务必要 :ref:`对 strategy 进行微调 <deploy-strategy>`, 让 Kubernetes 缓慢地进行滚动上线操作, 避免真的出现异常时, 事故迅速升级.
* :code:`lain deploy` 执行完毕以后, 会自动开启一个 :code:`lain status` 面板, 供你观察确认此次操作的"绿灯". "绿灯"是什么? 在 lain 看来, 起码要满足:

  * 没有异常状态的容器
  * 没有异常日志
  * web 服务的 endpoint 运作正常

  满足这几个条件, 作为操作者才能放心离开键盘. 但如果上线操作太频繁导致没精力总是盯梢, 或者压根就是在 CI 里自动执行的, 没有 TTY, 看不到 :code:`lain status`. 你也可以考虑往自动化方向更进一步, 也就是声明出 :code:`values.tests`, 在测试内检查你的应用是否运作正常.

  参考 :ref:`helm-values` 里的测试写法, :code:`lain wait` 做的事情就是, 等待所有容器进入正常 Running 的状态, 如果超时便报错. 你还可以补充更多自己的测试, 建设出更完善的检查流程(比方说检查容器日志有无异常, 甚至 sentry 有没有新的 issue!).
* 如果上线以后真的发生异常, 你需要迅速判断接下来的处置:

  * 采集错误信息 - 这个一般由 sentry 负责, 也许你还需要用 :code:`lain logs` 收集一下错误日志, 如果容器卡在启动环节, 日志不一定会进入 pipeline (比如 Fluentd --> ES --> Kibana), 这时候唯一的日志来源就是 :code:`kubectl logs` 了, 也就是 :code:`lain logs`.
  * 进一步在容器里进行 debug - 生产事故十万火急, 一般都急着回滚了, 但如果有条件, 确实可以 :code:`lain x` 进入容器内进行一些 debug 和信息采集.
  * 回滚 - 在本地操作 :code:`lain rollback`, 命令 helm 把你的应用回滚到上一个版本. 与 :code:`lain deploy` 相仿, 执行完 rollback 后, 也会自动开启 :code:`lain status`, 供你观察回滚状态.

但也请注意, 这里讲述的最佳实践, 也基本上是针对大型协作项目, 如果你是一个 one man project, 或者是一个次优先级项目, 那不妨按照自己觉得最高效的方式行事. "次优先级项目"是啥意思? 就是挂了影响也不大, 因此自然没必要盯梢上线.

.. _auto-migration:

Auto Migration
--------------

上线如果忘了做 Migration, 那十有八九就事故了. 因此极力建议把 Migration 步骤写在 :code:`values.jobs`, 这样一来 :code:`lain deploy` 便会自动为你执行 Migration.

.. code-block:: yaml

    # 如果你的应用需要做一些类似数据库初始化操作, 可以照着这个示范写一个 migrate job
    # 各种诸如 env, resources 之类的字段都支持, 如果需要的话也可以单独超载
    jobs:
      init:
        ttlSecondsAfterFinished: 86400  # https://kubernetes.io/docs/concepts/workloads/controllers/job/#clean-up-finished-jobs-automatically
        activeDeadlineSeconds: 3600  # 超时时间, https://kubernetes.io/docs/concepts/workloads/controllers/job/#job-termination-and-cleanup
        backoffLimit: 0  # https://kubernetes.io/docs/concepts/workloads/controllers/job/#pod-backoff-failure-policy
        # 执行 DDL 前, 先对数据库做备份, 稳
        initContainers:
          - name: backup
            image: ccr.ccs.tencentyun.com/yashi/ubuntu-python:latest
            command:
              - 'bash'
              - '-c'
              - |
                mysqldump --default-character-set=utf8mb4 --single-transaction --set-gtid-purged=OFF -h$MYSQL_HOST -p$MYSQL_PASSWORD -u$MYSQL_USER $MYSQL_DB | gzip -c > /jfs/backup/{{ appname }}/$MYSQL_DB-backup.sql.gz
            # 注意下面这里并不是照抄就能用的!
            # jfs-backup-dir 需要在 volumes 下声明出来, 才能在这里引用
            # 详见 "撰写 Helm Values" 这一节的示范
            volumeMounts:
              - name: jfs-backup-dir
                mountPath: /jfs/backup/{{ appname }}/  # 这个目录需要你手动创建好
        # 以下 annotation 能保证 helm 在 upgrade 之前运行该 job, 不成功不继续进行 deploy
        annotations:
          "helm.sh/hook": pre-upgrade
          "helm.sh/hook-delete-policy": before-hook-creation
        command:
          - 'bash'
          - '-c'
          - |
            set -e
            alembic upgrade heads

即便有了 Auto-Migration, 业务其实也有放心不下的事情: 上线都是 CI 来执行的, 做 Daily Release 的时候, CI 可不知道这一次上线需不需要执行 DDL, 万一出现死锁的话, 那可就事故了.

因此如果需要阻止 CI 进行需要 Migration 的上线任务, 可以用类似下方这个脚本来检查是否需要做 Migration, 如果有则打断 CI, 并且发消息到频道里, 提醒手动上线.

.. code-block:: bash

    #!/usr/bin/env bash
    set -euo pipefail
    IFS=$'\n\t'


    current=$(lain x -- bash -c 'basename $(alembic show current|grep Path|sed "s/Path: //")' | grep -o -E "^\w+\.py")
    head=$(basename $(ls alembic/versions/ -t1 -p | head -n1))

    if [ "$current" != "$head" ]; then
      msg="refuse to deploy due to alembic differences:
      current $current
      head $head
      job url: $CI_JOB_URL"
      echo $msg
      lain send-msg $msg
      exit 1
    fi

.. warning::

   运行 Job 出问题了! 如何中断?

   * 立刻 ctrl-c 掐断 lain deploy
   * 如果需要获取出错日志, 执行 :code:`lain logs [job-name]` 就能打印出来, 出错的容器不会被清理掉, 但万一容器真的找不到了, 也可以去 kibana 上看日志, 用 :code:`lain status -s` 就能打印出日志链接
   * 如果仅仅是需要打断 Job, 那就需要先获取 job name, 怎么找呢? 可以用以下方法:

     * 用 :code:`lain status` 找到 pod name, 例如 :code:`[APPNAME]-migration-xxx`, 那么 job name 便是 :code:`[APPNAME]-migration`
     * :code:`kubectl get job | ack [APPNAME]`

   * 知道 job name 就好办了, 执行 :code:`kubectl delete job [job name]`, Job 就被删除了
   * 对于 MySQL Migration, 删掉 Job 还不算完, 毕竟指令已经提交给数据库了, 你需要连上数据库, :code:`show processlist` 地研究为什么 Migration 会死锁, 并且对罪魁祸首的命令执行 Kill.

.. _deploy-strategy:

滚动上线
--------

滚动上线是一个最为常见的实践, 但要注意, 如果你的实例数众多 (>20), 并且存在超售 CPU 的情况, 那你最好对 `update strategy <https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#updating-a-deployment>`_ 进行调整适配, 防止同时启动大量容器的时候, 对节点 CPU 占用过高, 导致 `启动拥堵 <https://github.com/kubernetes/kubernetes/issues/3312>`_.

.. code-block:: yaml

    # values-prod.yaml
    deployments:
      web:
        strategy:
          type: RollingUpdate
          rollingUpdate:
            # 每次只滚动一个容器, 稳
            maxSurge: 1
            maxUnavailable: 1

同理, 如果你的应用第一次上线, 那最好不要一下子全量上线, 而是一次 10 个左右地递增. 某些应用启动期间有一瞬的 CPU 用量极高, 而之后则进入静息状态, 这种情况大家都喜欢写成 low requests, high limits. 这么做本来也没什么毛病, 但若是一下子启动大量容器, 节点的 CPU 就不一定能撑住了, 进入卡死状态, 最终只能重启节点才能解决.

把一个代码仓库部署成两个 APP
----------------------------

为啥一个仓库会想要部署成两个 APP? 这不是故意增加维护难度吗?

这么说吧, 很多应用的开发场景都有各种"难言之隐", 比如一个后端项目, 及承担 2c 的流量, 同时又作为管理后台的 API server. 作为内部系统的部分, 希望快速上线, 解决内需, 而面相客户的部分, 则需要谨慎操作, 装车发版. 这就需要两部分单独上线, 互不影响. 又或者开发者手上只有一个集群, 但也一样需要测试环境 + 生产环境, 这时候也需要考虑把一个代码仓库部署成两个 APP.

目前这件事有以下做法:

用 :code:`lain update-image` 单独更新 proc
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

把你的应用里需要单独部署的部分拆成单独的 proc, 用 update-image 进行部署:

.. code-block:: yaml

    appname: dummy

    deployments:
      web:
        replicaCount: 20
        containerPort: 5000
      # web-dev 与 web 是两个不同的 deploy
      # 而用 lain update-image 上线的时候只会更新一个 deploy 的镜像
      # 达到了互不影响的效果
      web-dev:
        replicaCount: 1
        containerPort: 5000

    # 如果需要的话, web-dev 也可以有自己的域名, 声明 ingress 的时候注意写对 deployName 就行
    # 如果不需要域名, 仅在集群内访问, 那么可以用 svc 访问, 也就是 dummy-web-dev:5000
    ingresses:
      - host: dummy-dev
        deployName: web-dev
        paths:
          - /

此法的一些特点, 和需要注意的地方:

* 如果有多个 proc 需要单独更新, 那么 update-image 命令便会显得有点长, 比如 :code:`lain update-image web-dev worker-dev`, 最好由 CI 代执行, 或者脚本化
* 单独更新 web-dev, 只能使用 lain update-image, 因此也仅仅能用来更新镜像, 其他的 values 配置改动将无法用该命令上线
* 如果 values 发生变动需要上线, 则必须用 :code:`lain deploy`, 这样就是"整体上线", web 和 web-dev 都会重新部署
* 每一个 proc 可以单独在 values 里锁死 imageTag, 示范请参考 :ref:`values.yaml 模板 <helm-values>`, 搜索 :code:`imageTag`, 这样一来, 无论怎么 :code:`lain deploy`, lain 都会尊重写死在 values 里边的值

在 values 里超载 appname
^^^^^^^^^^^^^^^^^^^^^^^^

在 chart 目录下多放一份 `values-dev.yaml`, 命名其实是任意的, 只要不与集群名称冲突就好. 这种办法灵活性更高, 当然也更复杂.

.. code-block:: yaml

    # values-dev.yaml
    # 这里仅仅超载了 appname, 如果需要的话, 域名也得做好相应的修改
    appname: dummy-dev

让超载的 values-dev.yaml 生效, 需要给 lain 传参:

.. code-block:: bash

    lain -f chart/values-dev.yaml deploy --build
    lain -f chart/values-dev.yaml status
    # 其他的各种命令, 也都需要加上 -f 参数

此法的一些特点, 和需要注意的地方:

* 灵活性大, 你可以在 :code:`values-dev.yaml` 里随心所欲地超载
* 由于修改了 appname, 在 lain 看来就是一个全新的 app 了, 那么自然, 镜像是没办法复用的, 你需要重新构建
* 操作 dummy-dev 这个 app 时, 所有 lain 命令都需要加上 :code:`-f chart/values-dev.yaml`, 并不是特别方便
