# Walkthrough: Member E 迭代三工具与测试实现结果

我们已成功完成迭代三中 **Member E** 的全部开发与测试内容。以下是实现的更改和验证结果的详细说明。

---

## 1. 修改与新增文件汇总

### 工具模块实现
- **[NEW] [wikidata_lookup.py](file:///home/hhikr/Learn/JobSeek/projects/agent/agent/tools/wikidata_lookup.py)**: 查询维基数据 API，解析通用结构化实体三元组属性，自动批量解析关联实体 QID 的中文标签。
- **[NEW] [official_statistics.py](file:///home/hhikr/Learn/JobSeek/projects/agent/agent/tools/official_statistics.py)**: 核对经济金融宏观数据（GDP增速、通胀率、失业率），具有鲁棒性输入解析，在线 World Bank 查询超时或失败时能自动执行降级，读取本地静态经济缓存。
- **[NEW] [pubmed_search.py](file:///home/hhikr/Learn/JobSeek/projects/agent/agent/tools/pubmed_search.py)**: 检索医学 PubMed 数据库。通过 `esearch` 获取 PMID 并在 `esummary` 获取 Title/Journal/Year/Authors 元数据。
- **[NEW] [academic_search.py](file:///home/hhikr/Learn/JobSeek/projects/agent/agent/tools/academic_search.py)**: 调用 Semantic Scholar API，检索学术论文标题、发表年份、来源、被引频次及摘要。
- **[NEW] [fact_check.py](file:///home/hhikr/Learn/JobSeek/projects/agent/agent/tools/fact_check.py)**: 检索辟谣和新闻事实核查库，在未配置 Google API Key 时降级至网络辟谣检索（`web_search`）。

### 本地缓存库
- **[NEW] [world_bank_cache.json](file:///home/hhikr/Learn/JobSeek/projects/agent/datasets/world_bank_cache.json)**: 本地缓存常用国家（CN, US, JP, DE, WLD）自 2020 至 2024 年的 GDP 增长率、CPI 通胀率与失业率，提供 100% 可靠的本地 Fallback 验证。

### 注册表与协作接口
- **[MODIFY] [registry.py](file:///home/hhikr/Learn/JobSeek/projects/agent/agent/tools/registry.py)**:
  - 扩展 `ToolSpec` 增加 `domains` 字段，完成 5 个新增工具在 `TOOL_REGISTRY` 中的高质描述注册。
  - 实现 `get_allowed_tools(active_domains: list[str]) -> list[ToolSpec]`，用于基于 Member D 匹配的 Skill/Domain 动态限制 Agent 工具的可见性。

### 自动化测试
- **[NEW] [test_new_tools.py](file:///home/hhikr/Learn/JobSeek/projects/agent/tests/test_new_tools.py)**: 完整的 Pytest 测试用例，通过 mock 模拟 API 返回数据，覆盖正常查询、异常降级、缓存加载和路由过滤。
- **[NEW] [test_tools_iteration_3.py](file:///home/hhikr/Learn/JobSeek/projects/agent/agent/test_tools_iteration_3.py)**: 用于演示和手动真实网络调试的脚本。

---

## 2. 自动化测试结果

我们在 zsh Conda base 环境中运行了测试：
```bash
zsh -c "source ~/.zshrc && conda activate base && python -m pytest tests/test_new_tools.py -v"
```

测试执行全部通过，结果如下：
```text
============================= test session starts ==============================
platform linux -- Python 3.13.12, pytest-9.0.3, pluggy-1.5.0 -- /opt/miniconda3/bin/python
cachedir: .pytest_cache
rootdir: /home/hhikr/Learn/JobSeek/projects/agent
plugins: Faker-24.11.0, asyncio-1.3.0, anyio-4.10.0
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 8 items

tests/test_new_tools.py::test_get_allowed_tools PASSED                   [ 12%]
tests/test_new_tools.py::test_wikidata_lookup_success PASSED             [ 25%]
tests/test_new_tools.py::test_official_statistics_lookup_online PASSED   [ 37%]
tests/test_new_tools.py::test_official_statistics_lookup_fallback PASSED [ 50%]
tests/test_new_tools.py::test_pubmed_search PASSED                       [ 62%]
tests/test_new_tools.py::test_semantic_scholar_search PASSED             [ 75%]
tests/test_new_tools.py::test_fact_check_registry_api PASSED             [ 87%]
tests/test_new_tools.py::test_fact_check_registry_fallback PASSED        [100%]
============================== 8 passed in 1.33s ===============================
```

---

## 3. 手动验证说明

你可以直接通过命令行执行以下诊断脚本，来测试真实网络环境下工具的返回结果：

```bash
zsh -c "source ~/.zshrc && conda activate base && python agent/test_tools_iteration_3.py"
```

该脚本将实时输出各个工具查询返回的结构化 Observation，例如：
* `wikidata_lookup` 返回结构化的实体国家、成立年份等事实；
* `pubmed_search` 返回文献 PMID 及其元数据；
* `fact_check_registry` 在未配置 API Key 下自动降级输出网络辟谣线索。
