"""已采用结果的现场关闭复核：冻结版本、追加隔断、记录持久化与并发读取。

覆盖：
1. 小图独立枚举对拍：随机小图 × 全部合法（污染源, 保护区）划分 ×
   多组现场已关闭/必须保持开启组合，直接枚举所有可选追加切断集合，
   与 Dinic 约束最小割的费用、最小源侧裁决、清单和见证对拍；
   保持开启约束不可行时服务也不得产生结果；
2. 非建议管段先被关闭，或必须保持开启迫使原割不可用：复核在约束
   网络上重新求最优，而不是机械补齐原建议清单；
3. 零费用边与"若已隔断则新增清单为空"；
4. 方案修订后复核：只能引用采用时冻结的方案版本（后来修订的同名
   管段费用不得混入），且复核不修改原方案、计算记录与采用快照；
5. 复核记录冻结输入与结果，按 ID 在新会话（重启后）读取一致，
   多线程并发读取也一致；
6. 未知/重复管段、无采用结果、数据库写入失败都不留下半条复核记录。
"""

import random
import threading
from collections import deque

import pytest

from app import services
from app.db import SessionLocal
from app.errors import ApiError
from app.flow import InfeasibleReviewError, solve_residual_min_cut
from app.models import Review

VALID_PLAN = {
    "zones": ["SRC1", "SRC2", "MID", "SAFE1", "SAFE2"],
    "segments": [
        {"id": "p1", "from": "SRC1", "to": "MID", "cost": 4},
        {"id": "p2", "from": "SRC2", "to": "MID", "cost": 6},
        {"id": "p3", "from": "MID", "to": "SAFE1", "cost": 5},
        {"id": "p4", "from": "MID", "to": "SAFE2", "cost": 7},
    ],
    "sources": ["SRC1", "SRC2"],
    "protections": ["SAFE1", "SAFE2"],
}
# 修订后：p4 费用 7 -> 1
REVISED_PLAN = {
    "zones": ["SRC1", "SRC2", "MID", "SAFE1", "SAFE2"],
    "segments": [
        {"id": "p1", "from": "SRC1", "to": "MID", "cost": 4},
        {"id": "p2", "from": "SRC2", "to": "MID", "cost": 6},
        {"id": "p3", "from": "MID", "to": "SAFE1", "cost": 5},
        {"id": "p4", "from": "MID", "to": "SAFE2", "cost": 1},
    ],
    "sources": ["SRC1", "SRC2"],
    "protections": ["SAFE1", "SAFE2"],
}
# 再修订一版：新增 p9（冻结版本之外的管段，复核不得引用）
EXTENDED_PLAN = {
    "zones": ["SRC1", "SRC2", "MID", "SAFE1", "SAFE2"],
    "segments": VALID_PLAN["segments"]
    + [{"id": "p9", "from": "MID", "to": "SAFE2", "cost": 1}],
    "sources": ["SRC1", "SRC2"],
    "protections": ["SAFE1", "SAFE2"],
}

ZERO_PLAN = {
    "zones": ["S", "A", "T"],
    "segments": [
        {"id": "z", "from": "S", "to": "A", "cost": 0},
        {"id": "w", "from": "A", "to": "T", "cost": 5},
    ],
    "sources": ["S"],
    "protections": ["T"],
}

# S -> A 必须保持开启时，只能追加关闭 A -> T 的 a2
FORCED_OPEN_PLAN = {
    "zones": ["S", "A", "B", "T"],
    "segments": [
        {"id": "a1", "from": "S", "to": "A", "cost": 1},
        {"id": "a2", "from": "A", "to": "T", "cost": 2},
        {"id": "b1", "from": "S", "to": "B", "cost": 4},
        {"id": "b2", "from": "B", "to": "T", "cost": 8},
    ],
    "sources": ["S"],
    "protections": ["T"],
}


# ---------- HTTP 辅助 ----------

def put(client, pid, plan):
    return client.put(f"/plans/{pid}", json=plan)


def compute(client, pid):
    return client.post(f"/plans/{pid}/computations").json()


def adopt(client, pid, cid):
    return client.post(f"/plans/{pid}/adopt", json={"computation_id": cid})


