import copy
from dataclasses import dataclass
import json
from pathlib import Path
import re
import uuid

from ..domain.models import DocumentError, Issue, Severity, failure
from ..domain.style_models import ResolvedStyle, StyleConfig
from ..resources import ResourceLocator
from ..storage.files import atomic_bytes, read_json

def merge(base, patch):
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if (key in result and isinstance(result[key], dict) and isinstance(value, dict)
                and not ("mode" in value and value.get("mode") != result[key].get("mode"))):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result

def resolve_style(*, personal: StyleConfig | None = None, overrides: dict | None = None,
                  template_id="lecture", resources=None) -> ResolvedStyle:
    if template_id != "lecture":
        raise failure("STYLE_TEMPLATE_UNKNOWN", "找不到所选内置模板。",
                      "选择通用文档模板。", "style")
    resources = resources or ResourceLocator()
    try:
        defaults = json.loads(resources.file("templates/style-defaults.json").read_text("utf-8"))
        builtin = json.loads(resources.file("templates/lecture.json").read_text("utf-8"))
    except (OSError, ValueError):
        raise failure("STYLE_RESOURCE_INVALID", "内置样式资源无法读取。",
                      "恢复随程序提供的样式资源。", "style") from None
    value = merge(defaults, builtin)
    if personal is not None:
        value = merge(value, personal.model_dump(mode="json"))
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise failure("STYLE_INVALID", "临时样式参数必须是键值配置。",
                          "检查样式参数。", "style")
        value = merge(value, overrides)
    return ResolvedStyle(StyleConfig.parse_config(value))

@dataclass(frozen=True)
class StyleSummary:
    id: str
    name: str
    builtin: bool

@dataclass(frozen=True)
class StyleListing:
    styles: tuple[StyleSummary, ...]
    issues: tuple[Issue, ...]

class PersonalStyleRepository:
    """One JSON per stable ID, never a user-controlled filename or built-in write."""
    def __init__(self, root: Path | None = None):
        self.storage_issue = None
        self.root = (root or ResourceLocator.data_root() / "styles").resolve()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.storage_issue = Issue("STYLE_STORAGE_UNAVAILABLE", Severity.WARNING, "style",
                "个人样式目录不可用，仍可使用内置模板。", "检查用户目录权限后重试。")

    def path(self, style_id):
        if not re.fullmatch(r"user\.[0-9a-f]{32}", style_id):
            raise failure("STYLE_READONLY", "内置样式只读或样式标识无效。",
                          "另存为个人样式。", "style")
        path = self.root / (style_id + ".json")
        if path.is_symlink() or path.resolve().parent != self.root:
            raise failure("STYLE_STORAGE_UNSAFE", "样式路径不属于当前样式目录。",
                          "检查样式文件。", "style")
        return path

    def load(self, style_id) -> StyleConfig:
        if style_id == "lecture":
            return resolve_style().config
        path = self.path(style_id)
        try:
            raw, _ = read_json(path, 128 * 1024)
        except (OSError, ValueError):
            raise failure("STYLE_READ_FAILED", "个人样式无法读取，原文件未修改。",
                          "重新选择样式或恢复有效备份。", "style") from None
        result = StyleConfig.parse_config(raw)
        if result.id != style_id:
            raise failure("STYLE_INVALID", "样式标识与文件不一致。",
                          "检查该个人样式文件。", "style")
        return result

    def list(self) -> StyleListing:
        styles = [StyleSummary("lecture", "通用文档", True)]
        issues = [self.storage_issue] if self.storage_issue else []
        try:
            paths = sorted(self.root.glob("user.*.json"))
        except OSError:
            return StyleListing(tuple(styles), tuple(issues) + (Issue("STYLE_LIST_FAILED", Severity.WARNING,
                "style", "无法读取个人样式列表，内置模板仍可用。", "检查目录权限。"),))
        for path in paths:
            try:
                style = self.load(path.stem)
                styles.append(StyleSummary(style.id, style.name, False))
            except DocumentError as exc:
                issues.append(exc.issue)
        return StyleListing(tuple(styles), tuple(issues))

    def save(self, config: StyleConfig, *, name: str, style_id: str | None = None) -> StyleConfig:
        identity = style_id or "user." + uuid.uuid4().hex
        path = self.path(identity)
        existed = path.exists()
        if existed:
            self.load(identity)  # Never overwrite corrupt or future-version config.
        raw = config.model_dump(mode="json")
        raw.update(id=identity, name=name)
        value = StyleConfig.parse_config(raw)
        try:
            atomic_bytes(path, (value.model_dump_json(indent=2) + "\n").encode("utf-8"), replace=existed)
        except OSError:
            raise failure("STYLE_SAVE_FAILED", "个人样式保存失败。",
                          "检查目录权限后重试。", "style") from None
        return value

    def rename(self, style_id, name):
        return self.save(self.load(style_id), name=name, style_id=style_id)

    def delete(self, style_id):
        path = self.path(style_id)
        try:
            path.unlink()
        except OSError:
            raise failure("STYLE_DELETE_FAILED", "个人样式删除失败。",
                          "检查文件权限后重试。", "style") from None

    def restore_builtin(self):
        return resolve_style()
