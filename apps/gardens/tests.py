import threading
import time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import OperationalError, connection
from django.test import TransactionTestCase, Client
from django.urls import reverse
from django.utils import timezone

from .models import (
    Garden,
    InvalidStatusTransition,
    Trough,
    WitherBatch,
    transition_trough_status,
)
from .views import _trough_status_counts


User = get_user_model()


def _make_garden():
    return Garden.objects.create(name="测试园", altitudeBand="600-800m")


def _run_concurrent(trough_id, new_status, expected_status):
    """两个线程用屏障对齐后并发提交，返回 {线程名: (结果, 消息)}。"""
    results = {}
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(name):
        try:
            barrier.wait(timeout=10)
            for attempt in range(60):
                try:
                    saved = transition_trough_status(
                        trough_id, new_status, expected_status=expected_status
                    )
                    with lock:
                        results[name] = ("成功", saved.status)
                    return
                except InvalidStatusTransition as exc:
                    with lock:
                        results[name] = ("拒绝", " ".join(exc.messages))
                    return
                except OperationalError as exc:
                    # SQLite 库级写锁竞争：退避后在新事务中重试，
                    # 重试时会读到对方已提交的状态并落入“拒绝”。
                    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                        time.sleep(0.05 * (attempt + 1))
                        continue
                    raise
        finally:
            connection.close()

    threads = [
        threading.Thread(target=worker, args=("甲",)),
        threading.Thread(target=worker, args=("乙",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


class StatusMachineTests(TransactionTestCase):
    def setUp(self):
        self.garden = _make_garden()
        self.trough = Trough.objects.create(
            garden=self.garden,
            troughCode="A-01",
            cultivar="福鼎大白",
            loadKg=Decimal("100.00"),
            status=Trough.STATUS_LOADING,
        )

    def test_legal_cycle(self):
        transition_trough_status(self.trough.pk, Trough.STATUS_WITHERING)
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_WITHERING)

        # 无合格含水率时不能进入 ready
        WitherBatch.objects.create(
            trough=self.trough,
            startedAt=timezone.now() - timezone.timedelta(hours=18),
            targetMoisture=Decimal("40"),
            actualMoisture=None,
            rollGrade="待评",
        )
        with self.assertRaises(InvalidStatusTransition):
            transition_trough_status(self.trough.pk, Trough.STATUS_READY)
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_WITHERING)

        WitherBatch.objects.create(
            trough=self.trough,
            startedAt=timezone.now() - timezone.timedelta(hours=2),
            targetMoisture=Decimal("38"),
            actualMoisture=Decimal("37.5"),
            rollGrade="一级",
        )
        transition_trough_status(self.trough.pk, Trough.STATUS_READY)
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_READY)

        transition_trough_status(self.trough.pk, Trough.STATUS_LOADING)
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_LOADING)

    def test_illegal_transition_rejected_and_unchanged(self):
        # loading 不能直接跳到 ready
        with self.assertRaises(InvalidStatusTransition):
            transition_trough_status(
                self.trough.pk,
                Trough.STATUS_READY,
                expected_status=Trough.STATUS_LOADING,
            )
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_LOADING)

    def test_same_status_duplicate_submit_rejected(self):
        with self.assertRaises(InvalidStatusTransition):
            transition_trough_status(
                self.trough.pk,
                Trough.STATUS_LOADING,
                expected_status=Trough.STATUS_LOADING,
            )

    def test_stale_expected_status_rejected(self):
        transition_trough_status(self.trough.pk, Trough.STATUS_WITHERING)
        # 另一人拿着旧页面（以为仍是 loading）提交
        with self.assertRaises(InvalidStatusTransition):
            transition_trough_status(
                self.trough.pk,
                Trough.STATUS_WITHERING,
                expected_status=Trough.STATUS_LOADING,
            )