def review(client, pid, closed, open_segments=..., include_open=False):
    body = {"closed_segments": closed}
    if include_open or open_segments is not ...:
        body["open_segments"] = [] if open_segments is ... else open_segments
    return client.post(f"/plans/{pid}/reviews", json=body)


def adopt_plan(client, pid, plan=VALID_PLAN):
    put(client, pid, plan)
    cid = compute(client, pid)["computation_id"]
    assert adopt(client, pid, cid).status_code == 200
    return cid


def count_reviews(plan_id):
    db = SessionLocal()
    try:
        return db.query(Review).filter(Review.plan_id == plan_id).count()
    finally:
        db.close()


def cut_isolates_sources(plan, cut_ids):
    """删除 cut_ids 管段后，任何污染源都不应能到达任何保护区。"""
    cut = set(cut_ids)
    adjacency = {}
    for seg in plan["segments"]:
        if seg["id"] in cut:
            continue
        adjacency.setdefault(seg["from"], []).append(seg["to"])
    seen = set(plan["sources"])
    queue = deque(plan["sources"])
    while queue:
        node = queue.popleft()
        for nxt in adjacency.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen.isdisjoint(plan["protections"])


# ---------- 1. 独立枚举对拍 ----------

def brute_force_residual(plan, closed):
    """独立暴力枚举：移除 closed 后的剩余网络上枚举全部源侧集合。

    在所有最低费用割中取所有最优掩码的交集（最小源侧），返回
    (升序源侧, 升序新增切断管段, 最低追加费用)。
    """
    zones = plan["zones"]
    remaining = [seg for seg in plan["segments"] if seg["id"] not in closed]
    n = len(zones)
    index = {zone: i for i, zone in enumerate(zones)}
    src_mask = 0
    for zone in plan["sources"]:
        src_mask |= 1 << index[zone]
    prot_mask = 0
    for zone in plan["protections"]:
        prot_mask |= 1 << index[zone]

    best_cost = None
    intersection = 0
    for mask in range(1 << n):
        if mask & src_mask != src_mask or mask & prot_mask:
            continue

        def on_source(zone):
            return bool(mask & (1 << index[zone]))

        cost = sum(
            seg["cost"]
            for seg in remaining
            if on_source(seg["from"]) and not on_source(seg["to"])
        )
        if best_cost is None or cost < best_cost:
            best_cost, intersection = cost, mask
        elif cost == best_cost:
            intersection &= mask
    assert best_cost is not None

    side = sorted(zone for zone in zones if intersection & (1 << index[zone]))
    additional = sorted(
        seg["id"]
        for seg in remaining
        if intersection & (1 << index[seg["from"]])
        and not intersection & (1 << index[seg["to"]])
    )
    return side, additional, best_cost


def all_legal_partitions(zones):
    n = len(zones)
    full = (1 << n) - 1
    for src_mask in range(1, 1 << n):
        complement = full ^ src_mask
        sub = complement
        while sub:
            sources = [zones[i] for i in range(n) if src_mask >> i & 1]
            protections = [zones[i] for i in range(n) if sub >> i & 1]
            yield sources, protections
            sub = (sub - 1) & complement


def random_segments(rng, n):
    count = 2 * n + rng.randint(0, n)
    segments = []
    for i in range(count):
        frm, to = rng.randrange(n), rng.randrange(n)  # 允许自环与平行管段
        roll = rng.random()
        if roll < 0.2:
            cost = 0  # 零费用管段
        elif roll < 0.9:
            cost = rng.randint(1, 8)
        else:
            cost = 10**9
        segments.append({"id": f"e{i}", "from": f"Z{frm}", "to": f"Z{to}", "cost": cost})
    return segments


def closed_subsets(rng, segments):
    """空集、全集、全部单元素/双元素子集与若干随机子集。"""
    ids = [seg["id"] for seg in segments]
    subsets = [set(), set(ids)]
    subsets.extend({seg_id} for seg_id in ids)
    subsets.extend(
        {a, b} for i, a in enumerate(ids) for b in ids[i + 1:]
    )
    for _ in range(24):
        subsets.append({seg_id for seg_id in ids if rng.random() < 0.5})
    return subsets


