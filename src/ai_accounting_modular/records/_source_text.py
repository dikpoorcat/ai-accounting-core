"""Decoded text and real CSV cells; long contents are split by the inspection pager."""

import codecs
import csv
import io

from openpyxl.utils import get_column_letter

from ._types import SOURCE_BLOCK_CHARS


def _encoding(stream):
    head = stream.read(4)
    stream.seek(0)
    if head.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return "utf-32"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    try:
        decoder = codecs.getincrementaldecoder("utf-8-sig")()
        while chunk := stream.read(65536):
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
        return "utf-8-sig"
    except UnicodeDecodeError:
        return "gb18030"
    finally:
        stream.seek(0)


def blocks(stream, *, csv_format, sections):
    section = "csv" if csv_format else "text"
    sections.add(section)
    encoding = _encoding(stream)
    wrapper = io.TextIOWrapper(stream, encoding=encoding, newline="" if csv_format else None)
    try:
        if csv_format:
            # Follow the source byte contract instead of truncating valid long CSV cells.
            csv.field_size_limit(2147483647)
            reader = csv.reader(wrapper, strict=True)
            for row, values in enumerate(reader, 1):
                for column, value in enumerate(values, 1):
                    if value != "":
                        # CSV has no Excel width limit; keep exact numeric positions beyond it.
                        position = (
                            f"{get_column_letter(column)}{row}"
                            if column <= 18278
                            else f"row:{row}:column:{column}"
                        )
                        yield section, f"CSV!{position}", value
        else:
            line, offset = 1, 0
            while value := wrapper.readline(SOURCE_BLOCK_CHARS):
                yield section, f"line:{line}:chars:{offset + 1}-{offset + len(value)}", value
                if value.endswith("\n"):
                    line, offset = line + 1, 0
                else:
                    offset += len(value)
    finally:
        # The owner keeps the verified source lease open for the complete parse.
        wrapper.detach()
