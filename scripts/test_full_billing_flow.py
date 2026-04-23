"""
Week 3 Day 1 — 端到端分期收款测试

流程:
  1. 创建 billing plan (ilci-william namespace, FocusingPro linked)
  2. 验证 DB 状态
  3. 手动通过 Mollie TEST 完成支付
  4. 验证 webhook 处理 (installment=charged, writeback=success)
  5. 验证 FocusingPro CP 记录 (stand3 GUI)
  6. 验证确认邮件收到

Usage:
  cd /opt/clawshow-mcp-server
  .venv/bin/python scripts/test_full_billing_flow.py
  .venv/bin/python scripts/test_full_billing_flow.py --email=you@example.com --inscription=CDUEG202506222590
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _sep(title: str = ""):
    print(f"\n{'='*60}")
    if title:
        print(f"  {title}")
        print(f"{'='*60}")


async def run_test(test_email: str, inscription_code: str):
    from storage.billing_db import BillingDB
    from engines.billing_engine.orchestrator import BillingOrchestrator

    _sep("Week 3 Day 1 — 端到端分期收款测试")
    print(f"  namespace:        ilci-william")
    print(f"  inscription_code: {inscription_code}")
    print(f"  test_email:       {test_email}")

    # ──────────────────────────────────────────────
    # Step 1: 创建 billing plan
    # ──────────────────────────────────────────────
    _sep("Step 1: 创建 billing plan")

    orch = BillingOrchestrator()
    result = await orch.create_plan(
        namespace="ilci-william",
        customer_email=test_email,
        customer_name="Jason Test E2E",
        total_amount=10.00,
        currency="EUR",
        installments=1,
        frequency="one_time",
        start_date="",
        gateway="mollie",
        external_platform_name="focusingpro",
        external_order_id=inscription_code,
        description="Week 3 Day 1 E2E Test",
        notify_customer_email=True,
    )

    if not result.get("success"):
        print(f"  ❌ create_plan failed: {result.get('error')}")
        return

    plan_id = result["plan_id"]
    payment_url = result.get("payment_url", "")
    print(f"  ✅ plan_id:      {plan_id}")
    print(f"  ✅ payment_url:  {payment_url}")

    # ──────────────────────────────────────────────
    # Step 2: 验证数据库
    # ──────────────────────────────────────────────
    _sep("Step 2: 验证 DB 状态")

    db = BillingDB()
    plan = db.get_plan(plan_id, "ilci-william")
    assert plan, "Plan not found in DB"
    print(f"  ✅ plan.status: {plan['status']}")

    installments = db.get_installments(plan_id)
    assert len(installments) >= 1, "No installments"
    inst = installments[0]
    print(f"  ✅ installment[0]: id={inst['id']} status={inst['status']} amount={inst['amount']}")

    # ──────────────────────────────────────────────
    # Step 3: 手动支付
    # ──────────────────────────────────────────────
    _sep("Step 3: 手动支付 (Mollie TEST)")
    print(f"  请完成以下操作:")
    print(f"  1. 打开邮箱 {test_email}")
    print(f"     找 'ILCI – Votre lien de paiement' 邮件")
    print(f"  2. 或直接访问支付页: {payment_url}")
    print(f"  3. 用 TEST 卡: 4111 1111 1111 1111  到期: 12/30  CVC: 123")
    print(f"  4. 完成后回来按 Enter")
    print()
    try:
        input("  → 付款完成后按 Enter ...")
    except EOFError:
        print("  (非交互模式，跳过等待)")

    # ──────────────────────────────────────────────
    # Step 4: 验证 webhook 处理
    # ──────────────────────────────────────────────
    _sep("Step 4: 验证 webhook 处理")
    print("  等待 Mollie webhook (最多 30 秒)...")

    deadline = time.time() + 30
    while time.time() < deadline:
        inst_now = db.get_installments(plan_id)
        inst = inst_now[0] if inst_now else inst
        if inst.get("status") == "charged":
            break
        time.sleep(3)

    print(f"  installment.status:           {inst.get('status')}")
    print(f"  installment.gateway_payment:  {inst.get('gateway_payment_id', '—')}")
    print(f"  installment.writeback_status: {inst.get('writeback_status', '—')}")
    print(f"  installment.focusingpro_id:   {inst.get('focusingpro_record_id', '—')}")

    if inst.get("status") == "charged":
        print(f"  ✅ Installment charged")
    else:
        print(f"  ⚠️  Installment still '{inst.get('status')}' — webhook may not have arrived yet")

    if inst.get("writeback_status") == "success":
        print(f"  ✅ FocusingPro writeback success: {inst.get('focusingpro_record_id')}")
    elif inst.get("writeback_status") == "skipped":
        print(f"  ⚠️  Writeback skipped (already exists)")
    elif inst.get("writeback_status") == "failed":
        print(f"  ❌ Writeback failed: {inst.get('writeback_error')}")
    else:
        print(f"  — Writeback not yet attempted")

    # ──────────────────────────────────────────────
    # Step 5: FocusingPro GUI 确认
    # ──────────────────────────────────────────────
    _sep("Step 5: 请在 FocusingPro stand3 GUI 验证")
    print(f"  1. 登录 stand3 UEG")
    print(f"  2. 找学生 {inscription_code}")
    print(f"  3. 检查收款记录:")
    fp_id = inst.get("focusingpro_record_id", "?")
    print(f"     期望 CP 记录: {fp_id}")
    print(f"     Mode: Online, Status: Confirmed, Amount: 10 EUR")
    print(f"     PayAccountType: Mollie, Abstract 含 'ClawShow'")

    # ──────────────────────────────────────────────
    # Step 6: 确认邮件
    # ──────────────────────────────────────────────
    _sep("Step 6: 检查确认邮件")
    print(f"  邮箱 {test_email} 应收到:")
    print(f"  主题: 'ILCI – Paiement reçu ✓'")
    print(f"  内容: 金额 10€, 期数 1/1, 付款日期, 参考号")

    # ──────────────────────────────────────────────
    # 总结
    # ──────────────────────────────────────────────
    _sep("测试完成")
    ok_webhook = inst.get("status") == "charged"
    ok_writeback = inst.get("writeback_status") in ("success", "skipped")
    print(f"  plan_id:          {plan_id}")
    print(f"  payment_url:      {payment_url}")
    print(f"  installment:      {'✅ charged' if ok_webhook else '⚠️  ' + str(inst.get('status'))}")
    print(f"  FP writeback:     {'✅ ' + str(inst.get('focusingpro_record_id')) if ok_writeback else '❌ ' + str(inst.get('writeback_status'))}")
    print(f"  确认邮件:         手动确认收件箱")
    print()
    if ok_webhook and ok_writeback:
        print("  🎉 分期收款闭环 100% 通过!")
    else:
        print("  ⚠️  部分步骤需要手动确认，查看 stand9 日志:")
        print("       journalctl -u clawshow-mcp -n 50 --no-pager")


def main():
    parser = argparse.ArgumentParser(description="Week 3 Day 1 E2E billing test")
    parser.add_argument("--email", default="jason+e2e@futushow.com")
    parser.add_argument("--inscription", default="CDUEG202506222590",
                        help="FocusingPro inscription code (stand3 verified)")
    args = parser.parse_args()
    asyncio.run(run_test(args.email, args.inscription))


if __name__ == "__main__":
    main()