@pytest.mark.parametrize("seed", [0x5E71E301, 0x5E71E302])
def test_residual_review_matches_brute_force(seed):
    rng = random.Random(seed)
    n = 4
    zones = [f"Z{i}" for i in range(n)]
    segments = random_segments(rng, n)
    cost_by_id = {seg["id"]: seg["cost"] for seg in segments}

    for sources, protections in all_legal_partitions(zones):
        plan = {
            "zones": zones,
            "segments": segments,
            "sources": sources,
            "protections": protections,
        }
        for closed in closed_subsets(rng, segments):
            outcome = solve_residual_min_cut(plan, closed)
            side, additional, best_cost = brute_force_residual(plan, closed)

            # 新增建议与追加费用等于独立枚举的最优解（源侧最小裁决一致）
            assert outcome["additional_segments"] == additional, (sources, closed)
            assert outcome["additional_cost"] == best_cost, (sources, closed)
            assert outcome["witness"]["source_zones"] == side, (sources, closed)
            assert outcome["closed_segments"] == sorted(closed)

            # 合并见证 = 现场已关闭 ∪ 新增建议，费用按（冻结）方案求和
            merged = sorted(set(closed) | set(additional))
            assert outcome["witness"]["cut_segments"] == merged, (sources, closed)
            assert outcome["witness"]["total_cost"] == sum(
                cost_by_id[seg_id] for seg_id in merged
            )
            # 新增清单与现场已关闭互不相交
            assert not set(additional) & closed
            # 合并清单确实切断冻结方案中的全部污染路径
            assert cut_isolates_sources(plan, merged)


def reachable_sources(plan, removed):
    """删除 removed 后，从任一污染源可达的区域集合。"""
    removed = set(removed)
    adjacency = {}
    for seg in plan["segments"]:
        if seg["id"] in removed:
            continue
        adjacency.setdefault(seg["from"], []).append(seg["to"])
    seen = set(plan["sources"])
    queue = deque(plan["sources"])
    while queue:
        node = queue.popleft()
        for nxt in adjacency.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def brute_force_constraints(plan, closed, keep_open):
    """直接枚举可选管段的所有追加切断子集。

    已关闭边固定移除；保持开启边不可选；其余边逐一枚举选/不选。
    对可行子集按费用取最小，费用并列时取各可行结果源侧可达集合的
    交集（最小源侧）。返回 (源侧, 新增清单, 最低费用)；无可行子集时
    返回 None。
    """
    closed = set(closed)
    keep_open = set(keep_open)
    optional = [
        seg
        for seg in plan["segments"]
        if seg["id"] not in closed and seg["id"] not in keep_open
    ]
    index = {zone: i for i, zone in enumerate(plan["zones"])}
    cost_by_id = {seg["id"]: seg["cost"] for seg in plan["segments"]}

    best_cost = None
    side_intersection = None

    for mask in range(1 << len(optional)):
        chosen = {
            optional[i]["id"]
            for i in range(len(optional))
            if mask & (1 << i)
        }
        removed = closed | chosen
        reachable = reachable_sources(plan, removed)
        if not reachable.isdisjoint(plan["protections"]):
            continue

        cost = sum(cost_by_id[seg_id] for seg_id in chosen)
        side_mask = 0
        for zone in reachable:
            side_mask |= 1 << index[zone]

        if best_cost is None or cost < best_cost:
            best_cost = cost
            side_intersection = side_mask
        elif cost == best_cost:
            side_intersection &= side_mask

    if best_cost is None:
        return None

    side = sorted(
        zone
        for zone in plan["zones"]
        if side_intersection & (1 << index[zone])
    )
    # 最小源侧的跨边即费用并列裁决下的规范追加集合
    additional = sorted(
        seg["id"]
        for seg in plan["segments"]
        if seg["id"] not in closed and seg["id"] not in keep_open
        and (
            side_intersection & (1 << index[seg["from"]])
            and not side_intersection & (1 << index[seg["to"]])
        )
    )
    return side, additional, best_cost


