from __future__ import annotations

import asyncio

from tin_lite.runtime import build_runtime
from tin_lite.settings import get_settings


async def run_worker() -> None:
    runtime = await build_runtime(get_settings())
    try:
        await runtime.worker.run()
    finally:
        if runtime.luna is not None:
            await runtime.luna.close()
        await runtime.model_router.close()
        await runtime.storage.close()
        if runtime.codex_api is not None:
            await runtime.codex_api.close()
        await runtime.database.close()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
