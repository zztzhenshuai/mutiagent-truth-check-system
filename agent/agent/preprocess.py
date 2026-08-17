"""
agent/preprocess.py

文章预处理管道：
1. 提取段落结构及其原文 offset
2. 在段落内进行中文分句
3. 过滤标题、纯数字行、过短句等噪声
4. 保留句子在原文中的字符偏移量，供高亮与交叉引用使用
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re


_SENTENCE_ENDINGS = {"。", "！", "？", "!", "?", "；", ";"}
_TRAILING_CLOSERS = {'"', "'", "”", "’", "」", "』", "）", ")", "】", "]", "》", ">"}
_MIN_EFFECTIVE_CHARS = 5
_MAX_TITLE_LENGTH = 20


@dataclass(frozen=True)
class ProcessedSentence:
    text: str
    start_offset: int
    end_offset: int
    paragraph_id: int


@dataclass(frozen=True)
class ProcessedParagraph:
    id: int
    text: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class ProcessedArticle:
    raw_text: str
    paragraphs: list[ProcessedParagraph] = field(default_factory=list)
    sentences: list[ProcessedSentence] = field(default_factory=list)


def _is_blank_line(line: str) -> bool:
    return not line.strip()


def _trimmed_span(text: str, start_offset: int) -> tuple[str, int, int]:
    stripped = text.strip()
    if not stripped:
        return "", start_offset, start_offset

    leading = len(text) - len(text.lstrip())
    trailing = len(text) - len(text.rstrip())
    start = start_offset + leading
    end = start_offset + len(text) - trailing
    return stripped, start, end


def extract_paragraphs(raw_text: str) -> list[ProcessedParagraph]:
    """按空行切分段落，并保留段落在原文中的 offset。"""
    paragraphs: list[ProcessedParagraph] = []
    lines = raw_text.splitlines(keepends=True)

    paragraph_start: int | None = None
    paragraph_parts: list[str] = []
    cursor = 0

    def flush() -> None:
        nonlocal paragraph_start, paragraph_parts
        if paragraph_start is None or not paragraph_parts:
            paragraph_start = None
            paragraph_parts = []
            return

        raw_paragraph = "".join(paragraph_parts)
        text, start, end = _trimmed_span(raw_paragraph, paragraph_start)
        if text:
            paragraphs.append(
                ProcessedParagraph(
                    id=len(paragraphs),
                    text=text,
                    start_offset=start,
                    end_offset=end,
                )
            )

        paragraph_start = None
        paragraph_parts = []

    for line in lines:
        if _is_blank_line(line):
            flush()
        else:
            if paragraph_start is None:
                paragraph_start = cursor
            paragraph_parts.append(line)
        cursor += len(line)

    if cursor < len(raw_text):
        trailing = raw_text[cursor:]
        if not _is_blank_line(trailing):
            if paragraph_start is None:
                paragraph_start = cursor
            paragraph_parts.append(trailing)

    flush()
    return paragraphs


def _consume_ellipsis(text: str, index: int) -> int:
    if text.startswith("……", index):
        return 2

    dot_run = re.match(r"\.{3,}", text[index:])
    if dot_run:
        return len(dot_run.group(0))

    return 0


def _sentence_break_length(text: str, index: int) -> int:
    char = text[index]
    if char in _SENTENCE_ENDINGS:
        return 1
    return _consume_ellipsis(text, index)


def _is_noise_sentence(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True

    terminal_text = stripped
    while terminal_text and terminal_text[-1] in _TRAILING_CLOSERS:
        terminal_text = terminal_text[:-1].rstrip()

    effective = re.sub(r"[\W_]+", "", stripped, flags=re.UNICODE)
    if len(effective) < _MIN_EFFECTIVE_CHARS:
        return True

    if re.fullmatch(r"[\d\s.,:%/%+-]+", stripped):
        return True

    has_terminal_punctuation = (
        bool(terminal_text)
        and (
            terminal_text[-1] in _SENTENCE_ENDINGS
            or terminal_text.endswith("……")
            or re.search(r"\.{3,}$", terminal_text) is not None
        )
    )
    if not has_terminal_punctuation and len(stripped) <= _MAX_TITLE_LENGTH:
        return True

    return False


def split_sentences(paragraph: ProcessedParagraph) -> list[ProcessedSentence]:
    """在单段内分句，保留句子在原文中的 offset。"""
    text = paragraph.text
    sentences: list[ProcessedSentence] = []
    sentence_start = 0
    index = 0

    while index < len(text):
        break_len = _sentence_break_length(text, index)
        if break_len:
            sentence_end = index + break_len
            while sentence_end < len(text) and text[sentence_end] in _TRAILING_CLOSERS:
                sentence_end += 1

            raw_sentence = text[sentence_start:sentence_end]
            trimmed, start, end = _trimmed_span(raw_sentence, paragraph.start_offset + sentence_start)
            if trimmed and not _is_noise_sentence(trimmed):
                sentences.append(
                    ProcessedSentence(
                        text=trimmed,
                        start_offset=start,
                        end_offset=end,
                        paragraph_id=paragraph.id,
                    )
                )

            sentence_start = sentence_end
            while sentence_start < len(text) and text[sentence_start].isspace():
                sentence_start += 1
            index = sentence_start
            continue

        index += 1

    if sentence_start < len(text):
        raw_sentence = text[sentence_start:]
        trimmed, start, end = _trimmed_span(raw_sentence, paragraph.start_offset + sentence_start)
        if trimmed and not _is_noise_sentence(trimmed):
            sentences.append(
                ProcessedSentence(
                    text=trimmed,
                    start_offset=start,
                    end_offset=end,
                    paragraph_id=paragraph.id,
                )
            )

    return sentences


def preprocess_article(raw_text: str) -> ProcessedArticle:
    """完整预处理：段落提取 + 分句 + 噪声过滤。"""
    paragraphs = extract_paragraphs(raw_text)
    sentences: list[ProcessedSentence] = []
    for paragraph in paragraphs:
        sentences.extend(split_sentences(paragraph))

    return ProcessedArticle(
        raw_text=raw_text,
        paragraphs=paragraphs,
        sentences=sentences,
    )