def constraint_states(rng, segments):
    """空/单元素/双元素/随机的已关闭、保持开启互斥组合。"""
    ids = [seg["id"] for seg in segments]
    states = [(set(), set())]
    states.extend(({seg_id}, set()) for seg_id in ids)
    states.extend((set(), {seg_id}) for seg_id in ids)
    for i, first in enumerate(ids):
        for second in ids[i + 1:]:
            states.append(({first}, {second}))
            states.append(({first, second}, set()))
    for _ in range(32):
        closed, keep_open = set(), set()
        for seg_id in ids:
            choice = rng.randrange(3)
            if choice == 1:
                closed.add(seg_id)
            elif choice == 2:
                keep_open.add(seg_id)
        states.append((closed, keep_open))
    return states


@pytest.mark.parametrize("seed", [0x5E710A01, 0x5E710A02])
def test_forced_open_review_matches_subset_enumeration(seed):
    rng = random.Random(seed)
    n = 3
    zones = [f"Z{i}" for i in range(n)]
    segments = random_segments(rng, n)
    cost_by_id = {seg["id"]: seg["cost"] for seg in segments}

    for sources, protections in all_legal_partitions(zones):
        plan = {
            "zones": zones,
            "segments": segments,
            "sources": sources,
            "protections": protections,
        }
        for closed, keep_open in constraint_states(rng, segments):
            expected = brute_force_constraints(plan, closed, keep_open)
            if expected is None:
                with pytest.raises(InfeasibleReviewError):
                    solve_residual_min_cut(plan, closed, keep_open)
                continue

            side, additional, best_cost = expected
            outcome = solve_residual_min_cut(plan, closed, keep_open)
            assert outcome["closed_segments"] == sorted(closed)
            assert outcome["open_segments"] == sorted(keep_open)
            assert outcome["additional_segments"] == additional
            assert outcome["additional_cost"] == best_cost
            assert outcome["witness"]["source_zones"] == side

            merged = sorted(set(closed) | set(additional))
            assert outcome["witness"]["cut_segments"] == merged
            assert outcome["witness"]["total_cost"] == sum(
                cost_by_id[seg_id] for seg_id in merged
            )
            assert not (set(additional) & keep_open)
            assert cut_isolates_sources(plan, merged)


# ---------- 2/3. 接口行为：新增建议、零费用、已隔断 ----------

def test_review_recommends_remaining_cut(client):
    pid = "review-basic"
    cid = adopt_plan(client, pid)
    # 现场先关闭 p1（费用 4）：剩余网络中 {p2}=6 远优于 {p3,p4}=12
    resp = review(client, pid, ["p1"])
    assert resp.status_code == 200
    record = resp.json()
    assert record["review_id"]
    assert record["created_at"]
    assert record["plan_id"] == pid
    assert record["plan_revision"] == 1
    assert record["computation_id"] == cid
    assert record["closed_segments"] == ["p1"]
    assert record["additional_segments"] == ["p2"]
    assert record["additional_cost"] == 6
    assert record["witness"] == {
        "source_zones": ["SRC1", "SRC2"],
        "cut_segments": ["p1", "p2"],
        "total_cost": 10,
    }

    # 按 ID 在新会话读取：与创建响应逐项一致
    got = client.get(f"/plans/{pid}/reviews/{record['review_id']}")
    assert got.status_code == 200
    assert got.json() == record
    assert count_reviews(pid) == 1


def test_review_when_non_recommended_segment_closed_first(client):
    """非建议管段先被关闭：在剩余网络上重新求最优，而非补齐原清单。

    冻结方案中原建议清单是 {p1,p2}；现场先关闭不在清单内的 p3 后，
    剩余网络中 {p4}=7 优于 {p1,p2}=10，新增建议必须是 p4。
    """
    pid = "review-non-recommended"
    adopt_plan(client, pid)
    record = review(client, pid, ["p3"]).json()
    assert record["closed_segments"] == ["p3"]
    assert record["additional_segments"] == ["p4"]
    assert record["additional_cost"] == 7
    assert record["witness"]["cut_segments"] == ["p3", "p4"]
    assert record["witness"]["total_cost"] == 12
    assert cut_isolates_sources(VALID_PLAN, record["witness"]["cut_segments"])


