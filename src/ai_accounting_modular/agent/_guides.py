"""Read only the installed, generated operating text; never workspace documents."""

from copy import deepcopy
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
            or set(value) != {"format", "topics", "business", "checklist"}
            or value["format"] != "ai-accounting-guides/1"
            or not isinstance(value["topics"], dict)
            or set(value["topics"]) != set(TOPICS)
            or not isinstance(value["business"], dict)
            or set(value["business"]) != {"kinds", "sections", "common", "corrections"}
        ):
            raise ValueError("Invalid installed guide directory")
        checklist = value["checklist"]
        if (
            not isinstance(checklist, dict)
            or set(checklist) != {"format", "common_checks", "stages"}
            or checklist["format"] != "ai-accounting-work-checklist/1"
            or not isinstance(checklist["common_checks"], list)
            or not checklist["common_checks"]
            or any(not isinstance(item, dict) for item in checklist["common_checks"])
            or not isinstance(checklist["stages"], list)
            or any(not isinstance(item, dict) for item in checklist["stages"])
            or [item.get("number") for item in checklist["stages"]] != list(range(1, 9))
        ):
            raise ValueError("Invalid installed work checklist")
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
        "message": (
            "按需读取当前阶段和专项规则；业务文档不代表实例已支持，"
            "accounting_schema 返回实际目录及所选命令合同。"
        ),
        "checklist_uri": "accounting://guides/work/checklist",
        "stage_uri_template": "accounting://guides/work/stages/{stage}",
        "corrections_uri": "accounting://guides/corrections",
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


def read_work_checklist():
    """Shared checks and stage navigation, without repeating all stage details."""
    rules = _book()["checklist"]
    return {
        "format": rules["format"],
        "common_checks": deepcopy(rules["common_checks"]),
        "stages": [
            {
                **{key: stage[key] for key in ("number", "id", "title", "completion")},
                "uri": f"accounting://guides/work/stages/{stage['number']}",
            }
            for stage in rules["stages"]
        ],
    }


def read_work_stage(stage):
    """Read one numbered stage; identifiers are not paths or company progress."""
    if not isinstance(stage, str) or stage not in tuple(map(str, range(1, 9))):
        raise ContractError("unsupported_guide", "办理阶段须为1至8，请先读取内部核对目录。")
    return deepcopy(_book()["checklist"]["stages"][int(stage) - 1])


def read_guide(topic, *, kind=None):
    """Return immutable Markdown for an explicit topic or exact documented Fact kind."""
    if topic == "corrections" and kind is None:
        return _book()["business"]["corrections"]
    if not isinstance(topic, str) or topic not in TOPICS:
        raise ContractError(
            "unsupported_guide", "没有此操作说明主题，请先读取 accounting://guides。"
        )
    if kind is None:
        if topic == "business":
            return _book()["business"]["common"] + "\n" + _book()["topics"][topic]["text"]
        return _book()["topics"][topic]["text"]
    if topic != "business" or not isinstance(kind, str) or kind not in _book()["business"]["kinds"]:
        raise ContractError(
            "unsupported_guide", "没有此事实种类的说明；请按实际 kind 选择，不能使用路径。"
        )
    business = _book()["business"]
    return f"# 业务规则：{kind}\n\n" + business["sections"][business["kinds"][kind]]
