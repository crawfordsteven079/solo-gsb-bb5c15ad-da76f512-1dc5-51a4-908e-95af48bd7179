"""两历史版本地址用途比较接口的端到端测试。

直接运行：``python3 -m tests.test_version_diff``（或 pytest 收集）。

覆盖：
* 保留 / 已分配地址各自的新增、移出方向（from -> to；反向交换）；
* 同一版本比较（from == to）四个列表均为空；
* 按**地址集合**求差：保留区拆分写法规范化后语义不变、已分配记录
  （/24 与两条相邻 /25）拆分/合并不产生伪变化；
* 差集折叠为数量最少的规范 CIDR：移出大片与新增相邻地址块的折叠；
* 按 (网络地址, 前缀长度) 数值序排序（区别于字符串字典序）；
* 任一版本不存在（含 0）返回 404；非整数版本号返回 422；
* 只读：比较不改写当前池版本，旧快照与比较结果可重复读取；
* IPv6 地址族下 address_set_diff 的折叠（直接单元调用）。
"""
from __future__ import annotations

import ipaddress
import os
import tempfile

PARENT = "10.10.0.0/16"
RESERVED = "10.10.0.0/20,10.10.255.0/24"

os.environ["PARENT_CIDR"] = PARENT
os.environ["RESERVED_CIDRS"] = RESERVED
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_version_diff.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.allocator import address_set_diff  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def _diff(frm, to):
    r = client.get(f"/api/pool/versions/{frm}/diff/{to}")
    assert r.status_code == 200, (frm, to, r.text)
    return r.json()


