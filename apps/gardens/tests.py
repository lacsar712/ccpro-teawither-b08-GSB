from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Garden, Trough, WitherBatch
from .services import TroughStatusConflict, update_trough


def make_garden(name="测试园"):
    return Garden.objects.create(name=name, altitudeBand="800-1000m")


def make_trough(garden, code="T-01", status=Trough.STATUS_LOADING):
    return Trough.objects.create(
        garden=garden,
        troughCode=code,
        cultivar="福鼎大白",
        loadKg=Decimal("100.00"),
        status=status,
    )


def make_batch(trough, moisture=Decimal("37.50")):
    return WitherBatch.objects.create(
        trough=trough,
        startedAt=timezone.now(),
        targetMoisture=Decimal("38.00"),
        actualMoisture=moisture,
        rollGrade="一级",
    )


def changes_for(trough, status):
    return {
        "garden": trough.garden,
        "troughCode": trough.troughCode,
        "cultivar": trough.cultivar,
        "loadKg": trough.loadKg,
        "status": status,
    }


class TransitionGraphTests(TestCase):
    """状态迁移图：clean() 对比库中当前状态，非法迁移被拒绝。"""

    def setUp(self):
        self.garden = make_garden()

    def _move(self, trough, target):
        trough.status = target
        trough.full_clean()

    def test_legal_forward_path(self):
        trough = make_trough(self.garden, status=Trough.STATUS_LOADING)
        self._move(trough, Trough.STATUS_WITHERING)  # 装叶中→萎凋中
        trough.save()
        make_batch(trough, moisture=Decimal("37.50"))
        self._move(trough, Trough.STATUS_READY)  # 萎凋中→可下槽
        trough.save()
        self._move(trough, Trough.STATUS_LOADING)  # 可下槽→装叶中

    def test_withering_can_revert_to_loading(self):
        trough = make_trough(self.garden, status=Trough.STATUS_WITHERING)
        self._move(trough, Trough.STATUS_LOADING)

    def test_loading_to_ready_is_illegal(self):
        trough = make_trough(self.garden, status=Trough.STATUS_LOADING)
        make_batch(trough, moisture=Decimal("37.50"))
        with self.assertRaises(ValidationError):
            self._move(trough, Trough.STATUS_READY)

    def test_ready_to_withering_is_illegal(self):
        trough = make_trough(self.garden, status=Trough.STATUS_WITHERING)
        make_batch(trough, moisture=Decimal("37.50"))
        trough.status = Trough.STATUS_READY
        trough.save()
        with self.assertRaises(ValidationError):
            self._move(trough, Trough.STATUS_WITHERING)

    def test_ready_requires_moisture_le_40(self):
        trough = make_trough(self.garden, status=Trough.STATUS_WITHERING)
        make_batch(trough, moisture=Decimal("42.00"))
        with self.assertRaises(ValidationError):
            self._move(trough, Trough.STATUS_READY)

    def test_unchanged_status_always_allowed(self):
        trough = make_trough(self.garden, status=Trough.STATUS_WITHERING)
        trough.cultivar = "铁观音"
        trough.full_clean()  # 同状态不算迁移


