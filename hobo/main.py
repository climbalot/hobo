"""Process entrypoint. Only wiring: configure logging, load config, build the
application graph, run it. All lifecycle lives in hobo/application.py, all
assembly in hobo/builder.py.
"""

from __future__ import annotations

import asyncio

from hobo.config import Config
from hobo.obs.logging_config import configure_logging
from hobo.builder import build_application


def main() -> None:
    configure_logging()
    config = Config.load()
    asyncio.run(build_application(config).run())


if __name__ == "__main__":
    main()
