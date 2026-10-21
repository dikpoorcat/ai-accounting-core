"""Read only the installed, generated operating text; never workspace documents."""

from functools import lru_cache
from importlib.resources import files

from ai_accounting_modular.contracts.public import ContractError, loads_json

TOPICS = ("work", "commands", "business", "reporting", "runtime")


@lru_cache(maxsize=1)
def _book():
    try:
        value = loads_json(files(__package__).joinpath("_guides.json").read_bytes())
        if (
            not isinstance(value, dict)
            or set(value) != {"format", "topics", "business"}
            or value["format"] != "ai-accounting-guides/1"
            or not isinstance(value["topics"], dict)
            or set(value["topics"]) != set(TOPICS)
            or not isinstance(value["business"], dict)
            or set(value["business"]) != {"kinds", "sections", "common", "corrections"}
        ):
            raise ValueError("Invalid installed guide directory")
        for topic in value["topics"].values():
            if (
                not isinstance(topic, dict)
                or set(topic) != {"title", "source", "text"}
                or any(not isinstance(item, str) or not item for item in topic.values())
            ):
                raise ValueError("Invalid installed guide topic")
        business = value["business"]
        if (
            not isinstance(business["kinds"], dict)
            or not business["kinds"]
            or not isinstance(business["sections"], dict)
            or any(not isinstance(kind, str) or not kind for kind in business["kinds"])
            or set(business["kinds"].values()) != set(business["sections"])
            or any(
                not isinstance(text, str) or not text
                for text in (
                    business["common"],
                    business["corrections"],
                    *business["sections"].values(),
                )
            )
        ):
            raise ValueError("Invalid installed business guides")
        return value
    except (OSError, ValueError, TypeError, KeyError, ContractError) as error:
        raise ContractError("storage_corrupt", "已安装的会计操作说明缺失或格式不符。") from error


def guide_index():
    """A small fresh directory; documentation availability is not runtime support."""
    return {
        "format": _book()["format"],
        "message": "按需读取；业务规则不代表实例已支持，先调用 accounting_schema 选择实际种类。",
        "topics": [
            {
                "topic": topic,
                "title": _book()["topics"][topic]["title"],
                "uri": "accounting://guides/" + topic,
                **(
                    {"uri_template": "accounting://guides/business/{kind}"}
                    if topic == "business"
                    else {}
                ),
            }
            for topic in TOPICS
        ],
    }


def read_guide(topic, *, kind=None):
    """Return immutable Markdown for an explicit topic or exact documented Fact kind."""
    if not isinstance(topic, str) or topic not in TOPICS:
        raise ContractError(
            "unsupported_guide", "没有此操作说明主题，请先读取 accounting://guides。"
        )
    if kind is None:
        return _book()["topics"][topic]["text"]
    if topic != "business" or not isinstance(kind, str) or kind not in _book()["business"]["kinds"]:
        raise ContractError(
            "unsupported_guide", "没有此事实种类的说明；请按实际 kind 选择，不能使用路径。"
        )
    business = _book()["business"]
    return (
        f"# 业务规则：{kind}\n\n"
        + business["common"]
        + "\n"
        + business["sections"][business["kinds"][kind]]
        + "\n"
        + business["corrections"]
    )
