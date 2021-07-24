.. _dev:

开发文档
========

为你的团队启用 lain
-------------------

使用 lain 是轻松高效的, 但为你的团队启用 lain, 却不是一件轻松的事情. 这是由 lain 本身的设计决定的: lain 没有 server side component (因为功能都基于 helm), 而且不需要用户维护集群配置(都写死在 :code:`cluster.py` 里了, 随包发布). 这是 lain 的最大特点 + 卖点, 针对用户的易用性都不是免费的, 都要靠 SA 的辛勤劳作才能挣得.

目前而言, 在你的团队启用 lain, 需要满足以下条件:

* Kubernetes 集群, Apiserver 服务向内网暴露, kubeconfig 发布给所有团队成员
* Docker Registry, 云原生时代, 这应该是每一家互联网公司必不可少的基础设施, lain 目前支持一系列 Registry: Harbor, 阿里云, 腾讯云, 以及原生的 Docker Registry.
* 你熟悉 Python, 有能力维护 lain 的内部分支, 以及打包发布.
* [可选] PyPI, 或者类 PyPI 的 Package Registry, 比如 `GitLab Package Registry <https://docs.gitlab.com/ee/user/packages/pypi_repository/>`_, lain 的代码里实现了检查新版, 自动提示升级. 如果你们是一个快节奏的开发团队, lain 的使用必定会遇到各种需要维护的情况, 因此应该尽量有一个内网 Package Index.
* [可选] Prometheus, Grafana, Kibana, 这些将会给 lain 提供强大的周边服务, 具体有什么用? 那就任君想象了, 云平台和监控/日志系统整合以后, 能做的事情那可太多了.
* [可选] 你的团队使用 GitLab 和 GitLab CI, 以我们内部现状, 大部分 DevOps 都基于 GitLab CI + lain, 如果你也恰好如此, 那便有很多工作可以分享.
* [可选] 你的团队对 Kubernetes + Helm 有着基本的了解, 明白 Kubernetes 的基本架构, 以及 Pod / Deploy / Service / Ingress / Ingress Controller 的基本概念.

假设你满足以上条件, 并且对路上的麻烦事有足够心理准备, 可以按照以下步骤, 让 lain 能为你的团队所用.

书写集群配置
^^^^^^^^^^^^

为了方便开发人员的使用, lain 将各种集群配置全部写死在代码库里: 让用户去抄写集群配置, 永远是不靠谱的, 必定意味着无穷无尽的技术支持工作.

集群配置分布在两个地方, :code:`lain_cli/clusters.py` 和 :code:`lain_cli/cluster_values`.

:code:`clusters.py::CLUSTER` 这个字典便是集群配置的全部内容, 由于是 Python 代码, 你也可以额外添加自己需要的校验步骤. 总而言之, 最基本的示范如下:

.. literalinclude:: ../lain_cli/clusters.py

那么 :code:`lain_cli/cluster_values` 有什么用呢? 这么说吧, 和集群相关的 values 配置, 都应该放在 :code:`lain_cli/cluster_values/values-[CLUSTER].yaml` 下, 比如:

.. literalinclude:: ../lain_cli/cluster_values/values-test.yaml
   :language: yaml

集群配置写好了, 本地也测通各项功能正常使用, 那就想办法发布给你的团队们用了.

打包发版
^^^^^^^^

打包有很多种方式, 既可以上传私有 PyPI 仓库, 也可以把代码库打包, 直接上传到任意能 HTTP 下载的地方, 简单分享下我们曾经用过的打包方案:

.. code-block:: yaml

    # 以下均为 GitLab CI Job
    upload_gitlab_pypi:
      stage: deliver
      rules:
        - if: '$CI_COMMIT_BRANCH == "master" && $CI_PIPELINE_SOURCE != "schedule"'
      allow_failure: true
      script:
        - python setup.py sdist bdist_wheel
        - pip install twine -i https://mirrors.cloud.tencent.com/pypi/simple/
        - TWINE_PASSWORD=${CI_JOB_TOKEN} TWINE_USERNAME=gitlab-ci-token python -m twine upload --repository-url https://gitlab.example.com/api/v4/projects/${CI_PROJECT_ID}/packages/pypi dist/*

    upload_devpi:
      stage: deliver
      rules:
        - if: '$CI_COMMIT_BRANCH == "master" && $CI_PIPELINE_SOURCE != "schedule"'
      variables:
        PACKAGE_NAME: lain_cli
      script:
        - export VERSION=$(cat lain_cli/__init__.py | ag -o "(?<=').+(?=')")
        - devpi login root --password=$PYPI_ROOT_PASSWORD
        - devpi remove $PACKAGE_NAME==$VERSION || true
        - devpi upload

    deliver_job:
      stage: deliver
      except:
        - schedules
      script:
        - ./setup.py sdist
        # 用你自己的方式发布 dist/lain_cli-*.tar.gz

打包发布好了, 大家都顺利安装好了, 但要真的操作集群, 还得持有 kubeconfig 才行, 那我们接下来开始安排发布 kubeconfig.

暴露 Apiserver, 发布 kubeconfig
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

lain 调用的 kubectl, helm, 都是直接和 Kubernetes Apiserver 打交道的, 因此你需要让 Apiserver 对内网可访问.

然后就是 kubeconfig 了, lain 需要 admin 权限的 kubeconfig, 你需要想办法发布给你的团队, 比如用 1password. 大家下载以后, 放置于各自电脑的 :code:`~/.kube/kubeconfig-[CLUSTER]` 目录, 目前 lain 都是在小公司用, 没那么在意权限问题. 关于安全性问题请阅读 :ref:`lain-security-design`.

kubeconfig 也就位了, 那事情就算完成了, 接下来就是教育你的团队, 开始普及 lain, 可以参考 :ref:`quick-start` 的内容.

.. _lain-cluster-config:
