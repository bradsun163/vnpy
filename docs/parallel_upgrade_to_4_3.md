# Parallel Upgrade To 4.3.0

This repository currently contains local custom work and a repaired vn.py 3.9.0 runtime for paper trading.

Do not replace it in place.

Use the parallel upgrade script below instead:

```powershell
.\scripts\setup_parallel_vnpy_4_3.ps1
```

What the script does:

- fetches the official upstream tag `4.3.0`
- creates a sibling git worktree checked out at that tag
- installs the official latest package line into a separate `--target` directory using the system Python interpreter
- includes the official `vnpy_riskmanager`, `vnpy_datamanager`, `vnpy_datarecorder`, and `vnpy_spreadtrading` packages as part of the parallel stack
- avoids virtual environments and avoids overwriting the current repaired 3.9.0 setup

After setup, run the parallel line by prefixing `PYTHONPATH` with both the package target and the 4.3.0 worktree.

Recommended migration order:

1. validate raw imports and GUI bootstrap on 4.3.0
2. validate CTP/CTPTEST connectivity in the parallel line
3. migrate custom scripts and strategies one by one
4. only switch the weekly paper trading workflow after two stable dry runs

Reason for using a parallel upgrade instead of an in-place upgrade:

- local `.vntrader` contains historical config and state
- current repository now has local MVP additions
- the jump from core `3.9.0` to `4.3.0` is a major-version migration, not a patch update
- current external gateway issues are not yet proven to be caused by the old core