class ConcurrencyTests(TransactionTestCase):
    """并发互斥：同一槽两笔同时提交，必须恰好一成一拒。"""

    def setUp(self):
        garden = _make_garden()
        self.trough = Trough.objects.create(
            garden=garden,
            troughCode="A-01",
            cultivar="福鼎大白",
            loadKg=Decimal("120.00"),
            status=Trough.STATUS_WITHERING,
        )
        WitherBatch.objects.create(
            trough=self.trough,
            startedAt=timezone.now() - timezone.timedelta(hours=2),
            targetMoisture=Decimal("38"),
            actualMoisture=Decimal("37.5"),
            rollGrade="一级",
        )

    def test_concurrent_submissions_only_one_succeeds(self):
        results = _run_concurrent(
            self.trough.pk,
            Trough.STATUS_READY,
            Trough.STATUS_WITHERING,
        )
        successes = [n for n, (o, _) in results.items() if o == "成功"]
        rejections = [n for n, (o, _) in results.items() if o == "拒绝"]

        self.assertEqual(len(successes), 1, results)
        self.assertEqual(len(rejections), 1, results)

        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_READY)

        # 只有一行被更新，不存在双成功/非法中间态
        self.assertEqual(Trough.objects.filter(status=Trough.STATUS_READY).count(), 1)
        self.assertEqual(Trough.objects.exclude(
            status__in=[s for s, _ in Trough.STATUS_CHOICES]
        ).count(), 0)

    def test_no_half_update_after_loser(self):
        _run_concurrent(
            self.trough.pk,
            Trough.STATUS_READY,
            Trough.STATUS_WITHERING,
        )
        # 败者回滚后，槽记录仍可正常读取、列表照常工作
        trough = Trough.objects.get(pk=self.trough.pk)
        self.assertEqual(trough.status, Trough.STATUS_READY)
        self.assertEqual(
            str(trough.latest_batch().actualMoisture), "37.50"
        )


class ViewTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user("tester", password="pw123456!")
        self.client = Client()
        self.client.force_login(self.user)
        garden = _make_garden()
        self.trough = Trough.objects.create(
            garden=garden,
            troughCode="A-01",
            cultivar="福鼎大白",
            loadKg=Decimal("120.00"),
            status=Trough.STATUS_WITHERING,
        )
        WitherBatch.objects.create(
            trough=self.trough,
            startedAt=timezone.now() - timezone.timedelta(hours=2),
            targetMoisture=Decimal("38"),
            actualMoisture=Decimal("37.5"),
            rollGrade="一级",
        )

    def test_home_and_list_open_after_rejected_submit(self):
        # 非法提交（withering -> loading 不合法）
        resp = self.client.post(
            reverse("trough_change_status", args=[self.trough.pk]),
            {
                "new_status": Trough.STATUS_LOADING,
                "expected_status": Trough.STATUS_WITHERING,
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_WITHERING)

        # 失败后首页与槽列表都必须正常打开
        home = self.client.get(reverse("home"))
        self.assertEqual(home.status_code, 200)
        listing = self.client.get(reverse("trough_list"))
        self.assertEqual(listing.status_code, 200)

    def test_stale_submit_via_view_shows_chinese_error(self):
        self.client.post(
            reverse("trough_change_status", args=[self.trough.pk]),
            {
                "new_status": Trough.STATUS_READY,
                "expected_status": Trough.STATUS_WITHERING,
            },
        )
        resp = self.client.post(
            reverse("trough_change_status", args=[self.trough.pk]),
            {
                "new_status": Trough.STATUS_READY,
                "expected_status": Trough.STATUS_WITHERING,
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        messages_text = " ".join(
            str(m) for m in resp.context["messages"]
        )
        self.assertIn("拒绝", messages_text)
        self.trough.refresh_from_db()
        self.assertEqual(self.trough.status, Trough.STATUS_READY)

    def test_home_cards_reconcile_with_filtered_list_rows(self):
        # 再加一条 loading，使三态分布明确
        Trough.objects.create(
            garden=self.trough.garden,
            troughCode="A-02",
            cultivar="铁观音",
            loadKg=Decimal("90.00"),
            status=Trough.STATUS_LOADING,
        )
        counts = _trough_status_counts()
        for status, _label in Trough.STATUS_CHOICES:
            resp = self.client.get(reverse("trough_list"), {"status": status})
            self.assertEqual(resp.status_code, 200)
            rows = resp.context["object_list"]
            # 首页状态卡数字 == 按态过滤后的列表行数
            self.assertEqual(
                len(rows),
                counts[status],
                msg=f"状态 {status} 首页卡与列表行数不一致",
            )
            self.assertTrue(all(t.status == status for t in rows))

        home = self.client.get(reverse("home"))
        self.assertEqual(home.context["loading_count"], counts[Trough.STATUS_LOADING])
        self.assertEqual(
            home.context["withering_count"], counts[Trough.STATUS_WITHERING]
        )
        self.assertEqual(home.context["ready_count"], counts[Trough.STATUS_READY])
        self.assertEqual(
            home.context["trough_count"], sum(counts.values())
        )

    def test_change_status_success_then_filter_redirect(self):
        resp = self.client.post(
            reverse("trough_change_status", args=[self.trough.pk]),
            {
                "new_status": Trough.STATUS_READY,
                "expected_status": Trough.STATUS_WITHERING,
                "filter_status": "withering",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("?status=withering", resp.url)

    def test_get_on_change_endpoint_redirects(self):
        resp = self.client.get(
            reverse("trough_change_status", args=[self.trough.pk])
        )
        self.assertEqual(resp.status_code, 302)
