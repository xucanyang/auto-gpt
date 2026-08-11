"""启动本地 Turnstile Solver 服务"""
import asyncio
import os
import sys
from pathlib import Path


SOLVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SOLVER_DIR.parents[1]
for import_root in (PROJECT_ROOT, SOLVER_DIR):
    import_path = str(import_root)
    if import_path not in sys.path:
        sys.path.insert(0, import_path)

from api_solver import create_app, parse_args


def _prepend_env_path(name: str, value: str) -> None:
    current = os.getenv(name, "")
    parts = [p for p in current.split(":") if p]
    if value in parts:
        return
    os.environ[name] = ":".join([value, *parts]) if parts else value


def _prepare_camoufox_env(browser_type: str) -> None:
    if browser_type != "camoufox" or os.name == "nt":
        return
    try:
        from platformdirs import user_cache_dir
    except Exception:
        return

    camoufox_dir = Path(user_cache_dir("camoufox"))
    if camoufox_dir.is_dir():
        _prepend_env_path("LD_LIBRARY_PATH", str(camoufox_dir))

if __name__ == "__main__":
    args = parse_args()
    _prepare_camoufox_env(args.browser_type)
    app = create_app(
        headless=not args.no_headless,
        useragent=args.useragent,
        debug=args.debug,
        browser_type=args.browser_type,
        thread=args.thread,
        proxy_support=args.proxy,
        use_random_config=args.random,
        browser_name=args.browser,
        browser_version=args.version,
    )
    app.run(host=args.host, port=int(args.port))