def _commit(version, allocations):
    r = client.post(
        "/api/allocations/commit",
        json={"pool_version": version, "allocations": allocations},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _reclaim(version, cidr):
    r = client.post(
        "/api/allocations/reclaim",
        json={"pool_version": version, "cidr": cidr},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _relocate(version, frm, to):
    r = client.post(
        "/api/allocations/relocate",
        json={
            "pool_version": version,
            "moves": [{"from_cidr": frm, "to_cidr": to}],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _replace(version, desired):
    r = client.post(
        "/api/pool/reservations/replace",
        json={"pool_version": version, "desired_cidrs": desired},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _empty(diff):
    return (
        diff["reserved_added"] == []
        and diff["reserved_removed"] == []
        and diff["allocated_added"] == []
        and diff["allocated_removed"] == []
    )


def main():
    # ---- 构造版本历史 -------------------------------------------------
    # v1: 基线（保留 10.10.0.0/20 + 10.10.255.0/24，无已分配）。
    assert client.get("/api/pool").json()["version"] == 1

    # v2: 直接提交 10.10.16.0/24（提交接口不要求方案出自预演，只要仍可用）。
    assert _commit(1, ["10.10.16.0/24"])["pool_version"] == 2
    # v3: 整批迁移 10.10.16.0/24 -> 10.10.32.0/24。
    assert _relocate(2, "10.10.16.0/24", "10.10.32.0/24")["pool_version"] == 3
    # v4: 保留区以拆分写法替换为**同一地址集合**：
    #     /21 + /21 恰覆盖 10.10.0.0/20，落库规范化回 /20。
    assert _replace(
        3, ["10.10.0.0/21", "10.10.8.0/21", "10.10.255.0/24"]
    )["pool_version"] == 4
    # v5: 保留区换成两条小保留 + 远处的 200/24（均不压在已分配 .32 上）。
    assert _replace(
        4, ["10.10.0.0/24", "10.10.1.0/24", "10.10.200.0/24"]
    )["pool_version"] == 5
    # v6: 去掉 200/24。
    assert _replace(5, ["10.10.0.0/24", "10.10.1.0/24"])["pool_version"] == 6
    # v7: 回收 .32.0/24 的后半 /25，剩 10.10.32.0/25。
    assert _reclaim(6, "10.10.32.128/25")["pool_version"] == 7
    # v8: 把后半 /25 提交回来：已分配地址集合恢复为 10.10.32.0/24，
    #     但存储为两条相邻 /25 记录。
    assert _commit(7, ["10.10.32.128/25"])["pool_version"] == 8
    # v9: 再提交相邻的 10.10.33.0/24（与 /24 集合相邻）。
    assert _commit(8, ["10.10.33.0/24"])["pool_version"] == 9
    # v10: 跨记录回收 10.10.32.0/23（覆盖 /25+/25+/24 三条记录）。
    assert _reclaim(9, "10.10.32.0/23")["pool_version"] == 10
    print("version history v1..v10 prepared")

    # ---- 基本方向：新增 / 移出 ---------------------------------------
    d = _diff(1, 2)
    assert d["from_version"] == 1 and d["to_version"] == 2
    assert d["allocated_added"] == ["10.10.16.0/24"]
    assert d["allocated_removed"] == []
    assert d["reserved_added"] == [] and d["reserved_removed"] == []

    # 反向：新增与移出互换。
    d = _diff(2, 1)
    assert d["allocated_removed"] == ["10.10.16.0/24"]
    assert d["allocated_added"] == []

    # 迁移：同一地址用途集合里“一进一出”。
    d = _diff(2, 3)
    assert d["allocated_added"] == ["10.10.32.0/24"]
    assert d["allocated_removed"] == ["10.10.16.0/24"]
    d = _diff(3, 2)
    assert d["allocated_added"] == ["10.10.16.0/24"]
    assert d["allocated_removed"] == ["10.10.32.0/24"]
    print("directional added/removed diffs ok")

    # ---- 记录拆分 / 合并不产生伪变化（按地址集合比较） ---------------
    # v3 -> v4：保留区以拆分写法提交、落库规范化回同一集合，四个列表全空。
    assert _empty(_diff(3, 4)), _diff(3, 4)
    # v1 -> v4：保留区集合与基线相同（拆分写法无伪变化）；已分配仍只有
    # 迁移后的 10.10.32.0/24。
    d = _diff(1, 4)
    assert d["reserved_added"] == [] and d["reserved_removed"] == []
    assert d["allocated_added"] == ["10.10.32.0/24"]
    assert d["allocated_removed"] == []
    # v7 -> v8：v7 仅剩半块 10.10.32.0/25，v8 补回后半块；集合差为
    # 后半块，不涉及记录拆分；再与完整 /24 的 v6 对照：
    d = _diff(7, 8)
    assert d["allocated_added"] == ["10.10.32.128/25"]
    assert d["allocated_removed"] == []
    d = _diff(6, 8)
    # v6 已分配是完整 10.10.32.0/24；v8 是两条 /25 记录覆盖同一 /24。
    assert _empty(d), d
    print("record split/merge causes no spurious changes ok")

    # ---- 部分回收 / 补回：真实的半块移出与折叠新增 -------------------
    d = _diff(6, 7)
    assert d["allocated_removed"] == ["10.10.32.128/25"]
    assert d["allocated_added"] == []
    d = _diff(7, 9)
    # 新增集合 = .32.128/25 + .33.0/24；二者相邻但合不成一个对齐块，
    # 最少 CIDR 恰为这两条，按网络地址排序。
    assert d["allocated_added"] == ["10.10.32.128/25", "10.10.33.0/24"]
    assert d["allocated_removed"] == []
    d = _diff(8, 9)
    assert d["allocated_added"] == ["10.10.33.0/24"]
    # v9 -> v10：跨三条记录的整片被移出，折叠成单条 /23。
    d = _diff(9, 10)
    assert d["allocated_removed"] == ["10.10.32.0/23"]
    assert d["allocated_added"] == []
    print("allocated folding on added/removed ok")

    # ---- 保留区：新增单点，移出折叠为最少 CIDR，并按数值序排序 ------
    d = _diff(4, 5)
    assert d["reserved_added"] == ["10.10.200.0/24"]
    # 旧保留 = 10.10.0.0/20 + 10.10.255.0/24，新保留 = 0/24 + 1/24；
    # 移出集合 10.10.2.0..10.10.15.255 的最少切分 + 255/24，
    # 按 (网络地址, 前缀长度) 数值序（255 在最后，非字符串序）。
    assert d["reserved_removed"] == [
        "10.10.2.0/23",
        "10.10.4.0/22",
        "10.10.8.0/21",
        "10.10.255.0/24",
    ], d["reserved_removed"]
    d = _diff(5, 6)
    assert d["reserved_removed"] == ["10.10.200.0/24"]
    assert d["reserved_added"] == []
    print("reserved folding and numeric sort ok")

    # ---- 同一版本比较：四个列表均为空 --------------------------------
    for v in (1, 5, 10):
        assert _empty(_diff(v, v)), v

    # ---- 404 / 422 ----------------------------------------------------
    for frm, to in ((1, 999), (999, 1), (0, 1), (11, 12)):
        r = client.get(f"/api/pool/versions/{frm}/diff/{to}")
        assert r.status_code == 404, (frm, to, r.status_code, r.text)
        assert "unknown pool version" in r.json()["detail"]
    for path in (
        "/api/pool/versions/abc/diff/2",
        "/api/pool/versions/1/diff/xyz",
    ):
        r = client.get(path)
        assert r.status_code == 422, (path, r.status_code)
    print("404 for missing snapshots, 422 for non-integer ok")

    # ---- 只读：当前池版本不被比较改写，结果与旧快照可重复读取 -------
    assert client.get("/api/pool").json()["version"] == 10
    again = _diff(1, 2)
    assert again["allocated_added"] == ["10.10.16.0/24"]
    v1 = client.get("/api/pool/versions/1").json()
    assert v1["reserved_cidrs"] == ["10.10.0.0/20", "10.10.255.0/24"]
    assert v1["allocated_cidrs"] == []
    print("diff is read-only and snapshots stay immutable ok")

    # ---- address_set_diff 直测：IPv6 折叠 -----------------------------
    added, removed = address_set_diff(
        128,
        [ipaddress.ip_network("2001:db8::/32")],
        [
            ipaddress.ip_network("2001:db8::/33"),
            ipaddress.ip_network("2001:db8:8000::/33"),
        ],
    )
    assert added == [] and removed == []
    added, removed = address_set_diff(
        128,
        [ipaddress.ip_network("2001:db8::/32")],
        [ipaddress.ip_network("2001:db8::/33")],
    )
    assert [str(n) for n in added] == []
    assert [str(n) for n in removed] == ["2001:db8:8000::/33"]

    print("ALL VERSION DIFF TESTS PASSED")


if __name__ == "__main__":
    main()
