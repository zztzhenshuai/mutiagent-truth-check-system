"""
tests/conftest.py

为当前仓库提供最小化的 asyncio 测试支持，
避免在未安装 pytest-asyncio 时无法运行已有异步单测。
"""

from __future__ import annotations

import asyncio
import inspect


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "asyncio: run async test with asyncio.run")


def pytest_pyfunc_call(pyfuncitem) -> bool | None:
    test_function = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_function):
        return None

    kwargs = {
        name: pyfuncitem.funcargs[name]
        for name in pyfuncitem._fixtureinfo.argnames
    }
    asyncio.run(test_function(**kwargs))
    return True
