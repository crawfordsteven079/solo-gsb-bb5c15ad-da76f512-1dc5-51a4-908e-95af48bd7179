"""保留区调整（同步腾挪已分配地址）接口的端到端测试。

直接运行：``python3 -m tests.test_reservations_adjust``（或 pytest 收集）。

造数：父网段 10.10.0.0/16，初始保留 10.10.0.0/20 + 10.10.255.0/24；
通过 DB 层直接提交两段已分配（避开预演的碎片最优选择）：

    10.10.16.0/28   （后续迁移的来源）
    10.10.16.32/28  （不参与迁移的已分配地址）

核心场景（分步无法完成、组合一次成功）：
* 目标保留 = 旧保留 − 10.10.0.16/28 + 10.10.16.0/28；
* 迁移：10.10.16.0/28（旧已分配，将被保留）→
  10.10.0.16/28（旧保留区内本次解除的地址）。
  先替换保留区会撞上来源占用（409），先迁移则目标落在旧保留区（409）；
  只有同一时刻计算的组合操作可以完成。

覆盖：
* 分步语义：替换/迁移各自 409，组合 200；
* 成功后两类地址折叠为最少规范 CIDR（两个相邻 /24 折成一个 /23），
  版本自增、快照与新状态一致、来源立即可用、GET /api/pool 立即反映；
* 非法输入 422：moves 为空或超过 8 组、CIDR 无法解析/非规范、同族
  等长约束不符（跨地址族、前缀长度不一致、组间混用族）、来源/目标
  重叠、越出父网段；
* 冲突 409：版本过期、来源未被已分配完整覆盖、目标占用未迁移的已
  分配地址、新保留压住未迁移的已分配地址、新保留压住迁移目标（最终
  保留与已分配相交）；
* 409 不改动地址池（版本、保留/已分配集、空闲 CIDR 全部不变）；
* 目标保留允许空、重叠与相邻 CIDR；
* IPv6 池的调整（直接走 Database 层）；
* 同版本并发调整：写锁互斥，恰有一个成功。
"""
import ipaddress
import os
import tempfile
import threading

os.environ["PARENT_CIDR"] = "10.10.0.0/16"
os.environ["RESERVED_CIDRS"] = "10.10.0.0/20,10.10.255.0/24"
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_adjust.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import (  # noqa: E402
    AdjustInputError,
    ConflictError,
    Database,
)
from app.main import app  # noqa: E402

client = TestClient(app)

ADJUST_PATH = "/api/pool/reservations/adjust"
REPLACE_PATH = "/api/pool/reservations/replace"
RELOCATE_PATH = "/api/allocations/relocate"


def _seed_allocated(cidr, version):
    """直接走 DB 层提交确定性的占用地址（仅用于造数）。"""
    from app.main import get_db

    get_db().commit_plan(
        version, (ipaddress.ip_network(cidr, strict=True),)
    )


def _pool():
    return client.get("/api/pool").json()


def _adjust(version, desired, moves):
    return client.post(
        ADJUST_PATH,
        json={"pool_version": version, "desired_cidrs": desired, "moves": moves},
    )


def _cidr_num_key(cidr: str) -> int:
    return int(ipaddress.ip_network(cidr, strict=False).network_address)


