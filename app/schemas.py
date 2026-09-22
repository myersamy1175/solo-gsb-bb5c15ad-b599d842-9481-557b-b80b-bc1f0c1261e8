"""Pydantic 请求与响应模型。"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

Amount = Annotated[float, Field(gt=0, description="必须为正数的质量/体积")]


# ---------------------------------------------------------------- 操作请求

class SamplingOp(BaseModel):
    """取样:从外部环境得到一个全新的根样品。"""

    type: Literal["sampling"]
    op_no: str = Field(min_length=1, description="全局唯一操作号(幂等键)")
    sample_id: str = Field(min_length=1)
    material: str = Field(min_length=1, description="物料/品类")
    amount: Amount
    unit: str = Field(min_length=1)
    location: Optional[str] = Field(default=None, description="操作/存放位置,用于冲突定位")


class AliquotPart(BaseModel):
    sample_id: str = Field(min_length=1)
    amount: Amount


class AliquotOp(BaseModel):
    """分装:把同一个父样品拆成多个新子样品。"""

    type: Literal["aliquot"]
    op_no: str = Field(min_length=1)
    parent_id: str = Field(min_length=1)
    outputs: list[AliquotPart] = Field(min_length=1)
    unit: Optional[str] = Field(default=None, description="指定单位,须与父样品一致;默认沿用父样品")
    location: Optional[str] = None

    @model_validator(mode="after")
    def _check_outputs(self) -> "AliquotOp":
        ids = [o.sample_id for o in self.outputs]
        if len(ids) != len(set(ids)):
            raise ValueError("outputs 中存在重复 sample_id")
        return self


class MergeSource(BaseModel):
    sample_id: str = Field(min_length=1)
    amount: Amount


class MergeOp(BaseModel):
    """合并:把多个来源样品(可部分取用)汇到输出样品;输出可为已有样品。"""

    type: Literal["merge"]
    op_no: str = Field(min_length=1)
    inputs: list[MergeSource] = Field(min_length=1)
    output_id: str = Field(min_length=1)
    unit: Optional[str] = Field(default=None, description="指定单位,须与各输入一致;默认沿用输入单位")
    material: Optional[str] = Field(default=None, description="新建输出样品时必填")
    expected_output_amount: Optional[Amount] = Field(
        default=None, description="可选:守恒校验,期望输出总量"
    )
    location: Optional[str] = None

    @model_validator(mode="after")
    def _check_inputs(self) -> "MergeOp":
        ids = [i.sample_id for i in self.inputs]
        if len(ids) != len(set(ids)):
            raise ValueError("inputs 中存在重复 sample_id")
        return self


AnyOp = Annotated[
    Union[SamplingOp, AliquotOp, MergeOp],
    Field(discriminator="type"),
]


class BatchRequest(BaseModel):
    batch_id: Optional[str] = Field(default=None, description="批次标识,仅用于审计展示")
    operations: list[AnyOp] = Field(min_length=1)


# ---------------------------------------------------------------- 冲突与结果

class Conflict(BaseModel):
    code: str = Field(description="冲突类型,如 insufficient_balance / cycle_detected")
    message: str
    op_no: Optional[str] = None
    batch_index: Optional[int] = Field(default=None, description="批量提交中从 0 开始的下标")
    sample_id: Optional[str] = None
    location: Optional[str] = None
    requested: Optional[float] = None
    available: Optional[float] = None
    cycle_path: Optional[list[str]] = Field(default=None, description="检测到的环路节点路径")


class EdgeOut(BaseModel):
    parent_id: Optional[str]
    child_id: str
    op_no: str
    amount: float
    unit: str


class OpResult(BaseModel):
    op_no: str
    type: Literal["sampling", "aliquot", "merge"]
    samples_created: list[str]
    edges: list[EdgeOut]
    remaining: dict[str, float] = Field(description="操作涉及样品的最新余量")
    replayed: bool = Field(default=False, description="True 表示命中幂等,返回的是原结果")


class BatchResponse(BaseModel):
    batch_id: Optional[str]
    status: Literal["committed", "replayed", "mixed"] = Field(
        description="committed=全部新提交;replayed=全部重复操作号回放;mixed=两者混合"
    )
    results: list[OpResult]


# ---------------------------------------------------------------- 谱系回溯

class LineageNode(BaseModel):
    sample_id: str
    material: str
    unit: str
    remaining: float
    location: Optional[str] = None
    depth: int = Field(description="相对目标样品的跳数,目标为 0")
    is_root: bool


class LineageEdge(BaseModel):
    parent_id: Optional[str]
    child_id: str
    op_no: str
    amount: float
    unit: str


class TraceOp(BaseModel):
    op_no: str
    type: str
    created_at: str
    location: Optional[str] = None


class TraceResponse(BaseModel):
    sample_id: str
    direction: Literal["upstream", "downstream", "both"]
    nodes: list[LineageNode]
    edges: list[LineageEdge]
    operations: list[TraceOp]
    roots: list[str] = Field(description="完整来源:取样根样品(向上)")
    leaves: list[str] = Field(description="完整去向:当前无下游的后代样品(向下)")


# ---------------------------------------------------------------- 余量校验

class SampleBalance(BaseModel):
    sample_id: str
    unit: str
    stored_remaining: float
    ledger_remaining: float
    consistent: bool
    total_in: float
    total_out: float
    available: float


class CheckResponse(BaseModel):
    sample_id: Optional[str] = None
    consistent: bool
    tolerance: float
    samples: list[SampleBalance]
    conflicts: list[Conflict] = Field(default_factory=list)


# ---------------------------------------------------------------- 预留

class ReservationItemIn(BaseModel):
    """预留明细:为指定样品锁定 amount,单位沿用样品主数据。"""

    sample_id: str = Field(min_length=1)
    amount: Amount


class ReservationRequest(BaseModel):
    """创建预留单:同一预留号对多个样品分别锁定数量。"""

    reservation_no: str = Field(min_length=1, description="预留号(幂等键),全局唯一")
    items: list[ReservationItemIn] = Field(min_length=1)
    note: Optional[str] = Field(default=None, description="备注,仅用于审计展示")

    @model_validator(mode="after")
    def _check_items(self) -> "ReservationRequest":
        ids = [i.sample_id for i in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("items 中存在重复 sample_id")
        return self


class ReservationItemOut(BaseModel):
    sample_id: str
    amount: float
    unit: str
    remaining: float = Field(description="样品当前余量(冗余余量)")
    reserved: float = Field(description="该样品未释放预留的锁定总量(含本单)")
    available: float = Field(description="可用量 = 当前余量 - 未释放预留")


class ReservationResult(BaseModel):
    reservation_no: str
    status: Literal["active", "released"]
    items: list[ReservationItemOut]
    note: Optional[str] = None
    replayed: bool = Field(
        default=False, description="True 表示命中预留号幂等,返回首次结果"
    )
    created_at: Optional[str] = None
    released_at: Optional[str] = None


class ReleaseResult(BaseModel):
    reservation_no: str
    status: Literal["released"]
    items: list[ReservationItemOut] = Field(
        description="释放后各样品的余量/已预留/可用量"
    )
    note: Optional[str] = None
    replayed: bool = Field(
        default=False, description="对已释放预留号再次释放时为 True"
    )
    created_at: Optional[str] = None
    released_at: Optional[str] = None


class SampleAvailability(BaseModel):
    sample_id: str
    unit: str
    remaining: float = Field(description="当前余量")
    reserved: float = Field(description="未释放预留的锁定总量")
    available: float = Field(description="可用量 = 当前余量 - 已预留量")


class AvailabilityResponse(BaseModel):
    samples: list[SampleAvailability]
