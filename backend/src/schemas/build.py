from typing import Literal

from pydantic import BaseModel, Field, model_validator

class NodeSpec(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    role: Literal['primary', 'replica']
    status: Literal['healthy', 'down'] = 'healthy'
    position: int = Field(ge=0, le=1000000000)
    delay_ms: int = Field(default=0, ge=0, le=3600000)


class RowSpec(BaseModel):
    id: int = Field(ge=1, le=1000000000)
    value: str = Field(min_length=1, max_length=200)


class BuildInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    table_name: str = Field(default='members', min_length=1, max_length=80)
    index_name: str = Field(default='uniq_member_email', min_length=1, max_length=80)
    key_column: str = Field(default='email', min_length=1, max_length=80)
    policy: Literal['block', 'accept'] = 'block'
    batch_size: int = Field(default=3, ge=1, le=100)
    nodes: list[NodeSpec] = Field(min_length=1, max_length=12)
    rows: list[RowSpec] = Field(default_factory=list, max_length=500)

    @model_validator(mode='after')
    def one_primary(self):
        if sum(1 for n in self.nodes if n.role == 'primary') != 1:
            raise ValueError('必须且只能配置一个主节点')
        return self


class EventInput(BaseModel):
    at_ms: int | None = Field(default=None, ge=0, le=86400000)
    kind: Literal['scan', 'write', 'delete', 'catch_up', 'finalize', 'query', 'node_down', 'node_up']
    session: str = Field(default='default', min_length=1, max_length=60)
    batch_no: int | None = Field(default=None, ge=0, le=100000)
    row_id: int | None = Field(default=None, ge=1, le=1000000000)
    value: str | None = Field(default=None, max_length=200)
    node: str | None = Field(default=None, max_length=80)
    position: int | None = Field(default=None, ge=0, le=1000000000)

    @model_validator(mode='after')
    def required_fields(self):
        if self.kind == 'scan' and self.batch_no is None:
            raise ValueError('扫描事件需要 batch_no')
        if self.kind == 'write' and (self.row_id is None or not (self.value or '').strip()):
            raise ValueError('写入事件需要 row_id 和非空 value')
        if self.kind == 'delete' and self.row_id is None:
            raise ValueError('删除事件需要 row_id')
        if self.kind == 'catch_up' and (not self.node or self.position is None):
            raise ValueError('副本追赶需要 node 和 position')
        if self.kind in ('node_down', 'node_up') and not self.node:
            raise ValueError('节点事件需要 node')
        if self.kind == 'query' and not (self.value or '').strip():
            raise ValueError('查询需要 value')
        return self


class EventBatchInput(BaseModel):
    events: list[EventInput] = Field(min_length=1, max_length=200)


class RewindInput(BaseModel):
    """Keep the first N events and replay; N=0 resets to the frozen snapshot."""
    keep: int = Field(ge=0, le=10000)
