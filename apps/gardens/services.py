"""萎凋槽保存路径：并发互斥 + 合法迁移校验。

并发约定（先提交者胜 / first-commit-wins）：
同一槽两人几乎同时提交状态变更时，最多一回合法迁移成功，
另一回抛出 TroughStatusConflict 被拒绝，且不留半更新。
"""

from django.db import transaction

from .models import Trough


class TroughStatusConflict(Exception):
    """提交所基于的状态已被他人修改（表单过期），本次保存被拒绝。"""


def update_trough(*, trough_id, expected_status, changes):
    """在单事务内互斥地更新萎凋槽。

    - ``expected_status``：编辑表单加载时用户看到的状态（隐藏字段回传）。
    - ``changes``：已通过表单校验的字段字典（garden/troughCode/cultivar/loadKg/status）。

    机制（两道闸，任一即可独立保证不双成功）：
    1. ``select_for_update()`` 行级锁（PostgreSQL）：后到者阻塞至先到者提交，
       随后读到新状态，与 expected_status 不符即拒绝；
    2. 条件更新 CAS（``UPDATE ... WHERE status = expected_status``）：
       在任何后端（含 SQLite）上都是单条原子语句，后到者命中 0 行即拒绝。

    校验（迁移路径 + 含水率等）在加锁后的实例上执行；任一失败整个事务回滚，
    不会留下半更新。
    """
    with transaction.atomic():
        try:
            trough = Trough.objects.select_for_update().get(pk=trough_id)
        except Trough.DoesNotExist:
            raise TroughStatusConflict(
                "保存被拒绝：该槽已被他人删除。"
            ) from None
        if not expected_status:
            raise TroughStatusConflict(
                "保存被拒绝：表单缺少状态指纹（可能已过期），请刷新后重试。"
            )
        if trough.status != expected_status:
            raise TroughStatusConflict(
                "保存被拒绝：该槽状态刚被他人修改"
                f"（当前为「{trough.get_status_display()}」），请刷新后重试。"
            )
        for field, value in changes.items():
            setattr(trough, field, value)
        # 迁移路径（clean 对比库中当前状态）+ 含水率规则 + 字段/唯一性校验
        trough.full_clean()
        updated = Trough.objects.filter(
            pk=trough_id, status=expected_status
        ).update(**changes)
        if updated != 1:
            raise TroughStatusConflict(
                "保存被拒绝：该槽状态刚被他人修改，请刷新后重试。"
            )
    return Trough.objects.get(pk=trough_id)
