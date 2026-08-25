# Online shared_buffers branch archive

This directory records the openGauss kernel commit used by the Huawei7
Sysbench five-stage online acceptance run.

## Source repository

- Repository: `openGauss-server-5.1.0`
- Base: `b5a8d5b056bbe660a6315cb424253717fb32cd04`
- Branch: `feature/online-shared-buffers-ppt-five-stage-20260825`
- Commit: `b314224d8b0a0c25e5212297914ca89d43275929`

The bundle is a small Git transport archive containing the branch commit and
its delta from the `5.1.0` base:

```bash
git clone https://gitee.com/opengauss/openGauss-server.git
cd openGauss-server
git bundle verify /path/to/openGauss-server-online-sb-20260825.bundle
git fetch /path/to/openGauss-server-online-sb-20260825.bundle \
  feature/online-shared-buffers-ppt-five-stage-20260825
git switch feature/online-shared-buffers-ppt-five-stage-20260825
```

The bundle is included because this environment does not have credentials to
push directly to the openGauss Gitee remote. The Huawei7 workflow branch,
including this bundle, is pushed to the GaussTune GitHub remote.