def test_review_without_any_closed_segment(client):
    """一个管段都没关：复核退化为原方案的最小隔断。"""
    pid = "review-empty"
    adopt_plan(client, pid)
    record = review(client, pid, []).json()
    assert record["closed_segments"] == []
    assert record["additional_segments"] == ["p1", "p2"]
    assert record["additional_cost"] == 10
    assert record["witness"] == {
        "source_zones": ["SRC1", "SRC2"],
        "cut_segments": ["p1", "p2"],
        "total_cost": 10,
    }


def test_review_zero_cost_edge(client):
    pid = "review-zero"
    adopt_plan(client, pid, ZERO_PLAN)
    record = review(client, pid, []).json()
    assert record["additional_segments"] == ["z"]
    assert record["additional_cost"] == 0
    assert record["witness"]["cut_segments"] == ["z"]
    assert record["witness"]["total_cost"] == 0
    assert cut_isolates_sources(ZERO_PLAN, ["z"])


def test_review_already_isolated_gives_empty_additional(client):
    """已隔断时新增清单为空、追加费用为 0，见证只含现场已关闭管段。"""
    pid = "review-isolated"
    adopt_plan(client, pid)
    record = review(client, pid, ["p1", "p2"]).json()
    assert record["additional_segments"] == []
    assert record["additional_cost"] == 0
    assert record["witness"]["cut_segments"] == ["p1", "p2"]
    assert record["witness"]["total_cost"] == 10
    assert cut_isolates_sources(VALID_PLAN, ["p1", "p2"])

    # 零费用方案关闭唯一隔断管段后同样已隔断
    pid2 = "review-isolated-zero"
    adopt_plan(client, pid2, ZERO_PLAN)
    record = review(client, pid2, ["z"]).json()
    assert record["additional_segments"] == []
    assert record["additional_cost"] == 0
    assert record["witness"]["cut_segments"] == ["z"]
    assert record["witness"]["source_zones"] == ["S"]


def test_review_with_required_open_segment_chooses_other_min_cut(client):
    pid = "review-forced-open"
    cid = adopt_plan(client, pid, FORCED_OPEN_PLAN)
    adoption_before = client.get(f"/plans/{pid}/adoption").json()
    computation_before = client.get(
        f"/plans/{pid}/computations/{cid}"
    ).json()
    resp = review(client, pid, [], ["a1"])
    assert resp.status_code == 200
    record = resp.json()
    assert record["plan_revision"] == 1
    assert record["computation_id"] == cid
    assert record["closed_segments"] == []
    assert record["open_segments"] == ["a1"]
    # 普通最小割是 a1(1)+b1(4)=5；禁止切 a1 后改为 a2(2)+b1(4)=6
    assert record["additional_segments"] == ["a2", "b1"]
    assert record["additional_cost"] == 6
    assert record["witness"]["cut_segments"] == ["a2", "b1"]
    assert record["witness"]["total_cost"] == 6
    assert cut_isolates_sources(FORCED_OPEN_PLAN, ["a2", "b1"])

    got = client.get(f"/plans/{pid}/reviews/{record['review_id']}")
    assert got.status_code == 200
    assert got.json() == record
    assert count_reviews(pid) == 1

    # 现场约束只冻结进复核记录，不改变方案/计算/采用快照
    assert client.get(f"/plans/{pid}").json()["plan"] == FORCED_OPEN_PLAN
    assert client.get(f"/plans/{pid}/adoption").json() == adoption_before
    assert (
        client.get(f"/plans/{pid}/computations/{cid}").json()
        == computation_before
    )


def test_review_required_open_with_zero_cost_edge(client):
    pid = "review-forced-open-zero"
    adopt_plan(client, pid, ZERO_PLAN)
    record = review(client, pid, [], ["z"]).json()
    assert record["open_segments"] == ["z"]
    assert record["additional_segments"] == ["w"]
    assert record["additional_cost"] == 5
    assert record["witness"]["cut_segments"] == ["w"]
    assert cut_isolates_sources(ZERO_PLAN, ["w"])


def test_review_required_open_on_already_closed_path(client):
    """已关闭边已经移除；保持开启同路径上的其他边仍可执行。"""
    pid = "review-forced-open-already-isolated"
    adopt_plan(client, pid, VALID_PLAN)
    record = review(client, pid, ["p1", "p2"], ["p3"]).json()
    assert record["closed_segments"] == ["p1", "p2"]
    assert record["open_segments"] == ["p3"]
    assert record["additional_segments"] == []
    assert record["additional_cost"] == 0
    assert record["witness"]["cut_segments"] == ["p1", "p2"]


