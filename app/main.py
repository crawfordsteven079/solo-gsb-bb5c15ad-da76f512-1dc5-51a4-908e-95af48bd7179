"""FastAPI 接口：池状态/历史版本查询、两版本地址用途比较、预演与原子提交、部分回收、整批迁移、保留区替换与已分配地址的历史快照恢复。"""
from __future__ import annotations

import ipaddress
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from .allocator import CidrAllocationSolver, InfeasibleError
from .config import settings
from .db import (
    ConflictError,
    Database,
    PoolState,
    RelocateInputError,
    ReservationInputError,
    SnapshotNotFoundError,
)

_db: Database | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 容器启动即完成建表与初始地址池写入（幂等）。
    get_db()
    yield


app = FastAPI(
    title="内网 CIDR 批量分配 API",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "在“父网段 + 保留子网”的地址池内批量预演并原子提交对齐子网分配。"
        "优化目标：先最小化分配后剩余空闲 CIDR 数量，再取按请求顺序的网络"
        "地址序列字典序最小者。"
    ),
)


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(settings)
    return _db


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------

class PreviewRequest(BaseModel):
    prefix_lengths: list[int] = Field(
        ...,
        min_length=1,
        max_length=8,
        description="按请求顺序排列的前缀长度，1～8 个。",
    )

    @field_validator("prefix_lengths")
    @classmethod
    def _validate_prefixes(cls, values: list[int]) -> list[int]:
        for p in values:
            if isinstance(p, bool) or not isinstance(p, int) or p < 0 or p > 128:
                raise ValueError("prefix length must be an integer in [0, 128]")
        return values


class CommitRequest(BaseModel):
    pool_version: int = Field(..., ge=1, description="预演返回的池版本号。")
    allocations: list[str] = Field(
        ...,
        min_length=1,
        max_length=8,
        description="预演返回的方案 CIDR，顺序与请求一致。",
    )

    @field_validator("allocations")
    @classmethod
    def _validate_cidrs(cls, values: list[str]) -> list[str]:
        # strict=True：主机位非零的非对齐写法直接报错。
        parsed: list[ipaddress._BaseNetwork] = [
            ipaddress.ip_network(raw, strict=True) for raw in values
        ]
        versions = {n.version for n in parsed}
        if len(versions) > 1:
            raise ValueError("allocations must all be IPv4 or all be IPv6")
        ordered = sorted(parsed, key=lambda n: int(n.network_address))
        for a, b in zip(ordered, ordered[1:]):
            if a.overlaps(b):
                raise ValueError("allocations contain overlapping CIDRs")
        return [str(n) for n in parsed]


class PoolInfo(BaseModel):
    parent_cidr: str
    version: int
    reserved_cidrs: list[str]
    allocated_cidrs: list[str]
    free_cidrs: list[str]
    free_cidr_count: int


class PreviewResponse(BaseModel):
    pool_version: int
    feasible: bool
    allocations: list[str]
    free_cidr_count_after: int
    free_cidrs_after: list[str]
    reason: str | None = None


class CommitResponse(BaseModel):
    status: Literal["committed"]
    pool_version: int
    allocations: list[str]
    free_cidrs: list[str]


class ReclaimRequest(BaseModel):
    pool_version: int = Field(..., ge=1, description="当前池版本号。")
    cidr: str = Field(
        ...,
        description=(
            "要回收的规范 CIDR，必须完全由当前已分配地址覆盖，"
            "可跨越相邻的多条分配记录。"
        ),
    )

    @field_validator("cidr")
    @classmethod
    def _validate_cidr(cls, raw: str) -> str:
        # strict=True：主机位非零的非对齐写法直接 422，不静默改写成网络地址。
        return str(ipaddress.ip_network(raw, strict=True))


class ReclaimResponse(BaseModel):
    status: Literal["reclaimed"]
    pool_version: int
    reclaimed_cidr: str
    allocated_cidrs: list[str]
    free_cidrs: list[str]


class RelocateItem(BaseModel):
    from_cidr: str = Field(
        ...,
        description=(
            "迁移来源规范 CIDR，必须完全由当前已分配地址覆盖，"
            "可跨越相邻的多条分配记录。"
        ),
    )
    to_cidr: str = Field(
        ...,
        description=(
            "迁移目标规范 CIDR，与来源地址族、前缀长度相同；必须落在"
            "父网段内，可使用本批来源释放的地址，但不得触及保留区或"
            "未迁移的已分配地址。"
        ),
    )

    @field_validator("from_cidr", "to_cidr")
    @classmethod
    def _validate_cidr(cls, raw: str) -> str:
        # strict=True：主机位非零的非对齐写法直接 422，不静默改写成网络地址。
        return str(ipaddress.ip_network(raw, strict=True))