def main():
    initial = _pool()
    assert initial["version"] == 1
    assert initial["reserved_cidrs"] == [
        "10.10.0.0/20",
        "10.10.255.0/24",
    ]

    # 造数：在首个空闲块 10.10.16.0/20 起点铺两段 /28（直接走 DB 层，
    # 避开预演优化器对碎片数的全局择优）。
    _seed_allocated("10.10.16.0/28", 1)
    _seed_allocated("10.10.16.32/28", 2)
    # v3：保留=0.0/20 + 255.0/24；已分配=16.0/28 + 16.32/28。
    assert _pool()["version"] == 3

    # 新保留 = 旧保留解除 0.16/28、纳入 16.0/28。0.0/24 解除 0.16/28
    # 后剩余部分用 0.0/28 + 0.32/27 + 0.64/26 + 0.128/25 表示（不能
    # 再给 0.0/24——它会把目标重新纳入保留）；1.0/24 与其余覆盖段保留。
    desired = [
        "10.10.0.0/28",
        "10.10.0.32/27",
        "10.10.0.64/26",
        "10.10.0.128/25",
        "10.10.1.0/24",
        "10.10.2.0/27",
        "10.10.2.32/27",
        "10.10.2.64/26",
        "10.10.2.128/25",
        "10.10.3.0/24",
        "10.10.4.0/22",
        "10.10.8.0/21",
        "10.10.16.0/28",
        "10.10.255.0/24",
    ]
    move = {"from_cidr": "10.10.16.0/28", "to_cidr": "10.10.0.16/28"}

    # =====================================================================
    # 一、分步操作确实无法完成：先替换 409、先迁移 409
    # =====================================================================
    r = client.post(
        REPLACE_PATH,
        json={"pool_version": 3, "desired_cidrs": desired},
    )
    assert r.status_code == 409, r.text
    assert "allocated" in r.json()["detail"]

    r = client.post(
        RELOCATE_PATH,
        json={"pool_version": 3, "moves": [move]},
    )
    assert r.status_code == 409, r.text
    assert "reserved" in r.json()["detail"]

    assert _pool()["version"] == 3
    print("stepwise replace/relocate each conflict (409) ok")

    # =====================================================================
    # 二、组合操作一次成功：地址折叠、版本自增、快照一致
    # =====================================================================
    r = _adjust(3, desired, [move])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "adjusted"
    assert body["pool_version"] == 4
    assert body["moves"] == [move]
    # 保留：旧 /20 解除 0.16/28 后，剩余连续段 0.32..15.255（起点
    # 仅对齐 /27）贪心最少切分为 0.32/27+0.64/26+0.128/25+
    # 1.0/24+2.0/23+4.0/22+8.0/21，加上 0.0/28。
    assert sorted(body["reserved_cidrs"], key=_cidr_num_key) == [
        "10.10.0.0/28",
        "10.10.0.32/27",
        "10.10.0.64/26",
        "10.10.0.128/25",
        "10.10.1.0/24",
        "10.10.2.0/23",
        "10.10.4.0/22",
        "10.10.8.0/21",
        "10.10.16.0/28",
        "10.10.255.0/24",
    ], body["reserved_cidrs"]
    # 已分配：来源被扣除、目标落在旧保留区；未迁移的 16.32/28 保留。
    assert sorted(body["allocated_cidrs"], key=_cidr_num_key) == [
        "10.10.0.16/28",
        "10.10.16.32/28",
    ], body["allocated_cidrs"]

    pool4 = _pool()
    assert pool4["version"] == 4
    assert pool4["reserved_cidrs"] == body["reserved_cidrs"]
    assert pool4["allocated_cidrs"] == body["allocated_cidrs"]
    assert pool4["free_cidrs"] == body["free_cidrs"]
    # 保留与已分配不相交。
    res_nets = [ipaddress.ip_network(c) for c in pool4["reserved_cidrs"]]
    for cidr in pool4["allocated_cidrs"]:
        net = ipaddress.ip_network(cidr)
        assert not any(net.overlaps(r) for r in res_nets)
    # 释放出的来源 10.10.16.16/28 立即变空闲（16.0/28 被保留，中间
    # 的 16.16/28 是来源释放、未被新保留覆盖的地址）。
    freed = ipaddress.ip_network("10.10.16.16/28")
    assert any(
        freed.subnet_of(ipaddress.ip_network(f)) for f in pool4["free_cidrs"]
    )
    # 历史快照：v4 与当前一致，v3 仍是旧状态（不可变）。
    snap4 = client.get("/api/pool/versions/4").json()
    assert snap4["reserved_cidrs"] == body["reserved_cidrs"]
    assert snap4["allocated_cidrs"] == body["allocated_cidrs"]
    snap3 = client.get("/api/pool/versions/3").json()
    assert snap3["reserved_cidrs"] == ["10.10.0.0/20", "10.10.255.0/24"]
    assert snap3["allocated_cidrs"] == ["10.10.16.0/28", "10.10.16.32/28"]
    print("combined adjust succeeds, folds CIDRs, snapshots ok")

    # =====================================================================
    # 三、非法输入 -> 422（不触库）
    # =====================================================================
    # 用一个结构合法的调整作为各坏载荷的参照（把 16.32/28 迁到空闲处
    # 并清空保留区之外的新增；这里直接构造针对 v4 的各种坏输入）。
    bad_payloads = [
        # moves 为空 / 超过 8 组
        {"pool_version": 4, "desired_cidrs": [], "moves": []},
        {
            "pool_version": 4,
            "desired_cidrs": [],
            "moves": [
                {"from_cidr": "10.10.16.32/28", "to_cidr": f"10.10.32.{i}/28"}
                for i in range(0, 36, 4)
            ][:9],
        },
        # 缺字段
        {"pool_version": 4, "desired_cidrs": []},
        # CIDR 无法解析 / 非规范（主机位非零）
        {"pool_version": 4, "desired_cidrs": [],
         "moves": [{"from_cidr": "nope", "to_cidr": "10.10.32.0/28"}]},
        {"pool_version": 4, "desired_cidrs": [],
         "moves": [{"from_cidr": "10.10.16.33/28", "to_cidr": "10.10.32.0/28"}]},
        {"pool_version": 4, "desired_cidrs": [],
         "moves": [{"from_cidr": "10.10.16.32/28", "to_cidr": "10.10.32.1/28"}]},
        # desired 非规范
        {"pool_version": 4, "desired_cidrs": ["10.10.0.1/24"],
         "moves": [{"from_cidr": "10.10.16.32/28", "to_cidr": "10.10.32.0/28"}]},
        # 同族等长约束不符：跨地址族
        {"pool_version": 4, "desired_cidrs": [],
         "moves": [{"from_cidr": "10.10.16.32/28", "to_cidr": "2001:db8::/120"}]},
        # 前缀长度不一致
        {"pool_version": 4, "desired_cidrs": [],
         "moves": [{"from_cidr": "10.10.16.32/28", "to_cidr": "10.10.32.0/27"}]},
        # 组间混用地址族（每组自身同族等长）
        {"pool_version": 4, "desired_cidrs": [], "moves": [
            {"from_cidr": "10.10.16.32/28", "to_cidr": "10.10.32.0/28"},
            {"from_cidr": "2001:db8::/120", "to_cidr": "2001:db8:1::/120"},
        ]},
        # 来源之间重叠
        {"pool_version": 4, "desired_cidrs": [], "moves": [
            {"from_cidr": "10.10.16.32/28", "to_cidr": "10.10.32.0/28"},
            {"from_cidr": "10.10.16.32/29", "to_cidr": "10.10.33.0/29"},
        ]},
        # 目标之间重叠
        {"pool_version": 4, "desired_cidrs": [], "moves": [
            {"from_cidr": "10.10.16.32/28", "to_cidr": "10.10.32.0/28"},
            {"from_cidr": "10.10.0.16/28", "to_cidr": "10.10.32.0/29"},
        ]},
        # 来源/目标/desired 越出父网段（DB 层 -> 422）
        {"pool_version": 4, "desired_cidrs": [],
         "moves": [{"from_cidr": "10.10.16.32/28", "to_cidr": "10.11.0.0/28"}]},
        {"pool_version": 4, "desired_cidrs": ["10.11.0.0/24"],
         "moves": [{"from_cidr": "10.10.16.32/28", "to_cidr": "10.10.32.0/28"}]},
        {"pool_version": 4, "desired_cidrs": [],
         "moves": [{"from_cidr": "10.9.0.0/28", "to_cidr": "10.10.32.0/28"}]},
        # pool_version 非法
        {"pool_version": 0, "desired_cidrs": [],
         "moves": [{"from_cidr": "10.10.16.32/28", "to_cidr": "10.10.32.0/28"}]},
    ]
    for payload in bad_payloads:
        r = client.post(ADJUST_PATH, json=payload)
        assert r.status_code == 422, (payload, r.status_code, r.text)
    assert _pool()["version"] == 4
    print("invalid payloads -> 422, pool untouched")

    # =====================================================================
    # 四、状态冲突 -> 409，池与版本不变
    # =====================================================================
    before = _pool()

    # 版本过期
    r = _adjust(3, [], [
        {"from_cidr": "10.10.16.32/28", "to_cidr": "10.10.32.0/28"}
    ])
    assert r.status_code == 409, r.text
    assert "version changed" in r.json()["detail"]

    # 来源未被旧已分配地址完整覆盖（10.10.32.0/28 完全空闲）
    r = _adjust(4, [], [
        {"from_cidr": "10.10.32.0/28", "to_cidr": "10.10.33.0/28"}
    ])
    assert r.status_code == 409, r.text
    assert "not fully covered" in r.json()["detail"]

    # 来源只覆盖部分已分配：/27 跨越占用的 16.32/28 与空闲的 16.48/28
    r = _adjust(4, [], [
        {"from_cidr": "10.10.16.32/27", "to_cidr": "10.10.32.0/27"}
    ])
    assert r.status_code == 409, r.text
    assert "not fully covered" in r.json()["detail"]

    # 目标占用未迁移的已分配地址：把 0.16/28（当前已分配）迁到
    # 16.32/28（未在本批来源中的已分配地址）。
    r = _adjust(4, [], [
        {"from_cidr": "10.10.0.16/28", "to_cidr": "10.10.16.32/28"}
    ])
    assert r.status_code == 409, r.text
    assert "not being migrated" in r.json()["detail"]

    # 新保留压住未迁移的已分配地址：desired 纳入 16.32/28，但本批
    # 迁移的是 0.16/28（16.32/28 不被释放）。
    r = _adjust(4, ["10.10.16.32/28"], [
        {"from_cidr": "10.10.0.16/28", "to_cidr": "10.10.32.0/28"}
    ])
    assert r.status_code == 409, r.text
    assert "not being migrated" in r.json()["detail"]

    # 最终保留与已分配相交：新保留压住迁移目标 10.10.32.0/28。
    r = _adjust(4, ["10.10.32.0/28"], [
        {"from_cidr": "10.10.0.16/28", "to_cidr": "10.10.32.0/28"}
    ])
    assert r.status_code == 409, r.text
    assert "migrated target" in r.json()["detail"]

    # 目标落在旧保留区但该地址仍属于**新**保留区 -> 最终相交 409。
    # 0.0/23 仍在 desired 中，目标 10.10.0.16/28 与之相交。
    r = _adjust(4, ["10.10.0.0/23"], [
        {"from_cidr": "10.10.16.32/28", "to_cidr": "10.10.0.16/28"}
    ])
    assert r.status_code == 409, r.text

    after = _pool()
    assert after == before
    print("state conflicts -> 409, pool and version unchanged")

    # =====================================================================
    # 五、互换/链式目标：可落在本批来源释放的地址上
    # =====================================================================
    # 当前已分配：0.16/28、16.32/28。来源取这两段，目标互换。
    r = _adjust(4, [], [
        {"from_cidr": "10.10.0.16/28", "to_cidr": "10.10.16.32/28"},
        {"from_cidr": "10.10.16.32/28", "to_cidr": "10.10.0.16/28"},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 5
    assert sorted(body["allocated_cidrs"], key=_cidr_num_key) == [
        "10.10.0.16/28",
        "10.10.16.32/28",
    ], body["allocated_cidrs"]
    print("swap targets onto other sources ok")

    # =====================================================================
    # 六、desired 允许空、重叠、相邻；目标可使用解除的旧保留地址
    # =====================================================================
    # v4 的新保留是 0.0/23+...，旧 /20 中被解除保留的有 2.0/23、
    # 3.0/24 等；清空保留区（desired 为空）后把两段已分配搬到这些
    # 旧保留地址上。
    r = _adjust(5, [], [
        {"from_cidr": "10.10.0.16/28", "to_cidr": "10.10.3.0/28"},
        {"from_cidr": "10.10.16.32/28", "to_cidr": "10.10.2.0/28"},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 6
    assert body["reserved_cidrs"] == [], body["reserved_cidrs"]
    assert sorted(body["allocated_cidrs"], key=_cidr_num_key) == [
        "10.10.2.0/28",
        "10.10.3.0/28",
    ], body["allocated_cidrs"]
    print("empty desired + targets reuse released reservations ok")

    # desired 重叠 + 相邻输入，折叠为最少 CIDR：把这两段已分配迁到完全
    # 空闲的 32.0/28、33.0/28，再把旧保留 0.0/21（重叠/相邻混合输入）
    # 与 255.0/24 保留回来。
    r = _adjust(6, [
        "10.10.0.0/21",          # 与下面两条重叠/包含
        "10.10.0.0/24",
        "10.10.1.0/24",
        "10.10.255.0/24",
    ], [
        {"from_cidr": "10.10.2.0/28", "to_cidr": "10.10.32.0/28"},
        {"from_cidr": "10.10.3.0/28", "to_cidr": "10.10.33.0/28"},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 7
    assert sorted(body["reserved_cidrs"], key=_cidr_num_key) == [
        "10.10.0.0/21",
        "10.10.255.0/24",
    ], body["reserved_cidrs"]
    assert sorted(body["allocated_cidrs"], key=_cidr_num_key) == [
        "10.10.32.0/28",
        "10.10.33.0/28",
    ], body["allocated_cidrs"]
    print("overlapping/adjacent desired folds minimally ok")

    # =====================================================================
    # 七、同版本并发调整：写锁互斥，恰有一个成功
    # =====================================================================
    barrier = threading.Barrier(2)
    results: dict[int, object] = {}

    def worker(idx, dst):
        barrier.wait()
        results[idx] = _adjust(7, [], [
            {"from_cidr": "10.10.32.0/28", "to_cidr": dst}
        ])

    t0 = threading.Thread(target=worker, args=(0, "10.10.40.0/28"))
    t1 = threading.Thread(target=worker, args=(1, "10.10.41.0/28"))
    t0.start()
    t1.start()
    t0.join(timeout=30)
    t1.join(timeout=30)
    assert not t0.is_alive() and not t1.is_alive()
    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], codes
    assert _pool()["version"] == 8  # 只自增一次
    winner = next(r for r in results.values() if r.status_code == 200)
    assert winner.json()["pool_version"] == 8
    print("concurrent adjust: exactly one succeeds ok")

    # =====================================================================
    # 八、IPv6 池（直接走 Database 层）
    # =====================================================================
    v6_db = Database(Settings(
        db_path=os.path.join(tempfile.mkdtemp(), "v6_adjust.db"),
        parent_cidr="2001:db8::/48",
        reserved_cidrs_raw="2001:db8::/64",
    ))
    v6_db.commit_plan(1, (ipaddress.ip_network("2001:db8:0:1::/64"),))
    # 把保留从 ::/64 挪到 0:1::/64（当前已分配），已分配迁回旧保留区
    # 中解除的 ::100/120……用等长 /64：目标 2001:db8::/64 正是本次解除
    # 的旧保留地址。
    state = v6_db.adjust_reservations(
        2,
        (ipaddress.ip_network("2001:db8:0:1::/64"),),
        ((ipaddress.ip_network("2001:db8:0:1::/64"),
          ipaddress.ip_network("2001:db8::/64")),),
    )
    assert state.version == 3
    assert tuple(str(n) for n in state.reserved) == ("2001:db8:0:1::/64",)
    assert tuple(str(n) for n in state.allocated) == ("2001:db8::/64",)

    # v6 冲突：新保留压住未迁移的已分配（无迁移，0:1::/64 仍占用）。
    try:
        v6_db.adjust_reservations(
            3,
            (ipaddress.ip_network("2001:db8::/63"),),
            (),
        )
        raise AssertionError("expected ConflictError")
    except ConflictError:
        pass
    assert v6_db.get_state().version == 3

    # v6 非法输入：目标越出父网段 -> AdjustInputError（HTTP 422）。
    try:
        v6_db.adjust_reservations(
            3,
            (),
            ((ipaddress.ip_network("2001:db8::/64"),
              ipaddress.ip_network("2001:db9::/64")),),
        )
        raise AssertionError("expected AdjustInputError")
    except AdjustInputError:
        pass
    assert v6_db.get_state().version == 3
    print("IPv6 adjust + conflict + input error ok")

    print("ALL RESERVATION ADJUST TESTS PASSED")


if __name__ == "__main__":
    main()