def test_review_not_executable_when_open_path_remains(client):
    pid = "review-infeasible"
    adopt_plan(client, pid)
    # 关闭 p1 后，SRC2 -> MID -> SAFE1/2 的路径仍存在；若两条出边都
    # 必须保持开启，则没有任何追加边可切断该路径。
    resp = review(client, pid, ["p1"], ["p3", "p4"])
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "REVIEW_NOT_EXECUTABLE"
    assert {d["code"] for d in error["details"]} == {"REVIEW_NOT_EXECUTABLE"}
    assert count_reviews(pid) == 0
    assert client.get(f"/plans/{pid}").json()["revision"] == 1
    assert client.get(f"/plans/{pid}/adoption").json()["plan_revision"] == 1

    # 失败不是校验型输入错误：允许切断上游 p2 后可正常执行
    ok = review(client, pid, ["p1"], ["p3"])
    assert ok.status_code == 200
    assert ok.json()["additional_segments"] == ["p2"]
    assert count_reviews(pid) == 1


def test_review_missing_open_field_keeps_legacy_response(client):
    pid = "review-legacy-open"
    adopt_plan(client, pid)
    record = review(client, pid, ["p1"]).json()  # 请求中完全没有新字段
    assert "open_segments" not in record
    assert set(record) == {
        "review_id",
        "plan_id",
        "plan_revision",
        "computation_id",
        "created_at",
        "closed_segments",
        "additional_segments",
        "additional_cost",
        "witness",
    }
    assert record["additional_segments"] == ["p2"]


def test_review_explicit_empty_open_segments_freezes_empty_constraint(client):
    pid = "review-explicit-empty-open"
    adopt_plan(client, pid)
    record = review(client, pid, ["p1"], [], include_open=True).json()
    assert record["open_segments"] == []
    assert record["additional_segments"] == ["p2"]
    got = client.get(f"/plans/{pid}/reviews/{record['review_id']}")
    assert got.json() == record


# ---------- 4. 方案修订后复核：冻结版本 ----------

def test_review_after_revision_uses_frozen_plan_and_changes_nothing(client):
    pid = "review-frozen"
    cid = adopt_plan(client, pid)  # 第 1 版计算并采用
    # 修订方案：p4 费用 7 -> 1（同名管段的新费用不得混入复核）
    assert put(client, pid, REVISED_PLAN).json()["revision"] == 2

    adoption_before = client.get(f"/plans/{pid}/adoption").json()
    computation_before = client.get(
        f"/plans/{pid}/computations/{cid}"
    ).json()

    # 现场已关闭 p3：冻结第 1 版中 p4 费用仍是 7 → 追加 p4 费用为 7；
    # 若错误混入修订版费用，追加费用会是 1
    record = review(client, pid, ["p3"], ["p2"]).json()
    assert record["plan_revision"] == 1
    assert record["computation_id"] == cid
    assert record["closed_segments"] == ["p3"]
    assert record["open_segments"] == ["p2"]
    assert record["additional_segments"] == ["p4"]
    assert record["additional_cost"] == 7
    assert record["witness"]["total_cost"] == 12
    assert cut_isolates_sources(VALID_PLAN, record["witness"]["cut_segments"])

    # 复核不得修改原方案、计算记录或采用快照
    current = client.get(f"/plans/{pid}").json()
    assert current["revision"] == 2
    assert current["plan"] == REVISED_PLAN
    assert client.get(f"/plans/{pid}/adoption").json() == adoption_before
    assert (
        client.get(f"/plans/{pid}/computations/{cid}").json()
        == computation_before
    )


def test_review_cannot_reference_segment_added_by_later_revision(client):
    """后来修订才新增的管段 p9 不属于冻结方案：未知管段，拒绝且不留记录。"""
    pid = "review-later-segment"
    adopt_plan(client, pid)
    put(client, pid, EXTENDED_PLAN)  # 第 2 版新增 p9
    resp = review(client, pid, [], ["p9"])
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "UNKNOWN_SEGMENT" in {d["code"] for d in error["details"]}
    assert count_reviews(pid) == 0
    # 采用快照仍是第 1 版
    adoption = client.get(f"/plans/{pid}/adoption").json()
    assert adoption["plan_revision"] == 1
    assert all(seg["id"] != "p9" for seg in adoption["plan"]["segments"])


