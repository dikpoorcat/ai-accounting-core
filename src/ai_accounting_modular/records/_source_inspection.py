"""A complete, verified source view with bounded response blocks and real continuation."""

import base64
import binascii
import csv
from pathlib import PurePosixPath

from pypdf import PdfReader
from pypdf.errors import PyPdfError
from xlrd import XLRDError
from xlrd.compdoc import CompDocError

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)

from . import _source_text
from ._types import SOURCE_BLOCK_CHARS

_FORMAT = "records-source-inspection/1"


def _offset(cursor, version):
    if cursor is None:
        return 0
    try:
        value = loads_json(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "offset"}
            or type(value["offset"]) is not int
            or value["offset"] < 0
        ):
            raise ValueError
        validate_contract("core.schema.json", "Digest", value["version"])
    except (ValueError, TypeError, binascii.Error) as error:
        raise ContractError("invalid_input", "来源内容游标无效，请从第一页读取。") from error
    if value["version"] != version:
        raise ContractError("snapshot_changed", "来源内容或读取范围已改变，请从第一页读取。")
    return value["offset"]


def _split(location, text):
    if len(text) <= SOURCE_BLOCK_CHARS:
        if text:
            yield {"location": location, "text": text}
        return
    for first in range(0, len(text), SOURCE_BLOCK_CHARS):
        last = min(first + SOURCE_BLOCK_CHARS, len(text))
        yield {
            "location": f"{location}:chars:{first + 1}-{last}",
            "text": text[first : first + SOURCE_BLOCK_CHARS],
        }


def _pdf(stream, sections, unsupported):
    reader = PdfReader(stream, strict=False)
    if reader.is_encrypted:
        unsupported.append("document:encrypted")
        return
    for number, page in enumerate(reader.pages, 1):
        section = f"page:{number}"
        sections.add(section)
        text = page.extract_text()
        if text is None or not text.strip():
            unsupported.append(section)
        else:
            yield section, section, text


def _scan(stream, source, sections, unsupported):
    head = stream.read(8)
    stream.seek(0)
    suffix = PurePosixPath(source.get("original_name", "")).suffix.casefold()
    if source["kind"] == "owner_statement":
        yield from _source_text.blocks(stream, csv_format=False, sections=sections)
    elif head.startswith(b"%PDF-") or suffix == ".pdf":
        yield from _pdf(stream, sections, unsupported)
    elif head.startswith(b"PK\x03\x04") and suffix in {".xlsx", ".xlsm"}:
        from ._source_spreadsheets import xlsx

        yield from xlsx(stream, sections)
    elif head.startswith(bytes.fromhex("d0cf11e0a1b11ae1")) and suffix == ".xls":
        from ._source_spreadsheets import xls

        yield from xls(stream, sections)
    elif suffix in {".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".log"} or source.get(
        "media_type", ""
    ).startswith("text/"):
        yield from _source_text.blocks(stream, csv_format=suffix == ".csv", sections=sections)
    else:
        unsupported.append("all")


def inspect_source(records, source, request, read):
    section = request.get("section", "all")
    if len(section) > 200:
        raise ContractError(
            "invalid_input", "请选择实际的 all、text、csv、sheet:工作表名或page:页码。"
        )
    version = content_digest(
        {
            "format": _FORMAT,
            "company_id": read.company_id,
            "database_id": read.database_id,
            "source_id": source["source_id"],
            "sha256": source["sha256"],
            "kind": source["kind"],
            "name": source.get("original_name"),
            "media": source.get("media_type"),
            "section": section,
        }
    )
    offset, limit = _offset(request.get("cursor"), version), request["limit"]
    sections, unsupported, items = set(), [], []
    total, filtered = 0, 0
    try:
        with records.lease_source(source["source_id"], read) as stream:
            for block_section, location, text in _scan(stream, source, sections, unsupported):
                for item in _split(location, text):
                    total += 1
                    if section == "all" or section == block_section:
                        if offset <= filtered < offset + limit:
                            items.append(item)
                        filtered += 1
    except (
        PyPdfError,
        XLRDError,
        CompDocError,
        csv.Error,
        UnicodeError,
        ValueError,
        KeyError,
        IndexError,
    ) as error:
        if isinstance(error, ContractError):
            raise
        raise ContractError(
            "source_unreadable", "原件已核验，但内容无法按文件格式完整读取。"
        ) from error
    if section != "all" and section not in sections:
        raise ContractError("invalid_input", "所选来源没有这个实际页面或工作表。")
    if offset > filtered:
        raise ContractError("invalid_input", "来源内容游标超过完整范围，请重新读取。")
    more = offset + len(items) < filtered
    cursor = (
        base64.urlsafe_b64encode(
            canonical_json({"version": version, "offset": offset + len(items)}).encode()
        ).decode()
        if more
        else None
    )
    return validate_contract(
        "core.schema.json",
        "SourceInspection",
        {
            "source_id": source["source_id"],
            "sha256": source["sha256"],
            "section": section,
            "blocks": items,
            "page": {
                "total_count": total,
                "filtered_count": filtered,
                "returned_count": len(items),
                "has_more": more,
                "next_cursor": cursor,
                "collection_version": version,
            },
            "unsupported_media": bool(unsupported),
            "unsupported_sections": list(dict.fromkeys(unsupported)),
        },
    )
