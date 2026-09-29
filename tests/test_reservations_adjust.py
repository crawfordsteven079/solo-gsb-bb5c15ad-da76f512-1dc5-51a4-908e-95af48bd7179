"""保留区调整（替换保留区 + 同事务整批迁移）接口端到端测试。

直接运行：``python3 -m tests.test_reservations_adjust``（或 pytest 收集）。
"""
import ipaddress
import os
import tempfile
import threading

os.environ["PARENT_CIDR"] = "10.10.0.0/16"
os.environ["RESERVED_CIDRS"] = "10.10.0.0/20,10.10.255.0/24"
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_reservations_adjust.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def _pool():
    return client.get("/api/pool").json()


def _commit(version, allocations):
    r = client.post(
        "/api/allocations/commit",
        json={"pool_version": version, "allocations": allocations},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _adjust(version, desired, moves):
    return client.post(
        "/api/pool/reservations/adjust",
        json={
            "pool_version": version,
            "desired_cidrs": desired,
            "moves": moves,
        },
    )


def main():
    initial = _pool()
    assert initial["version"] == 1
    assert initial["reserved_cidrs"] == ["10.10.0.0/20", "10.10.255.0/24"]
    assert initial["allocated_cidrs"] == []

    # 两块普通空闲地址：其中 10.10.100.0/24 将成为新保留区，10.10.200.0/24
    # 作为不参与迁移的已分配地址。
    committed = _commit(1, ["10.10.100.0/24", "10.10.200.0/24"])
    assert committed["pool_version"] == 2

    # 串行地“先替换保留区”会撞上 10.10.100.0/24；复合调整同时把它搬到
    # 即将解除的旧保留区 10.10.0.0/24。相邻的新保留目标折叠为 /23。
    r = _adjust(
        2,
        ["10.10.100.0/24", "10.10.101.0/24"],
        [{"from_cidr": "10.10.100.0/24", "to_cidr": "10.10.0.0/24"}],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "adjusted"
    assert body["pool_version"] == 3
    assert body["moves"] == [
        {"from_cidr": "10.10.100.0/24", "to_cidr": "10.10.0.0/24"}
    ]
    assert body["reserved_cidrs"] == ["10.10.100.0/23"]
    assert body["allocated_cidrs"] == [
        "10.10.0.0/24",
        "10.10.200.0/24",
    ]
    pool = _pool()
    assert pool["version"] == 3
    assert pool["reserved_cidrs"] == body["reserved_cidrs"]
    assert pool["allocated_cidrs"] == body["allocated_cidrs"]
    assert pool["free_cidrs"] == body["free_cidrs"]
    snapshot = client.get("/api/pool/versions/3").json()
    assert snapshot["reserved_cidrs"] == body["reserved_cidrs"]
    assert snapshot["allocated_cidrs"] == body["allocated_cidrs"]
    print("replace reservations and relocate in one instant ok")

    before = _pool()

    invalid_payloads = [
        # moves 数量必须为 1～8
        {"pool_version": 3, "desired_cidrs": [], "moves": []},
        {
            "pool_version": 3,
            "desired_cidrs": [],
            "moves": [
                {"from_cidr": "10.10.0.0/24", "to_cidr": f"10.10.{i}.0/24"}
                for i in range(2, 11)
            ],
        },
        # 非规范 CIDR
        {
            "pool_version": 3,
            "desired_cidrs": [],
            "moves": [{"from_cidr": "10.10.0.1/24", "to_cidr": "10.10.1.0/24"}],
        },
        {
            "pool_version": 3,
            "desired_cidrs": [],
            "moves": [{"from_cidr": "10.10.0.0/24", "to_cidr": "10.10.1.1/24"}],
        },
        {
            "pool_version": 3,
            "desired_cidrs": ["10.10.100.1/24"],
            "moves": [{"from_cidr": "10.10.0.0/24", "to_cidr": "10.10.1.0/24"}],
        },
        # 同族等长约束
        {
            "pool_version": 3,
            "desired_cidrs": [],
            "moves": [{"from_cidr": "10.10.0.0/24", "to_cidr": "10.10.1.0/25"}],
        },
        {
            "pool_version": 3,
            "desired_cidrs": [],
            "moves": [{"from_cidr": "10.10.0.0/24", "to_cidr": "2001:db8::/120"}],
        },
        # 来源之间 / 目标之间不得重叠
        {
            "pool_version": 3,
            "desired_cidrs": [],
            "moves": [
                {"from_cidr": "10.10.0.0/25", "to_cidr": "10.10.1.0/25"},
                {"from_cidr": "10.10.0.0/26", "to_cidr": "10.10.2.0/26"},
            ],
        },
        {
            "pool_version": 3,
            "desired_cidrs": [],
            "moves": [
                {"from_cidr": "10.10.0.0/25", "to_cidr": "10.10.2.0/25"},
                {"from_cidr": "10.10.200.0/26", "to_cidr": "10.10.2.0/26"},
            ],
        },
        # 所有迁移必须同族（每组两端也必须同族）
        {
            "pool_version": 3,
            "desired_cidrs": [],
            "moves": [
                {"from_cidr": "10.10.0.0/24", "to_cidr": "10.10.1.0/24"},
                {"from_cidr": "2001:db8::/120", "to_cidr": "2001:db8:1::/120"},
            ],
        },
        # 越出父网段 / 地址族不匹配
        {
            "pool_version": 3,
            "desired_cidrs": [],
            "moves": [{"from_cidr": "10.10.0.0/24", "to_cidr": "10.11.0.0/24"}],
        },
        {
            "pool_version": 3,
            "desired_cidrs": ["10.11.0.0/24"],
            "moves": [{"from_cidr": "10.10.0.0/24", "to_cidr": "10.10.1.0/24"}],
        },
        {
            "pool_version": 3,
            "desired_cidrs": ["2001:db8::/64"],
            "moves": [{"from_cidr": "10.10.0.0/24", "to_cidr": "10.10.1.0/24"}],
        },
    ]
    for payload in invalid_payloads:
        r = client.post("/api/pool/reservations/adjust", json=payload)
        assert r.status_code == 422, (payload, r.status_code, r.text)
    assert _pool() == before
    print("invalid inputs -> 422, pool untouched")

    # 状态冲突：版本过期、来源未由旧已分配完整覆盖、目标撞上未迁移占用、
    # 新保留区撞上未迁移占用。
    r = _adjust(
        2,
        [],
        [{"from_cidr": "10.10.0.0/24", "to_cidr": "10.10.1.0/24"}],
    )
    assert r.status_code == 409, r.text
    assert "version changed" in r.json()["detail"]

    r = _adjust(
        3,
        [],
        [{"from_cidr": "10.10.2.0/24", "to_cidr": "10.10.1.0/24"}],
    )
    assert r.status_code == 409, r.text
    assert "not fully covered" in r.json()["detail"]

    r = _adjust(
        3,
        [],
        [{"from_cidr": "10.10.0.0/24", "to_cidr": "10.10.200.0/24"}],
    )
    assert r.status_code == 409, r.text
    assert "not being migrated" in r.json()["detail"]

    r = _adjust(
        3,
        ["10.10.50.0/24"],
        [{"from_cidr": "10.10.0.0/24", "to_cidr": "10.10.50.0/24"}],
    )
    assert r.status_code == 409, r.text
    assert "desired reservation" in r.json()["detail"]

    r = _adjust(
        3,
        ["10.10.200.0/24"],
        [{"from_cidr": "10.10.0.0/24", "to_cidr": "10.10.1.0/24"}],
    )
    assert r.status_code == 409, r.text
    assert "desired reservations overlap" in r.json()["detail"]
    assert _pool() == before
    print("state conflicts -> 409, pool untouched")

    # 整批同时语义：A 的目标落在 B 的来源，B 搬到另一个刚解除的旧保留地址。
    r = _adjust(
        3,
        ["10.10.100.0/23"],
        [
            {"from_cidr": "10.10.0.0/24", "to_cidr": "10.10.200.0/24"},
            {"from_cidr": "10.10.200.0/24", "to_cidr": "10.10.1.0/24"},
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 4
    assert body["reserved_cidrs"] == ["10.10.100.0/23"]
    assert body["allocated_cidrs"] == [
        "10.10.1.0/24",
        "10.10.200.0/24",
    ]
    print("batch chain can reuse another migrated source ok")

    # 清空保留集，同时把占用搬到另一块旧保留地址 10.10.255.0/24。
    r = _adjust(
        4,
        [],
        [
            {"from_cidr": "10.10.1.0/24", "to_cidr": "10.10.255.0/24"},
            # 另一组原地迁移：保留集清空后旧保留/已分配语义仍按同时刻计算。
            {"from_cidr": "10.10.200.0/24", "to_cidr": "10.10.200.0/24"},
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 5
    assert body["reserved_cidrs"] == []
    assert body["allocated_cidrs"] == [
        "10.10.200.0/24",
        "10.10.255.0/24",
    ]
    print("empty desired reservations and multiple moves ok")

    # 同版本并发：写锁互斥，只有一个复合调整成功。
    barrier = threading.Barrier(2)
    results = {}

    def worker(idx, target):
        barrier.wait()
        results[idx] = _adjust(
            5,
            ["10.10.50.0/24"],
            [{"from_cidr": "10.10.255.0/24", "to_cidr": target}],
        )

    threads = [
        threading.Thread(target=worker, args=(0, "10.10.0.0/24")),
        threading.Thread(target=worker, args=(1, "10.10.1.0/24")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert sorted(r.status_code for r in results.values()) == [200, 409]
    assert _pool()["version"] == 6
    winner = next(r for r in results.values() if r.status_code == 200).json()
    assert winner["reserved_cidrs"] == ["10.10.50.0/24"]
    assert winner["allocated_cidrs"] == [
        "10.10.200.0/24",
        winner["moves"][0]["to_cidr"],
    ]
    print("concurrent adjustment: exactly one succeeds ok")

    # IPv6 池直接走持久化层：目标可复用本次解除的旧保留区。
    def v6_net(value):
        return ipaddress.ip_network(value, strict=True)

    v6_db = Database(
        Settings(
            db_path=os.path.join(tempfile.mkdtemp(), "v6-adjust.db"),
            parent_cidr="2001:db8::/48",
            reserved_cidrs_raw="2001:db8::/64,2001:db8:0:ffff::/64",
        )
    )
    v6_db.commit_plan(1, (v6_net("2001:db8:0:100::/64"),))
    v6_state = v6_db.replace_reservations_with_relocations(
        2,
        (v6_net("2001:db8:0:100::/64"),),
        ((v6_net("2001:db8:0:100::/64"), v6_net("2001:db8::/64")),),
    )
    assert v6_state.version == 3
    assert tuple(str(n) for n in v6_state.reserved) == (
        "2001:db8:0:100::/64",
    )
    assert tuple(str(n) for n in v6_state.allocated) == ("2001:db8::/64",)
    print("IPv6 adjustment ok")

    print("ALL RESERVATION ADJUST TESTS PASSED")


if __name__ == "__main__":
    main()
