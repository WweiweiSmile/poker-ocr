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


class IngestHands(APIModel):
    """实时 outbox 的结构化 ingest：直接吃 HandRecord v2 数组。

    与 `ImportHands.content` 的区别：不做文本解析，保留 table_roster /
    advice.context 等结构化字段，按 external_uid 幂等。
    """

    source: Literal["live", "review"] = "live"
    records: list[dict[str, Any]] = Field(min_length=1, max_length=500)
    filename: str | None = None


class ExperimentCreate(APIModel):
    """离线 A/B 实验：冻结手牌集 + baseline/variant 两个版本。"""

    name: str = Field(min_length=1, max_length=100)
    hypothesis: str | None = None
    baselineVersionId: int
    variantVersionId: int
    scope: Literal["hands", "filter"] = "hands"
    handIds: list[int] | None = None
    filter: dict[str, Any] = Field(default_factory=dict)
    devRatio: float = Field(0.7, ge=0.1, le=0.9)
    repeat: int = Field(3, ge=1, le=5)


class ExperimentRun(APIModel):
    confirmLarge: bool = True


class DecisionMark(APIModel):
    verdict: Literal["good", "bad", "unsure"] = "unsure"
    tags: list[str] | None = None
    note: str | None = None


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
    promptStyle: Literal["lean"] = "lean"
    tableDynamicsMode: Literal["current", "text", "none"] = "current"
    tableDynamicsText: str | None = None
    contextMode: Literal["none", "snapshot", "recompute"] = "none"
    tableStatsMode: Literal["none", "snapshot", "recompute"] = "none"
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
    promptStyle: Literal["lean"] | None = None
    tableDynamicsMode: Literal["keep", "current", "text", "none"] | None = None
    tableDynamicsText: str | None = None
    contextMode: Literal["none", "snapshot", "recompute"] | None = None
    tableStatsMode: Literal["none", "snapshot", "recompute"] | None = None
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