class UpdateTroughServiceTests(TestCase):
    """保存路径互斥：先提交者胜，后到者被拒绝且不留半更新。"""

    def setUp(self):
        self.garden = make_garden()

    def test_happy_path(self):
        trough = make_trough(self.garden, status=Trough.STATUS_WITHERING)
        make_batch(trough)
        update_trough(
            trough_id=trough.pk,
            expected_status=Trough.STATUS_WITHERING,
            changes=changes_for(trough, Trough.STATUS_READY),
        )
        trough.refresh_from_db()
        self.assertEqual(trough.status, Trough.STATUS_READY)

    def test_sequential_race_only_first_wins(self):
        """两人基于同一「萎凋中」提交：第二人 expected 过期被拒绝。"""
        trough = make_trough(self.garden, status=Trough.STATUS_WITHERING)
        make_batch(trough)
        update_trough(
            trough_id=trough.pk,
            expected_status=Trough.STATUS_WITHERING,
            changes=changes_for(trough, Trough.STATUS_READY),
        )
        with self.assertRaises(TroughStatusConflict):
            update_trough(
                trough_id=trough.pk,
                expected_status=Trough.STATUS_WITHERING,
                changes=changes_for(trough, Trough.STATUS_LOADING),
            )
        trough.refresh_from_db()
        self.assertEqual(trough.status, Trough.STATUS_READY)

    def test_rejected_update_leaves_no_partial_write(self):
        trough = make_trough(self.garden, status=Trough.STATUS_WITHERING)
        make_batch(trough)
        update_trough(
            trough_id=trough.pk,
            expected_status=Trough.STATUS_WITHERING,
            changes=changes_for(trough, Trough.STATUS_READY),
        )
        stale = changes_for(trough, Trough.STATUS_LOADING)
        stale["cultivar"] = "被并发修改的品种"
        with self.assertRaises(TroughStatusConflict):
            update_trough(
                trough_id=trough.pk,
                expected_status=Trough.STATUS_WITHERING,
                changes=stale,
            )
        trough.refresh_from_db()
        self.assertEqual(trough.status, Trough.STATUS_READY)
        self.assertEqual(trough.cultivar, "福鼎大白")  # 无半更新

    def test_missing_expected_status_rejected(self):
        trough = make_trough(self.garden, status=Trough.STATUS_WITHERING)
        make_batch(trough)
        with self.assertRaises(TroughStatusConflict):
            update_trough(
                trough_id=trough.pk,
                expected_status="",
                changes=changes_for(trough, Trough.STATUS_READY),
            )
        trough.refresh_from_db()
        self.assertEqual(trough.status, Trough.STATUS_WITHERING)

    def test_business_rule_failure_rolls_back(self):
        """含水率超标 → ValidationError，状态保持原值（事务回滚）。"""
        trough = make_trough(self.garden, status=Trough.STATUS_WITHERING)
        make_batch(trough, moisture=Decimal("42.00"))
        with self.assertRaises(ValidationError):
            update_trough(
                trough_id=trough.pk,
                expected_status=Trough.STATUS_WITHERING,
                changes=changes_for(trough, Trough.STATUS_READY),
            )
        trough.refresh_from_db()
        self.assertEqual(trough.status, Trough.STATUS_WITHERING)


