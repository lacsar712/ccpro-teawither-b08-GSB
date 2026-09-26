"""并发演示：两人几乎同时提交同一槽的状态变更。

用法：
    python manage.py demo_status_race                # 默认选种子槽 A-01（萎凋中）
    python manage.py demo_status_race --trough-id 3
    python manage.py demo_status_race --keep         # 演示后保留为「可下槽」

预期（任何后端都应恰好一次成功、一次拒绝）：
  - PostgreSQL：SELECT ... FOR UPDATE 行级锁把两笔事务串行化，后到者锁内重读
    发现状态已变，expected_status 不匹配，事务整体回滚并被拒绝。
  - SQLite（USE_SQLITE=1 开发库）：无行级锁，走事务内重读 + 带状态条件的
    UPDATE（WHERE status = 旧值）兜底；库级写锁串行化写竞争，命令对
    "database is locked" 做有限重试，重开事务后同样落到“状态已变”拒绝。
"""

import threading
import time

from django.core.management.base import BaseCommand
from django.db import OperationalError, connection
from django.db.models import Q

from apps.gardens.models import (
    InvalidStatusTransition,
    Trough,
    transition_trough_status,
)


class Command(BaseCommand):
    help = "并发演示：两个线程几乎同时提交同一槽 withering -> ready，应恰好一次成功"

    def add_arguments(self, parser):
        parser.add_argument("--trough-id", type=int, default=None, help="目标槽 ID")
        parser.add_argument(
            "--keep",
            action="store_true",
            help="演示后保留「可下槽」；默认恢复原态，便于重复演示",
        )

    def handle(self, *args, **options):
        trough = self._pick_trough(options["trough_id"])
        original_status = trough.status

        self.stdout.write(
            "目标槽：%s（ID=%s），初始状态=%s"
            % (trough, trough.pk, Trough.status_label(trough.status))
        )
        if trough.status != Trough.STATUS_WITHERING:
            self.stdout.write(
                self.style.WARNING(
                    "目标槽不是「萎凋中」，并发迁移可能因状态机本身被拒；"
                    "建议先运行 seed_data 并选 A-01。"
                )
            )

        results = {}
        errors_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(name):
            # 线程有独立的数据库连接，必须自行管理生命周期。
            try:
                barrier.wait(timeout=10)
                for attempt in range(60):
                    try:
                        saved = transition_trough_status(
                            trough.pk,
                            Trough.STATUS_READY,
                            expected_status=Trough.STATUS_WITHERING,
                        )
                        with errors_lock:
                            results[name] = ("成功", "已落库为「%s」"
                                             % Trough.status_label(saved.status))
                        return
                    except InvalidStatusTransition as exc:
                        with errors_lock:
                            results[name] = ("拒绝", " ".join(exc.messages))
                        return
                    except OperationalError as exc:  # 仅 SQLite 写锁竞争时出现
                        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                            time.sleep(0.05 * (attempt + 1))
                            continue
                        with errors_lock:
                            results[name] = ("数据库错误", str(exc))
                        return
            finally:
                connection.close()

        t1 = threading.Thread(target=worker, args=("提交甲",))
        t2 = threading.Thread(target=worker, args=("提交乙",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.stdout.write("")
        for name in ("提交甲", "提交乙"):
            outcome, detail = results.get(name, ("无结果", ""))
            style = self.style.SUCCESS if outcome == "成功" else (
                self.style.WARNING if outcome == "拒绝" else self.style.ERROR
            )
            self.stdout.write(style("  %s：%s —— %s" % (name, outcome, detail)))

        trough.refresh_from_db()
        successes = [n for n, (o, _) in results.items() if o == "成功"]
        rejections = [n for n, (o, _) in results.items() if o == "拒绝"]
        self.stdout.write("")
        self.stdout.write(
            "数据库实际状态：%s" % Trough.status_label(trough.status)
        )

        ok = len(successes) == 1 and len(rejections) == 1 and (
            trough.status == Trough.STATUS_READY
        )
        if ok:
            self.stdout.write(
                self.style.SUCCESS(
                    "结论：并发下仅 %s 一笔合法迁移成功，%s 被拒绝，无双成功、无非法态。"
                    % (successes[0], rejections[0])
                )
            )
        else:
            self.stdout.write(
                self.style.ERROR("结论：结果不符合并发预期，请检查保存路径互斥逻辑。")
            )

        if not options["keep"] and original_status != trough.status:
            self._restore(trough, original_status)
            self.stdout.write(
                "已恢复初始状态「%s」，可重复运行（加 --keep 可保留结果）。"
                % Trough.status_label(original_status)
            )

    def _pick_trough(self, trough_id):
        if trough_id is not None:
            return Trough.objects.get(pk=trough_id)

        # 优先种子槽 A-01：萎凋中且最新批次实测含水率合格，两笔提交在业务上都合法，
        # 纯粹比拼并发互斥。
        candidate = (
            Trough.objects.filter(
                status=Trough.STATUS_WITHERING,
                troughCode="A-01",
            )
            .filter(
                Q(batches__actualMoisture__isnull=False)
                & Q(batches__actualMoisture__lte=40)
            )
            .first()
        )
        if candidate is not None:
            return candidate

        raise SystemExit(
            "未找到可演示的「萎凋中」槽（A-01 不存在或含水率不合格）。"
            "请先运行 python manage.py seed_data，或用 --trough-id 指定。"
        )

    def _restore(self, trough, original_status):
        # 沿状态机反向走回原态：ready -> loading -> withering。
        reverse = {
            Trough.STATUS_READY: Trough.STATUS_LOADING,
            Trough.STATUS_LOADING: Trough.STATUS_WITHERING,
        }
        while trough.status != original_status:
            nxt = reverse.get(trough.status)
            if nxt is None:
                break
            transition_trough_status(trough.pk, nxt)
            trough.refresh_from_db()
