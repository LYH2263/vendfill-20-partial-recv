def test_http_full_flow_partial_then_complete(client, seeded):
    loc = seeded["location_id"]
    ids = seeded["lane_ids"]

    # 开单（幂等）
    r1 = client.post(f"/api/refills/run?location_id={loc}")
    assert r1.status_code == 200
    order_id = r1.json()["id"]
    assert r1.json()["status"] == "active"
    r1b = client.post(f"/api/refills/run?location_id={loc}")
    assert r1b.json()["id"] == order_id  # 不重复开单

    # 第一批：只核销 B1
    r2 = client.post(f"/api/refills/{order_id}/verify", json={"lane_ids": [ids["B1"]]})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "active"
    assert body["verified_count"] == 1 and body["pending_count"] == 2
    by_slot = {l["slot_no"]: l for l in body["lines"]}
    assert by_slot["B1"]["line_status"] == "verified"
    assert by_slot["B1"]["lane_stock"] == 10 and by_slot["B1"]["lane_in_transit"] == 2
    assert by_slot["A1"]["line_status"] == "pending"

    # 汇总端点同一套口径
    s = client.get(f"/api/refills/summary?location_id={loc}").json()
    assert s["status"] == "active" and s["all_verified"] is False
    assert s["verified_qty"] == 7 and s["pending_qty"] == 25

    # 第二批：剩余正补量行 → 整单完成
    r3 = client.post(f"/api/refills/{order_id}/verify",
                     json={"lane_ids": [ids["A1"], ids["C1"]]})
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "completed"
    assert r3.json()["all_verified"] is True

    # 已完成整单再核销 → 409
    r4 = client.post(f"/api/refills/{order_id}/verify", json={"lane_ids": [ids["B1"]]})
    assert r4.status_code == 409 and "整单已完成" in r4.json()["detail"]

    # 货道视图与汇总一致
    lanes = {l["slot_no"]: l for l in client.get("/api/lanes").json()}
    assert (lanes["A1"]["stock"], lanes["A1"]["in_transit"]) == (20, 0)
    assert (lanes["B1"]["stock"], lanes["B1"]["in_transit"]) == (10, 2)
    assert (lanes["C1"]["stock"], lanes["C1"]["in_transit"]) == (10, 0)


def test_http_batch_failure_rolls_back(client, seeded):
    loc = seeded["location_id"]
    ids = seeded["lane_ids"]
    order_id = client.post(f"/api/refills/run?location_id={loc}").json()["id"]
    # 空勾选 → 409，整单不变
    r = client.post(f"/api/refills/{order_id}/verify", json={"lane_ids": []})
    assert r.status_code == 409
    # 勾选满仓行（补量 0）→ 409
    r = client.post(f"/api/refills/{order_id}/verify", json={"lane_ids": [ids["A2"]]})
    assert r.status_code == 409 and "补量为 0" in r.json()["detail"]
    body = client.get(f"/api/refills/{order_id}").json()
    assert body["status"] == "active" and body["verified_count"] == 0
    # 库存在途未动
    lanes = {l["slot_no"]: l for l in client.get("/api/lanes").json()}
    assert (lanes["A2"]["stock"], lanes["A2"]["in_transit"]) == (18, 0)
    assert (lanes["B1"]["stock"], lanes["B1"]["in_transit"]) == (3, 9)


def test_http_void(client, seeded):
    loc = seeded["location_id"]
    ids = seeded["lane_ids"]
    order_id = client.post(f"/api/refills/run?location_id={loc}").json()["id"]
    client.post(f"/api/refills/{order_id}/verify", json={"lane_ids": [ids["B1"]]})
    r = client.post(f"/api/refills/{order_id}/void")
    assert r.status_code == 200 and r.json()["status"] == "voided"
    # 作废单核销被拒
    r2 = client.post(f"/api/refills/{order_id}/verify", json={"lane_ids": [ids["A1"]]})
    assert r2.status_code == 409
    # 待核销行在途已释放
    lanes = {l["slot_no"]: l for l in client.get("/api/lanes").json()}
    assert lanes["A1"]["in_transit"] == 0 and lanes["C1"]["in_transit"] == 0