class TroughViewConcurrencyTests(TestCase):
    """视图层：并发拒绝后列表/首页正常，状态卡与按态过滤行数可对账。"""

    def setUp(self):
        self.garden = make_garden()
        User = get_user_model()
        self.user = User.objects.create_user("op", "op@example.com", "pw123456")
        self.client.force_login(self.user)

    def _post_edit(self, trough, status, expected_status=None):
        data = {
            "garden": trough.garden.pk,
            "troughCode": trough.troughCode,
            "cultivar": trough.cultivar,
            "loadKg": "100.00",
            "status": status,
        }
        if expected_status is not None:
            data["expected_status"] = expected_status
        return self.client.post(
            reverse("trough_edit", args=[trough.pk]), data, follow=True
        )

    def _assert_reconciles(self):
        home = self.client.get(reverse("home"))
        self.assertEqual(home.status_code, 200)
        ctx = home.context
        self.assertEqual(
            ctx["loading_count"] + ctx["withering_count"] + ctx["ready_count"],
            ctx["trough_count"],
        )
        for value, key in [
            (Trough.STATUS_LOADING, "loading_count"),
            (Trough.STATUS_WITHERING, "withering_count"),
            (Trough.STATUS_READY, "ready_count"),
        ]:
            resp = self.client.get(reverse("trough_list"), {"status": value})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(len(resp.context["troughs"]), ctx[key])

    def test_concurrent_double_submit_first_wins(self):
        """两人同时编辑同一「萎凋中」槽：一回成功，另一回被拒绝，
        之后首页与列表（含按态过滤）仍正常且对账一致。"""
        trough = make_trough(self.garden, status=Trough.STATUS_WITHERING)
        make_batch(trough)

        # 甲：萎凋中 → 可下槽（合法，先提交）
        resp = self._post_edit(trough, Trough.STATUS_READY, Trough.STATUS_WITHERING)
        self.assertRedirects(resp, reverse("trough_list"))
        trough.refresh_from_db()
        self.assertEqual(trough.status, Trough.STATUS_READY)

        # 乙：同样基于「萎凋中」提交 → 被拒绝并提示刷新
        resp = self._post_edit(trough, Trough.STATUS_LOADING, Trough.STATUS_WITHERING)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "请刷新后重试")
        trough.refresh_from_db()
        self.assertEqual(trough.status, Trough.STATUS_READY)  # 未被覆盖

        self._assert_reconciles()

    def test_illegal_transition_shows_form_error(self):
        trough = make_trough(self.garden, status=Trough.STATUS_LOADING)
        make_batch(trough)
        resp = self._post_edit(trough, Trough.STATUS_READY, Trough.STATUS_LOADING)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "非法状态迁移")
        trough.refresh_from_db()
        self.assertEqual(trough.status, Trough.STATUS_LOADING)
        self._assert_reconciles()

    def test_missing_expected_status_rejected(self):
        trough = make_trough(self.garden, status=Trough.STATUS_WITHERING)
        make_batch(trough)
        resp = self._post_edit(trough, Trough.STATUS_READY)  # 无 expected_status
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "请刷新后重试")
        trough.refresh_from_db()
        self.assertEqual(trough.status, Trough.STATUS_WITHERING)
        self._assert_reconciles()

    def test_plain_field_edit_without_status_change_ok(self):
        trough = make_trough(self.garden, status=Trough.STATUS_WITHERING)
        make_batch(trough)
        data = {
            "garden": trough.garden.pk,
            "troughCode": trough.troughCode,
            "cultivar": "铁观音",
            "loadKg": "100.00",
            "status": Trough.STATUS_WITHERING,
            "expected_status": Trough.STATUS_WITHERING,
        }
        resp = self.client.post(
            reverse("trough_edit", args=[trough.pk]), data, follow=True
        )
        self.assertRedirects(resp, reverse("trough_list"))
        trough.refresh_from_db()
        self.assertEqual(trough.cultivar, "铁观音")
        self.assertEqual(trough.status, Trough.STATUS_WITHERING)

    def test_service_validation_error_rerendered_not_500(self):
        """保存路径在加锁后仍可能因业务规则失败（如含水率竞态）：
        视图须回显表单错误而非 500，且不留半更新。"""
        trough = make_trough(self.garden, status=Trough.STATUS_WITHERING)
        make_batch(trough)
        err = ValidationError(
            {"status": "无法设为可下槽：最新萎凋批次的实测含水率为空或高于 40%。"}
        )
        with patch("apps.gardens.views.update_trough", side_effect=err):
            resp = self._post_edit(
                trough, Trough.STATUS_READY, Trough.STATUS_WITHERING
            )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "无法设为可下槽")
        trough.refresh_from_db()
        self.assertEqual(trough.status, Trough.STATUS_WITHERING)
        self._assert_reconciles()

    def test_edit_form_carries_expected_status(self):
        trough = make_trough(self.garden, status=Trough.STATUS_WITHERING)
        resp = self.client.get(reverse("trough_edit", args=[trough.pk]))
        self.assertContains(resp, 'name="expected_status" value="withering"')

    def test_list_status_filter(self):
        make_trough(self.garden, code="T-01", status=Trough.STATUS_LOADING)
        make_trough(self.garden, code="T-02", status=Trough.STATUS_WITHERING)
        resp = self.client.get(
            reverse("trough_list"), {"status": Trough.STATUS_WITHERING}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["troughs"]), 1)
        self.assertEqual(resp.context["current_status"], Trough.STATUS_WITHERING)
        self.assertContains(resp, "按状态过滤")
        self.assertContains(resp, "共 1 条")
        resp = self.client.get(reverse("trough_list"), {"status": "bogus"})
        self.assertEqual(len(resp.context["troughs"]), 2)  # 非法值回退为全部
