"""请求体模型（Pydantic）。字段名直接用 camelCase，和 API/前端一致。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class APIModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class MarkPatch(APIModel):
    favorite: bool | None = None
    tags: list[str] | None = None
    status: Literal["todo", "confirmed", "disputed"] | None = None
    note: str | None = None


class ImportHands(APIModel):
    """复盘手牌 / 实时日志手牌的导入请求。

    `content` 是文件原文或粘贴的 JSON/JSONL；`format=auto` 时按记录形状识别。
    先 `dryRun=true` 预览，再 `dryRun=false` 落库。
    """

    format: Literal["auto", "review", "live"] = "auto"
    content: str = Field(min_length=1)
    filename: str | None = None
    dryRun: bool = False
    dedupe: bool = True
    onlyExact: bool = True
    allowInvalid: bool = False
    includePartial: bool = False
    includeMidHand: bool = False


class HandDelete(APIModel):
    handIds: list[int] = Field(min_length=1, max_length=1000)


class VersionCreate(APIModel):
    name: str | None = None
    provider: str = "deepseek"
    baseUrl: str = ""
    model: str = ""
    apiKeyRef: str = "AI_SERVER_API_KEY"
    temperature: float | None = None
    topP: float | None = None
    maxTokens: int | None = None
    thinking: Literal["enabled", "disabled"] | None = None
    reasoningEffort: Literal["none", "low", "high", "max"] | None = None
    promptStyle: Literal["lean", "skills"] = "lean"
    tableDynamicsMode: Literal["current", "text", "none"] = "current"
    tableDynamicsText: str | None = None
    notes: str | None = None


class VersionClone(APIModel):
    name: str | None = None
    provider: str | None = None
    baseUrl: str | None = None
    model: str | None = None
    apiKeyRef: str | None = None
    temperature: float | None = None
    topP: float | None = None
    maxTokens: int | None = None
    thinking: Literal["enabled", "disabled"] | None = None
    reasoningEffort: Literal["none", "low", "high", "max"] | None = None
    promptStyle: Literal["lean", "skills"] | None = None
    tableDynamicsMode: Literal["keep", "current", "text", "none"] | None = None
    tableDynamicsText: str | None = None
    notes: str | None = None


class VersionMetaPatch(APIModel):
    name: str | None = None
    notes: str | None = None
    archived: bool | None = None


class RunCreate(APIModel):
    versionIds: list[int] = Field(default_factory=list)
    scope: Literal["hands", "favorites", "filter"] = "hands"
    filter: dict[str, Any] = Field(default_factory=dict)
    handIds: list[int] | None = None
    repeat: int = Field(1, ge=1, le=5)
    dedupe: bool = True
    includeQuality: list[Literal["exact", "approx"]] = Field(
        default_factory=lambda: ["exact", "approx"])
    note: str | None = None
    confirmLarge: bool = False
    parentJobId: int | None = None


class RunPreview(RunCreate):
    pass
