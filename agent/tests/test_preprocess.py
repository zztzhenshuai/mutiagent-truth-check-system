"""
tests/test_preprocess.py

测试 D 负责的文章预处理管道。
"""

from agent.preprocess import extract_paragraphs, preprocess_article


ARTICLE = (
    "主标题\n\n"
    "北京是中国的首都。上海是中国最大的城市？\n\n"
    "2024\n\n"
    "他补充说：“今年增速会放缓……” 但下一句仍需保留。"
)


def test_extract_paragraphs_preserves_offsets():
    paragraphs = extract_paragraphs(ARTICLE)

    assert len(paragraphs) == 4
    for paragraph in paragraphs:
        assert ARTICLE[paragraph.start_offset:paragraph.end_offset] == paragraph.text


def test_preprocess_filters_titles_and_numeric_noise():
    processed = preprocess_article(ARTICLE)

    sentence_texts = [sentence.text for sentence in processed.sentences]
    assert "主标题" not in sentence_texts
    assert "2024" not in sentence_texts
    assert "北京是中国的首都。" in sentence_texts
    assert "上海是中国最大的城市？" in sentence_texts


def test_preprocess_keeps_sentence_offsets_and_paragraph_ids():
    processed = preprocess_article(ARTICLE)

    target = next(sentence for sentence in processed.sentences if sentence.text == "北京是中国的首都。")
    assert ARTICLE[target.start_offset:target.end_offset] == target.text
    assert target.paragraph_id == 1


def test_preprocess_handles_ellipsis_and_trailing_quotes():
    processed = preprocess_article(ARTICLE)

    target = next(sentence for sentence in processed.sentences if "今年增速会放缓" in sentence.text)
    assert target.text == "他补充说：“今年增速会放缓……”"
    assert ARTICLE[target.start_offset:target.end_offset] == target.text