class RelocateRequest(BaseModel):
    pool_version: int = Field(..., ge=1, description="当前池版本号。")
    moves: list[RelocateItem] = Field(
        ...,
        min_length=1,
        max_length=8,
        description="整批同时迁移的 1～8 组 from_cidr/to_cidr。",
    )

    @field_validator("moves")
    @classmethod
    def _validate_moves(
        cls, items: list[RelocateItem]
    ) -> list[RelocateItem]:
        parsed = [
            (
                ipaddress.ip_network(item.from_cidr, strict=True),
                ipaddress.ip_network(item.to_cidr, strict=True),
            )
            for item in items
        ]
        # 同组两端：地址族与前缀长度必须一致。
        for src, dst in parsed:
            if src.version != dst.version or src.prefixlen != dst.prefixlen:
                raise ValueError(
                    "from_cidr and to_cidr must share the same address "
                    "family and prefix length"
                )
        # 各来源之间、各目标之间不得重叠（来源与来源允许相邻；目标
        # 之间同样允许相邻——重写时会合并）。
        for label, nets in (
            ("from_cidr", [src for src, _ in parsed]),
            ("to_cidr", [dst for _, dst in parsed]),
        ):
            ordered = sorted(nets, key=lambda n: int(n.network_address))
            for a, b in zip(ordered, ordered[1:]):
                if a.overlaps(b):
                    raise ValueError(f"{label} values contain overlapping CIDRs")
        return items


class RelocateResponse(BaseModel):
    status: Literal["relocated"]
    pool_version: int
    moves: list[RelocateItem]
    allocated_cidrs: list[str]
    free_cidrs: list[str]


class ReplaceReservationsRequest(BaseModel):
    pool_version: int = Field(..., ge=1, description="当前池版本号。")
    desired_cidrs: list[str] = Field(
        ...,
        description=(
            "保留地址的完整目标集合（而非增量）：规范 CIDR 列表，允许空列表"
            "（清空全部保留地址）、相互重叠或相邻；落库时规范化为覆盖范围"
            "相同且数量最少的 CIDR。"
        ),
    )

    @field_validator("desired_cidrs")
    @classmethod
    def _validate_cidrs(cls, values: list[str]) -> list[str]:
        # strict=True：主机位非零的非对齐写法直接 422，不静默改写成网络地址。
        return [str(ipaddress.ip_network(raw, strict=True)) for raw in values]


class ReplaceReservationsResponse(BaseModel):
    status: Literal["replaced"]
    pool_version: int
    reserved_cidrs: list[str]
    free_cidrs: list[str]


class RestoreRequest(BaseModel):
    pool_version: int = Field(
        ...,
        ge=1,
        description="当前池版本号（乐观并发控制）。",
    )
    snapshot_version: int = Field(
        ...,
        ge=1,
        description="要恢复到的已保存历史版本号；其快照必须真实存在。",
    )


class RestoreResponse(BaseModel):
    status: Literal["restored"]
    pool_version: int
    restored_from_version: int
    allocated_cidrs: list[str]
    free_cidrs: list[str]


class VersionDiffResponse(BaseModel):
    from_version: int = Field(..., description="比较起点版本号（旧快照）。")
    to_version: int = Field(..., description="比较终点版本号（新快照）。")
    reserved_added: list[str] = Field(
        ...,
        description=(
            "保留地址新增：终点版本有、起点版本没有的地址，按地址集合"
            "求差后折叠为数量最少的规范 CIDR，按 (网络地址, 前缀长度) 排序。"
        ),
    )
    reserved_removed: list[str] = Field(
        ..., description="保留地址移出：起点版本有、终点版本没有的地址。"
    )
    allocated_added: list[str] = Field(
        ..., description="已分配地址新增：终点版本有、起点版本没有的地址。"
    )
    allocated_removed: list[str] = Field(
        ..., description="已分配地址移出：起点版本有、终点版本没有的地址。"
    )


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _free_cidrs(state: PoolState) -> tuple[list[ipaddress._BaseNetwork], int]:
    reserved_all = list(state.reserved) + list(state.allocated)
    solver = CidrAllocationSolver(state.parent, reserved_all, ())
    return solver.free_cidrs, len(solver.free_cidrs)


