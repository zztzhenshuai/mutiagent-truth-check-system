# cross_reference 工具说明

## 功能范围

成员 D 负责的模块包括：

- 文章预处理管道：段落提取、中文分句、噪声过滤、字符偏移量保留
- `cross_reference` 工具：句向量检索 + LLM 矛盾判断

## 模型缓存

`cross_reference` 默认使用 `paraphrase-multilingual-MiniLM-L12-v2`，并将缓存目录固定到仓库内：

```text
.cache/huggingface/
```

可通过环境变量覆盖：

```env
CROSS_REFERENCE_CACHE_DIR=.cache/huggingface
CROSS_REFERENCE_LOCAL_FILES_ONLY=false
LOCAL_MODEL_PATH=models/cross-reference/paraphrase-multilingual-MiniLM-L12-v2
```

- `CROSS_REFERENCE_CACHE_DIR`：模型缓存目录。相对路径按仓库根目录解析。
- `CROSS_REFERENCE_LOCAL_FILES_ONLY=true`：仅使用本地缓存，不尝试联网下载。
- `LOCAL_MODEL_PATH`：本地 `sentence-transformers` 模型目录。相对路径按仓库根目录解析；当连接 Hugging Face 超时或失败时自动回退到这里。

## 首次预热模型

安装依赖后执行：

```bash
python -m agent.tools.cross_reference
```

该命令会加载 `paraphrase-multilingual-MiniLM-L12-v2`，并把缓存写入仓库下的 `.cache/huggingface/`。

若配置了 `LOCAL_MODEL_PATH`，并且 Hugging Face 连接超时，工具会自动改为加载该目录下的本地模型。这里应放的是**解压后的模型目录**，不是 zip 压缩包；目录内建议直接包含模型根目录文件（例如 `config.json`、`modules.json`、`tokenizer.json`、`sentence_bert_config.json`）以及子目录 `1_Pooling/` 等。

建议依赖：

```bash
pip install sentence-transformers transformers torch numpy python-dotenv
```

## 运行时接入

`Agent.run()` 在每篇文章开始处理前会自动调用：

```python
await prepare_cross_reference_context(article_text, llm=self._llm)
```

因此 ReAct 在后续调用 `cross_reference` 时，工具已经持有当前文章的句子索引与偏移量信息。

## 工具输入格式

工具注册接口仍遵循 A 定义的：

```python
async def cross_reference(input: str) -> str
```

支持两种输入方式：

1. 直接传声明文本

```text
北京是中国的首都。
```

2. 传 JSON 字符串

```json
{"claim": "北京是中国的首都。", "top_k": 5, "threshold": 0.6}
```

## 工具输出格式

工具返回纯文本，包含：

- 原始声明
- Top-K 相关句及相似度
- LLM 输出的矛盾结论
- JSON 解析失败或未配置 LLM 时的降级备注
