"""已分配地址历史快照恢复接口的端到端测试。

直接运行：``python3 -m tests.test_restore``（或 pytest 收集）。

造数（父网段 10.10.0.0/16，初始保留 10.10.0.0/20、10.10.255.0/24；
优化目标使单前缀分配依次落在池尾空闲块）：

    v1  基线：已分配为空
    v2  提交 /25 -> 10.10.254.0/25
    v3  提交 /25 -> 10.10.254.128/25（与上一条相邻，快照中是两条 /25）
    v4  恢复到 v3 自身：恢复把已分配规范化为最少 CIDR -> 10.10.254.0/24
    v5  提交 /24 -> 10.10.252.0/24
    v6  提交 /24 -> 10.10.253.0/24（v6 快照已分配为 .252/.253/.254 三条 /24）
    v7  误操作：提交 /24 -> 10.10.248.0/24
    v8  恢复到 v6：误分配被撤销，已分配规范化为 10.10.252.0/23 + .254.0/24，
        当前保留区原样保留
    v9  把保留区替换为 10.10.248.0/24（恢复释放出的空闲地址）
        失败恢复若干（均不生成版本）：
          * 恢复到 v7（历史已分配与当前保留区相交）-> 409
          * 过期版本 + 不存在快照 -> 409（版本检查优先）
          * 当前版本 + 不存在快照 -> 404
    v10 恢复到 v1（空已分配快照）：已分配清空，保留区保持 v9 的结果
    v11 同版本并发恢复到 v6：写锁互斥，恰有一个成功

覆盖：
* 恢复只改已分配地址：当前保留区保持原样，历史快照中的保留地址不被采用；
* 恢复后已分配地址规范化为数量最少的规范 CIDR（相邻两条 /25 -> /24；
  .252/.253 相邻 -> /23，.254 单独保留）；
* 历史快照不可变：被引用的 v3/v6 快照在恢复前后逐字节一致，仍保留
  恢复前的记录切分；
* 冲突场景：当前版本过期 -> 409；历史已分配与当前保留区相交 -> 409；
  历史版本不存在 -> 404（绝不以当前状态代替）；版本号非正 -> 422；
* 任一冲突均在写事务内回滚：池与版本不变，不生成新版本快照；
* 成功后查询 / 预演 / 其它写入立即看到恢复后的状态；
* 同版本并发恢复恰有一个成功。
"""
import ipaddress
import os
import tempfile
import threading

os.environ["PARENT_CIDR"] = "10.10.0.0/16"
os.environ["RESERVED_CIDRS"] = "10.10.0.0/20,10.10.255.0/24"
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_restore.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

INITIAL_RESERVED = ["10.10.0.0/20", "10.10.255.0/24"]


def _pool():
    return client.get("/api/pool").json()


def _version(version):
    return client.get(f"/api/pool/versions/{version}")