def _pool_info(state: PoolState) -> PoolInfo:
    free, count = _free_cidrs(state)
    return PoolInfo(
        parent_cidr=str(state.parent),
        version=state.version,
        reserved_cidrs=[str(n) for n in state.reserved],
        allocated_cidrs=[str(n) for n in state.allocated],
        free_cidrs=[str(n) for n in free],
        free_cidr_count=count,
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/pool", response_model=PoolInfo, tags=["pool"])
def get_pool(db: Database = Depends(get_db)) -> PoolInfo:
    """返回当前地址池配置、版本与剩余空闲 CIDR。"""
    return _pool_info(db.get_state())


@app.get(
    "/api/pool/versions/{version}",
    response_model=PoolInfo,
    tags=["pool"],
)
def get_pool_version(
    version: int, db: Database = Depends(get_db)
) -> PoolInfo:
    """查询**历史版本**的池状态快照。

    返回该版本保存时的父网段、保留与已分配 CIDR，以及依据该快照计算
    的空闲 CIDR。快照只追加、从不更新：旧版本的状态不随后续写入变化。
    数据库首次升级时会给当前池版本补存基线快照，此后每次成功的提交 /
    回收 / 迁移 / 保留区替换 / 误操作恢复都会在各自写事务内生成新版本
    快照；失败的操作不生成版本。未知版本（含基线之前不存在的版本号）
    返回 404。
    """
    state = db.get_state_version(version)
    if state is None:
        raise HTTPException(
            status_code=404, detail=f"unknown pool version: {version}"
        )
    return _pool_info(state)


@app.get(
    "/api/pool/versions/{from_version}/diff/{to_version}",
    response_model=VersionDiffResponse,
    tags=["pool"],
)
def diff_pool_versions(
    from_version: int, to_version: int, db: Database = Depends(get_db)
) -> VersionDiffResponse:
    """比较两个**已存历史版本**快照的地址用途变化（方向 from -> to）。

    返回保留、已分配地址各自的新增（to 有 from 无）与移出（from 有 to
    无）四个 CIDR 列表。比较基于两份**不可变快照**的**地址集合**而非
    记录边界：同一地址集在一个版本记作一条 /24、另一版本记作两条相邻
    /25（记录拆分）或反之（记录合并）时，两侧地址集合相等，四个列表均
    不含该地址，不产生伪变化。每个差集都折叠为覆盖该地址集合且数量
    最少的规范 CIDR，并按 (网络地址, 前缀长度) 升序返回。

    数据全部取自版本快照表（只读），不读取也不改写当前地址池；两个版本
    相同（含 from == to）时四个列表均为空。任一版本不存在（基线之前的
    版本、尚未产生的未来版本）返回 404，绝不以当前池状态代替旧快照。
    """
    diff = db.get_version_diff(from_version, to_version)
    if diff is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"unknown pool version in diff request: "
                f"from={from_version}, to={to_version}"
            ),
        )
    return VersionDiffResponse(
        from_version=from_version,
        to_version=to_version,
        reserved_added=[str(n) for n in diff.reserved_added],
        reserved_removed=[str(n) for n in diff.reserved_removed],
        allocated_added=[str(n) for n in diff.allocated_added],
        allocated_removed=[str(n) for n in diff.allocated_removed],
    )


@app.post(
    "/api/pool/reservations/replace",
    response_model=ReplaceReservationsResponse,
    tags=["pool"],
)
def replace_reservations(
    body: ReplaceReservationsRequest, db: Database = Depends(get_db)
) -> ReplaceReservationsResponse:
    """整体替换保留地址：以 desired_cidrs 的覆盖范围为准重写保留记录。

    允许空列表与重叠/相邻网段，落库时规范化为覆盖范围相同且数量最少的
    规范 CIDR。版本核对、与已分配地址的冲突复核、保留记录替换与版本
    自增在同一 SQLite 写事务内完成；任何一步失败即整体回滚，池状态与
    版本均不变。成功后池状态查询与后续预演/提交立即采用新保留区。
    """
    networks = tuple(
        ipaddress.ip_network(c, strict=True) for c in body.desired_cidrs
    )
    try:
        state = db.replace_reservations(body.pool_version, networks)
    except ReservationInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    free, _ = _free_cidrs(state)
    return ReplaceReservationsResponse(
        status="replaced",
        pool_version=state.version,
        reserved_cidrs=[str(n) for n in state.reserved],
        free_cidrs=[str(n) for n in free],
    )


@app.post("/api/allocations/preview", response_model=PreviewResponse, tags=["allocations"])
def preview(
    body: PreviewRequest, db: Database = Depends(get_db)
) -> PreviewResponse:
    """预演：不修改任何状态，返回池版本与最优可行方案。"""
    state = db.get_state()
    occupied = list(state.reserved) + list(state.allocated)
    try:
        plan = CidrAllocationSolver(
            state.parent, occupied, tuple(body.prefix_lengths)
        ).solve()
    except InfeasibleError as exc:
        return PreviewResponse(
            pool_version=state.version,
            feasible=False,
            allocations=[],
            free_cidr_count_after=0,
            free_cidrs_after=[],
            reason=str(exc),
        )
    return PreviewResponse(
        pool_version=state.version,
        feasible=True,
        allocations=[str(n) for n in plan.networks],
        free_cidr_count_after=len(plan.free_cidrs_after),
        free_cidrs_after=[str(n) for n in plan.free_cidrs_after],
    )