def test_review_history_survives_adoption_replacement(client):
    """采用被替换后，旧复核记录仍按 ID 可读；新复核针对新冻结版本，
    并列费用时沿用源侧最小裁决。"""
    pid = "review-history"
    cid1 = adopt_plan(client, pid)
    r1 = review(client, pid, ["p1"]).json()

    put(client, pid, REVISED_PLAN)
    cid2 = compute(client, pid)["computation_id"]
    assert adopt(client, pid, cid2).status_code == 200

    # 第 2 版关闭 p1：{p2}=6 与 {p3,p4}=6 并列，源侧最小取 {p2}
    r2 = review(client, pid, ["p1"]).json()
    assert r2["plan_revision"] == 2
    assert r2["computation_id"] == cid2
    assert r2["additional_segments"] == ["p2"]
    assert r2["additional_cost"] == 6
    assert r2["witness"]["cut_segments"] == ["p1", "p2"]

    # 旧记录冻结在第 1 版，仍然完整可读
    got1 = client.get(f"/plans/{pid}/reviews/{r1['review_id']}")
    assert got1.status_code == 200
    assert got1.json() == r1
    assert got1.json()["computation_id"] == cid1
    got2 = client.get(f"/plans/{pid}/reviews/{r2['review_id']}")
    assert got2.json() == r2
    assert count_reviews(pid) == 2


# ---------- 5. 并发读取 ----------

def test_concurrent_reads_return_identical_record(client):
    pid = "review-concurrent-read"
    cid = adopt_plan(client, pid)
    record = review(client, pid, ["p3"]).json()
    review_url = f"/plans/{pid}/reviews/{record['review_id']}"
    mismatches = []

    def reader():
        try:
            for _ in range(25):
                got = client.get(review_url)
                if got.status_code != 200 or got.json() != record:
                    mismatches.append(("review", got.status_code, got.text))
                adoption = client.get(f"/plans/{pid}/adoption")
                if adoption.status_code != 200:
                    mismatches.append(("adoption", adoption.status_code))
                elif adoption.json()["computation_id"] != cid:
                    mismatches.append(("adoption-content",))
                plan = client.get(f"/plans/{pid}")
                if plan.status_code != 200 or plan.json()["revision"] != 1:
                    mismatches.append(("plan", plan.status_code))
        except Exception as exc:  # 任何线程异常都让测试显式失败
            mismatches.append(("exception", repr(exc)))

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "reader thread hung"
    assert mismatches == []
    assert count_reviews(pid) == 1


# ---------- 6. 错误与原子性 ----------

def test_review_requires_adoption(client):
    pid = "review-no-adoption"
    put(client, pid, VALID_PLAN)
    resp = review(client, pid, ["p1"])
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ADOPTION_NOT_FOUND"
    assert count_reviews(pid) == 0


def test_review_unknown_plan(client):
    resp = review(client, "ghost", ["p1"])
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PLAN_NOT_FOUND"


def test_review_unknown_segment_rejected_without_record(client):
    pid = "review-unknown-seg"
    adopt_plan(client, pid)
    resp = review(client, pid, ["p1", "nope"])
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert {d["code"] for d in error["details"]} == {"UNKNOWN_SEGMENT"}
    assert count_reviews(pid) == 0


def test_review_duplicate_segment_rejected_without_record(client):
    pid = "review-dup"
    adopt_plan(client, pid)
    resp = review(client, pid, ["p1", "p1"])
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert {d["code"] for d in error["details"]} == {"DUPLICATE_SEGMENT_ID"}
    assert count_reviews(pid) == 0


def test_review_unknown_open_segment_rejected_without_record(client):
    pid = "review-unknown-open"
    adopt_plan(client, pid)
    resp = review(client, pid, ["p1"], ["nope"])
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert {d["code"] for d in error["details"]} == {"UNKNOWN_SEGMENT"}
    assert count_reviews(pid) == 0


