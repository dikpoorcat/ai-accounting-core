"""Chinese display names follow phonetic order rather than code point order."""

from ai_accounting.kernel.text_sort import pinyin_key


def test_chinese_names_follow_full_pinyin_a_to_z():
    assert sorted(["张三", "王五", "赵六", "李四"], key=pinyin_key) == [
        "李四", "王五", "张三", "赵六",
    ]


def test_pinyin_uses_phrase_readings_and_combines_latin_names():
    assert sorted(["张三", "重庆", "bob", "Alice", "李四"], key=pinyin_key) == [
        "Alice", "bob", "重庆", "李四", "张三",
    ]


def test_same_pinyin_has_stable_original_text_ties():
    assert sorted(["李", "里"], key=pinyin_key) == sorted(["里", "李"], key=pinyin_key)