@app.post(
    "/api/allocations/commit",
    response_model=CommitResponse,
    tags=["allocations"],
)
def commit(
    body: CommitRequest, db: Database = Depends(get_db)
) -> CommitResponse:
    """提交：仅当版本未变化且方案仍可用时原子落库，否则 409 冲突。"""
    networks = tuple(
        ipaddress.ip_network(c, strict=True) for c in body.allocations
    )
    try:
        state = db.commit_plan(body.pool_version, networks)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    free, _ = _free_cidrs(state)
    return CommitResponse(
        status="committed",
        pool_version=state.version,
        allocations=[str(n) for n in networks],
        free_cidrs=[str(n) for n in free],
    )


@app.post(
    "/api/allocations/reclaim",
    response_model=ReclaimResponse,
    tags=["allocations"],
)
def reclaim(
    body: ReclaimRequest, db: Database = Depends(get_db)
) -> ReclaimResponse:
    """部分回收：目标 CIDR 必须完全被已分配地址覆盖，否则 409 且不改池。

    在单个 SQLite 写事务内扣除目标地址、把仍被占用的部分重写为最少的
    规范 CIDR、自增池版本；回收出的空间立即体现在池状态查询与后续预演中。
    """
    target = ipaddress.ip_network(body.cidr, strict=True)
    try:
        state, allocated_after = db.reclaim_allocated(
            body.pool_version, target
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    free, _ = _free_cidrs(state)
    return ReclaimResponse(
        status="reclaimed",
        pool_version=state.version,
        reclaimed_cidr=str(target),
        allocated_cidrs=[str(n) for n in allocated_after],
        free_cidrs=[str(n) for n in free],
    )


@app.post(
    "/api/allocations/relocate",
    response_model=RelocateResponse,
    tags=["allocations"],
)
def relocate(
    body: RelocateRequest, db: Database = Depends(get_db)
) -> RelocateResponse:
    """整批同时迁移：把 1～8 段已分配地址从 from_cidr 搬到 to_cidr。

    每组两端须为规范 CIDR、地址族与前缀长度相同；各来源之间、各目标
    之间不得重叠。每个来源必须被当前已分配地址完整覆盖（可跨越相邻
    多条记录，也可只覆盖一条记录的一部分）；目标必须在父网段内，可
    使用本批来源释放的地址（允许来源与目标相同），但不得触及保留区
    或未迁移的已分配地址。

    按**整批同时迁移**的结果（当前已分配地址减去全部来源，再并入
    全部目标），把已分配地址全局合并并重写为最少的规范 CIDR。版本
    检查、覆盖与冲突复核、重写及版本自增在单个 SQLite 写事务内完成，
    任何一步失败即整体回滚，返回 409 且池与版本不变。
    """
    moves = tuple(
        (
            ipaddress.ip_network(item.from_cidr, strict=True),
            ipaddress.ip_network(item.to_cidr, strict=True),
        )
        for item in body.moves
    )
    try:
        state = db.relocate_allocations(body.pool_version, moves)
    except RelocateInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    free, _ = _free_cidrs(state)
    return RelocateResponse(
        status="relocated",
        pool_version=state.version,
        moves=body.moves,
        allocated_cidrs=[str(n) for n in state.allocated],
        free_cidrs=[str(n) for n in free],
    )


@app.post(
    "/api/allocations/restore",
    response_model=RestoreResponse,
    tags=["allocations"],
)
def restore_allocations(
    body: RestoreRequest, db: Database = Depends(get_db)
) -> RestoreResponse:
    """误操作回滚：把已分配地址恢复为某个历史版本快照的地址集合。

    仅恢复**已分配地址**，当前保留区保持原样（历史快照中的保留地址
    不参与恢复）。在同一 SQLite 写事务内：核对当前池版本、检查历史
    快照是否存在、复核历史已分配地址是否与当前保留区相交，任一不满足
    即整体回滚——版本过期或地址相交返回 `409 Conflict`，池与版本均
    不变；历史版本不存在返回 `404 Not Found`，绝不以当前状态代替。

    复核通过后把快照已分配地址集合规范化为数量最少的规范 CIDR，整体
    重写已分配记录并自增版本、为新版本追加不可变快照（被引用的历史
    快照本身不变）。成功后查询、预演、提交及其它写入立即看到恢复后的
    状态。
    """
    try:
        state = db.restore_allocated(
            body.pool_version, body.snapshot_version
        )
    except SnapshotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    free, _ = _free_cidrs(state)
    return RestoreResponse(
        status="restored",
        pool_version=state.version,
        restored_from_version=body.snapshot_version,
        allocated_cidrs=[str(n) for n in state.allocated],
        free_cidrs=[str(n) for n in free],
    )