def test_review_duplicate_open_segment_rejected_without_record(client):
    pid = "review-dup-open"
    adopt_plan(client, pid)
    resp = review(client, pid, ["p1"], ["p2", "p2"])
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert {d["code"] for d in error["details"]} == {"DUPLICATE_SEGMENT_ID"}
    assert count_reviews(pid) == 0


def test_review_closed_and_open_overlap_rejected_without_record(client):
    pid = "review-closed-open-overlap"
    adopt_plan(client, pid)
    resp = review(client, pid, ["p1", "p2"], ["p2", "p3"])
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert {
        d["code"] for d in error["details"]
    } == {"CLOSED_OPEN_SEGMENT_OVERLAP"}
    assert count_reviews(pid) == 0


INVALID_BODIES = [
    ([], "INVALID_BODY"),
    ("nope", "INVALID_BODY"),
    ({}, "INVALID_CLOSED_SEGMENTS_FIELD"),
    ({"closed_segments": "p1"}, "INVALID_CLOSED_SEGMENTS_FIELD"),
    ({"closed_segments": None}, "INVALID_CLOSED_SEGMENTS_FIELD"),
    ({"closed_segments": [1]}, "INVALID_SEGMENT_ID"),
    ({"closed_segments": ["bad id"]}, "INVALID_SEGMENT_ID"),
    (
        {"closed_segments": [], "open_segments": "p1"},
        "INVALID_OPEN_SEGMENTS_FIELD",
    ),
    (
        {"closed_segments": [], "open_segments": None},
        "INVALID_OPEN_SEGMENTS_FIELD",
    ),
    (
        {"closed_segments": [], "open_segments": [1]},
        "INVALID_SEGMENT_ID",
    ),
    (
        {"closed_segments": [], "open_segments": ["bad id"]},
        "INVALID_SEGMENT_ID",
    ),
]


@pytest.mark.parametrize("body, code", INVALID_BODIES)
def test_review_invalid_payload_rejected_without_record(client, body, code):
    pid = "review-invalid"
    adopt_plan(client, pid)
    resp = client.post(f"/plans/{pid}/reviews", json=body)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert code in {d["code"] for d in error["details"]}
    assert count_reviews(pid) == 0


def test_review_malformed_json(client):
    pid = "review-bad-json"
    adopt_plan(client, pid)
    resp = client.post(
        f"/plans/{pid}/reviews",
        content=b"{broken",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_JSON"
    assert count_reviews(pid) == 0


def test_get_unknown_review(client):
    pid = "review-get-unknown"
    adopt_plan(client, pid)
    resp = client.get(f"/plans/{pid}/reviews/missing123")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REVIEW_NOT_FOUND"


def test_get_review_of_other_plan_is_404(client):
    adopt_plan(client, "review-plan-a")
    adopt_plan(client, "review-plan-b")
    record = review(client, "review-plan-a", ["p1"]).json()
    resp = client.get(f"/plans/review-plan-b/reviews/{record['review_id']}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REVIEW_NOT_FOUND"
    # 属主方案读取正常
    ok = client.get(f"/plans/review-plan-a/reviews/{record['review_id']}")
    assert ok.status_code == 200
    assert ok.json() == record


def test_get_review_unknown_plan(client):
    resp = client.get("/plans/ghost/reviews/anything")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PLAN_NOT_FOUND"


def test_db_write_failure_leaves_no_partial_review_record(client):
    """数据库写入失败：服务整体回滚并报 500，不留下半条复核记录。"""
    pid = "review-db-failure"
    adopt_plan(client, pid)

    db = SessionLocal()
    original_commit = db.commit

    def failing_commit(*args, **kwargs):
        raise RuntimeError("simulated database failure")

    db.commit = failing_commit
    try:
        with pytest.raises(ApiError) as excinfo:
            services.review(db, pid, ["p1"], ["p3"])
        assert excinfo.value.status_code == 500
        assert excinfo.value.code == "INTERNAL_ERROR"
    finally:
        db.commit = original_commit
        db.close()

    assert count_reviews(pid) == 0
    # 失败之后数据库仍可正常工作：重试复核成功
    resp = review(client, pid, ["p1"], ["p3"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["open_segments"] == ["p3"]
    assert count_reviews(pid) == 1
