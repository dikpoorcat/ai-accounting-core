"""Scalar semantics shared by the native validator and the diagnostic interpreter."""

import re

from jsonschema import ValidationError

from ._scalars import MAX_FEN, MIN_FEN, parse_fen


def integer_bound(validator, bound, instance, schema):
    if isinstance(instance, str):
        try:
            value = parse_fen(instance)
        except ValueError:
            yield ValidationError("整数分必须使用规范 int64 字符串")
            return
        minimum = int(schema.get("x-integer-minimum", MIN_FEN))
        maximum = int(schema.get("x-integer-maximum", MAX_FEN))
        if not minimum <= value <= maximum:
            yield ValidationError("整数分超出合同范围")


def wire_fen(validator, enabled, instance, schema):
    if enabled and isinstance(instance, str):
        try:
            parse_fen(instance)
        except ValueError:
            yield ValidationError("整数分必须使用规范 int64 字符串")


def exact_pattern(validator, pattern, instance, schema):
    if isinstance(instance, str):
        # `$` is full-end only in a doubly anchored contract pattern. Other
        # expressions retain Python search semantics, including Unicode classes.
        match = (
            re.fullmatch(pattern, instance)
            if pattern.startswith("^") and pattern.endswith("$")
            else re.search(pattern, instance)
        )
        if match is None:
            yield ValidationError("字符串格式不符合合同")