def _preview(prefixes):
    r = client.post(
        "/api/allocations/preview", json={"prefix_lengths": prefixes}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is True, body
    return body


def _commit(version, allocations):
    r = client.post(
        "/api/allocations/commit",
        json={"pool_version": version, "allocations": allocations},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _restore(version, snapshot_version):
    return client.post(
        "/api/allocations/restore",
        json={
            "pool_version": version,
            "snapshot_version": snapshot_version,
        },
    )


def _replace(version, desired_cidrs):
    return client.post(
        "/api/pool/reservations/replace",
        json={"pool_version": version, "desired_cidrs": desired_cidrs},
    )


def _is_free(cidr: str, free_cidrs: list[str]) -> bool:
    """cidr 完整落在某个规范空闲块内（空闲块是最大块，需按包含判断）。"""
    net = ipaddress.ip_network(cidr)
    return any(
        net.subnet_of(ipaddress.ip_network(free)) for free in free_cidrs
    )


def main():
    # ---- 入参校验：版本号非正 -> 422 ----
    r = _restore(0, 1)
    assert r.status_code == 422, r.text
    r = _restore(1, 0)
    assert r.status_code == 422, r.text
    print("non-positive version -> 422 ok")

    # ---- 基线：v1 已分配为空，v1 快照与当前池一致 ----
    pool = _pool()
    assert pool["version"] == 1
    assert pool["allocated_cidrs"] == []
    assert pool["reserved_cidrs"] == INITIAL_RESERVED
    v1_before = _version(1).json()
    assert v1_before == pool

    # =====================================================================
    # 造数：两条相邻 /25（v2、v3）
    # =====================================================================
    first = _preview([25])["allocations"][0]
    assert first == "10.10.254.0/25", first
    assert _commit(1, [first])["pool_version"] == 2

    second = _preview([25])["allocations"][0]
    assert second == "10.10.254.128/25", second
    assert _commit(2, [second])["pool_version"] == 3

    v3_before = _version(3).json()
    assert v3_before["allocated_cidrs"] == [
        "10.10.254.0/25",
        "10.10.254.128/25",
    ], v3_before["allocated_cidrs"]
    print("seed: two adjacent /25 records ok")

    # =====================================================================
    # 规范化：恢复到当前版本自身的快照，两条相邻 /25 重写为一条 /24
    # =====================================================================
    r = _restore(3, 3)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "restored"
    assert body["restored_from_version"] == 3
    assert body["pool_version"] == 4
    assert body["allocated_cidrs"] == ["10.10.254.0/24"], body
    # 被引用的历史快照不改写：v3 仍是两条 /25、原保留区。
    assert _version(3).json() == v3_before
    # 新版本快照保存规范化结果与当前保留区。
    v4 = _version(4).json()
    assert v4["allocated_cidrs"] == ["10.10.254.0/24"]
    assert v4["reserved_cidrs"] == INITIAL_RESERVED
    pool4 = _pool()
    assert pool4["version"] == 4
    assert body["free_cidrs"] == pool4["free_cidrs"]
    print("restore normalizes adjacent records to minimal CIDRs ok")

    # =====================================================================
    # 再造数：v5=.252/24，v6=.253/24（v6 已分配集 = .252/.253/.254）
    # =====================================================================
    for expect_version, cidr in (
        (4, "10.10.252.0/24"),
        (5, "10.10.253.0/24"),
    ):
        plan = _preview([24])
        assert plan["allocations"] == [cidr], plan
        assert _commit(expect_version, [cidr])["pool_version"] == (
            expect_version + 1
        )
    v6_before = _version(6).json()
    assert v6_before["allocated_cidrs"] == [
        "10.10.252.0/24",
        "10.10.253.0/24",
        "10.10.254.0/24",
    ], v6_before["allocated_cidrs"]

    # =====================================================================
    # 误操作：v7 多分配了 10.10.248.0/24
    # =====================================================================
    mistake = _preview([24])["allocations"][0]
    assert mistake == "10.10.248.0/24", mistake
    assert _commit(6, [mistake])["pool_version"] == 7
    assert _pool()["allocated_cidrs"] == [
        "10.10.248.0/24",
        "10.10.252.0/24",
        "10.10.253.0/24",
        "10.10.254.0/24",
    ]
    print("mistaken allocation committed at v7 ok")

    # 过期版本 -> 409，池与版本不变，不生成 v8。
    r = _restore(6, 6)
    assert r.status_code == 409, r.text
    assert "version changed" in r.json()["detail"]
    assert _pool()["version"] == 7
    assert _version(8).status_code == 404

    # 历史版本不存在 -> 404，不能以当前状态代替。
    r = _restore(7, 999)
    assert r.status_code == 404, r.text
    assert "unknown pool version snapshot: 999" in r.json()["detail"]
    assert _pool()["version"] == 7
    assert _version(8).status_code == 404
    print("stale version -> 409, missing snapshot -> 404, no new version ok")

    # =====================================================================
    # 恢复到 v6：撤销误分配；.252/.253 规范化为 /23，.254 单独保留
    # =====================================================================
    r = _restore(7, 6)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 8
    assert body["restored_from_version"] == 6
    assert body["allocated_cidrs"] == [
        "10.10.252.0/23",
        "10.10.254.0/24",
    ], body["allocated_cidrs"]
    # 当前保留区保持原样，历史快照里的保留地址不影响当前池。
    pool8 = _pool()
    assert pool8["version"] == 8
    assert pool8["reserved_cidrs"] == INITIAL_RESERVED
    assert pool8["allocated_cidrs"] == body["allocated_cidrs"]
    assert pool8["free_cidrs"] == body["free_cidrs"]
    # 误分配地址（10.10.248.0/24）撤销后，其地址回到空闲块
    # 10.10.248.0/22 内，预演立即可见并再次选中它。
    assert _is_free("10.10.248.0/24", pool8["free_cidrs"])
    plan = _preview([24])
    assert plan["pool_version"] == 8
    assert plan["allocations"] == ["10.10.248.0/24"], plan
    # 被引用的 v6 快照逐字节不变（仍是三条 /24）。
    assert _version(6).json() == v6_before
    v8 = _version(8).json()
    assert v8["allocated_cidrs"] == body["allocated_cidrs"]
    assert v8["reserved_cidrs"] == INITIAL_RESERVED
    print("restore to v6 undoes mistake, normalization + visibility ok")

    # =====================================================================
    # v9：把保留区替换成恢复释放出的 10.10.248.0/24（其它写入照常）
    # =====================================================================
    r = _replace(8, ["10.10.248.0/24"])
    assert r.status_code == 200, r.text
    assert r.json()["pool_version"] == 9
    assert _pool()["reserved_cidrs"] == ["10.10.248.0/24"]
    print("reservation replace after restore still works ok")

    # ---- 历史已分配地址与当前保留区相交 -> 409，池与版本不变 ----
    before = _pool()
    r = _restore(9, 7)  # v7 已分配含 10.10.248.0/24，恰为当前保留区
    assert r.status_code == 409, r.text
    assert "overlap current reservations" in r.json()["detail"]
    after = _pool()
    assert after["version"] == before["version"] == 9
    assert after["allocated_cidrs"] == before["allocated_cidrs"]
    assert after["reserved_cidrs"] == before["reserved_cidrs"]
    assert after["free_cidrs"] == before["free_cidrs"]
    assert _version(10).status_code == 404
    print("snapshot allocated intersects current reservations -> 409 ok")

    # ---- 版本过期与快照缺失同时存在：版本检查优先 -> 409 ----
    r = _restore(1, 999)
    assert r.status_code == 409, r.text
    assert "version changed" in r.json()["detail"]
    assert _pool()["version"] == 9
    assert _version(10).status_code == 404

    # 当前版本 + 快照缺失 -> 404（恢复前的缺失检查在写事务内）。
    r = _restore(9, 999)
    assert r.status_code == 404, r.text
    assert _pool()["version"] == 9
    assert _version(10).status_code == 404
    print("conflicts roll back entirely, no version generated ok")

    # =====================================================================
    # v10：恢复到 v1 的空快照 -> 清空已分配；当前保留区保持 v9 的结果
    # =====================================================================
    r = _restore(9, 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 10
    assert body["restored_from_version"] == 1
    assert body["allocated_cidrs"] == []
    pool10 = _pool()
    assert pool10["version"] == 10
    assert pool10["allocated_cidrs"] == []
    # 保留区不随历史快照回退：仍是 v9 替换后的 10.10.248.0/24。
    assert pool10["reserved_cidrs"] == ["10.10.248.0/24"]
    assert pool10["free_cidrs"] == body["free_cidrs"]
    v10 = _version(10).json()
    assert v10["allocated_cidrs"] == []
    assert v10["reserved_cidrs"] == ["10.10.248.0/24"]
    # v1 快照本身不改变（仍记录初始保留区与空已分配）。
    assert _version(1).json() == v1_before
    print("restore to empty snapshot keeps current reservations ok")

    # v10 下快照仍缺失 -> 404，不生成 v11。
    r = _restore(10, 999)
    assert r.status_code == 404, r.text
    assert _version(11).status_code == 404

    # 恢复后预演立即基于恢复后的占用集与当前保留区工作。
    plan = _preview([24])
    assert plan["pool_version"] == 10
    assert not _is_free("10.10.248.0/24", plan["free_cidrs_after"])
    assert plan["allocations"] == ["10.10.249.0/24"], plan
    print("preview after restore reflects restored state ok")

    # =====================================================================
    # 同版本并发恢复：写锁互斥，恰有一个成功（v11）
    # =====================================================================
    barrier = threading.Barrier(2)
    results: dict[int, object] = {}

    def worker(idx):
        barrier.wait()
        results[idx] = _restore(10, 6)

    t0 = threading.Thread(target=worker, args=(0,))
    t1 = threading.Thread(target=worker, args=(1,))
    t0.start()
    t1.start()
    t0.join(timeout=30)
    t1.join(timeout=30)
    assert not t0.is_alive() and not t1.is_alive()
    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], codes
    loser = next(r for r in results.values() if r.status_code == 409)
    assert "version changed" in loser.json()["detail"]

    final = _pool()
    assert final["version"] == 11
    winner = next(r for r in results.values() if r.status_code == 200)
    wb = winner.json()
    assert wb["pool_version"] == 11
    assert wb["restored_from_version"] == 6
    assert wb["allocated_cidrs"] == [
        "10.10.252.0/23",
        "10.10.254.0/24",
    ], wb["allocated_cidrs"]
    assert final["allocated_cidrs"] == wb["allocated_cidrs"]
    assert final["free_cidrs"] == wb["free_cidrs"]
    # 当前保留区仍是恢复前的 10.10.248.0/24（快照 v6 的保留区未被采用，
    # 且与 v6 已分配地址不相交，故恢复成功）。
    assert final["reserved_cidrs"] == ["10.10.248.0/24"]
    # 历史快照依旧不可变。
    assert _version(6).json() == v6_before
    assert _version(1).json() == v1_before
    print("concurrent restore: exactly one succeeds ok")

    print("ALL RESTORE TESTS PASSED")


if __name__ == "__main__":
    main()
