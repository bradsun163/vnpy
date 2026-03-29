import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from paper_trading_mvp.live_runner import run_from_path


def main() -> int:
    config_path = Path(__file__).with_name("live_ctp_paper_config.json")
    return run_from_path(config_path)


if __name__ == "__main__":
    raise SystemExit(main())
