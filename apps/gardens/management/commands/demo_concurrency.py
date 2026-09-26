"""并发演示：两个线程几乎同时提交同一萎凋槽的状态变更。

用法：
    python manage.py demo_concurrency

预期（并发预期说明见 README「并发与状态迁移」）：
    两人同时基于「萎凋中」提交 —— 甲 →可下槽、乙 →装叶中。
    两者各自都是合法迁移，但同一槽最多成功一回：先提交者胜，
    后到者被 TroughStatusConflict 拒绝，最终状态与先到者一致，
    首页状态卡与列表按态过滤行数仍可对账。
"""

import threading
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import connections
from django.utils import timezone

from apps.gardens.models import Garden, Trough, WitherBatch
from apps.gardens.services import TroughStatusConflict, update_trough

DEMO_GARDEN = "并发演示园"
DEMO_CODE = "DEMO-01"


class Command(BaseCommand):
    help = "并发演示：同槽双提交状态变更，验证最多一回合法迁移成功"

    def _reset_demo_trough(self):
        """幂等复位：演示槽固定回到「萎凋中」，最新批次含水率 37.5（≤40）。"""
        garden, _ = Garden.objects.get_or_create(
            name=DEMO_GARDEN,
            defaults={"altitudeBand": "900-1100m", "notes": "demo_concurrency 演示用"},
        )
        trough, _ = Trough.objects.get_or_create(
            garden=garden,
            troughCode=DEMO_CODE,
            defaults={"cultivar": "福鼎大白", "loadKg": Decimal("100.00")},
        )
        # 复位绕过迁移图（ready→withering 本不合法），仅作演示准备
        Trough.objects.filter(pk=trough.pk).update(status=Trough.STATUS_WITHERING)
        batch, _ = WitherBatch.objects.get_or_create(
            trough=trough,
            rollGrade="演示批",
            defaults={
                "startedAt": timezone.now(),
                "targetMoisture": Decimal("38.00"),
            },
        )
        batch.startedAt = timezone.now()
        batch.actualMoisture = Decimal("37.50")
        batch.save()
        trough.refresh_from_db()
        return trough

    def handle(self, *args, **options):
        trough = self._reset_demo_trough()
        engine = connections["default"].settings_dict["ENGINE"]
        self.stdout.write(
            f"演示槽：{trough}（当前状态：{trough.get_status_display()}，"
            f"最新批次实测含水率 37.5%）"
        )
        if "sqlite" in engine:
            self.stdout.write(
                self.style.WARNING(
                    "提示：当前为 SQLite，select_for_update 为空操作，"
                    "互斥由条件更新（CAS）保证；PostgreSQL 下另有行级锁双保险。"
                )
            )

        base = {
            "garden": trough.garden,
            "troughCode": trough.troughCode,
            "cultivar": trough.cultivar,
            "loadKg": trough.loadKg,
        }
        barrier = threading.Barrier(2)
        outcomes = {}

        def worker(name, target_status):
            """模拟一个用户：表单在「萎凋中」时打开，随后提交状态变更。"""
            try:
                barrier.wait(timeout=10)
                update_trough(
                    trough_id=trough.pk,
                    expected_status=Trough.STATUS_WITHERING,
                    changes={**base, "status": target_status},
                )
                outcomes[name] = ("成功", f"已迁移到「{target_status}」")
            except TroughStatusConflict as exc:
                outcomes[name] = ("被拒绝", str(exc))
            except Exception as exc:  # 如 SQLite 忙等待超时，同样不得双成功
                outcomes[name] = ("被拒绝", f"{type(exc).__name__}: {exc}")
            finally:
                connections.close_all()

        threads = [
            threading.Thread(
                target=worker, args=("甲（萎凋中→可下槽）", Trough.STATUS_READY)
            ),
            threading.Thread(
                target=worker, args=("乙（萎凋中→装叶中）", Trough.STATUS_LOADING)
            ),
        ]
        self.stdout.write("两个线程同时提交（均基于「萎凋中」）……")
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for name, (result, detail) in sorted(outcomes.items()):
            style = self.style.SUCCESS if result == "成功" else self.style.WARNING
            self.stdout.write(style(f"  {name}：{result} —— {detail}"))

        trough.refresh_from_db()
        succeeded = [n for n, (r, _) in outcomes.items() if r == "成功"]
        self.stdout.write(f"最终状态：{trough.get_status_display()}")

        # 对账：首页状态卡口径 == 列表按态过滤口径 == 总数
        total = Trough.objects.count()
        per_status = {
            label: Trough.objects.filter(status=value).count()
            for value, label in Trough.STATUS_CHOICES
        }
        reconciled = sum(per_status.values()) == total
        self.stdout.write(
            "对账：总数 {total} = {detail}".format(
                total=total,
                detail=" + ".join(
                    f"{label} {count}" for label, count in per_status.items()
                ),
            )
        )

        if len(succeeded) == 1 and reconciled:
            self.stdout.write(
                self.style.SUCCESS(
                    "符合并发预期：恰一回合法迁移成功，另一回被拒绝；"
                    "无半更新，状态计数可对账。"
                )
            )
            if succeeded[0].startswith("甲"):
                expected_final = Trough.STATUS_READY
            else:
                expected_final = Trough.STATUS_LOADING
            if trough.status != expected_final:
                raise AssertionError("最终状态与成功提交不一致")
        else:
            raise AssertionError(
                f"不符合并发预期：成功数={len(succeeded)}，对账={reconciled}"
            )
