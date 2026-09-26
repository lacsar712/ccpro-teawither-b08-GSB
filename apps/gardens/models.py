from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import DatabaseError, models, transaction


class InvalidStatusTransition(ValidationError):
    """状态迁移被业务规则或并发互斥拒绝（均为可向用户展示的中文消息）。"""


class Garden(models.Model):
    name = models.CharField("茶园名称", max_length=120)
    altitudeBand = models.CharField("海拔带", max_length=60)
    notes = models.TextField("备注", blank=True, default="")

    class Meta:
        ordering = ["name"]
        verbose_name = "茶园"
        verbose_name_plural = "茶园"

    def __str__(self):
        return self.name


class Trough(models.Model):
    STATUS_LOADING = "loading"
    STATUS_WITHERING = "withering"
    STATUS_READY = "ready"
    STATUS_CHOICES = [
        (STATUS_LOADING, "装叶中"),
        (STATUS_WITHERING, "萎凋中"),
        (STATUS_READY, "可下槽"),
    ]

    # 合法状态机：装叶中 -> 萎凋中 -> 可下槽 ->（出槽后）回到装叶中。
    LEGAL_TRANSITIONS = {
        STATUS_LOADING: {STATUS_WITHERING},
        STATUS_WITHERING: {STATUS_READY},
        STATUS_READY: {STATUS_LOADING},
    }

    garden = models.ForeignKey(
        Garden,
        on_delete=models.CASCADE,
        related_name="troughs",
        verbose_name="茶园",
    )
    troughCode = models.CharField("槽位编号", max_length=40)
    cultivar = models.CharField("茶树品种", max_length=80)
    loadKg = models.DecimalField("装叶量(kg)", max_digits=10, decimal_places=2)
    status = models.CharField(
        "状态",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_LOADING,
    )

    class Meta:
        ordering = ["garden__name", "troughCode"]
        verbose_name = "萎凋槽"
        verbose_name_plural = "萎凋槽"
        constraints = [
            models.UniqueConstraint(
                fields=["garden", "troughCode"],
                name="uniq_trough_code_per_garden",
            ),
        ]

    def __str__(self):
        return f"{self.garden.name}-{self.troughCode}"

    def latest_batch(self):
        return self.batches.order_by("-startedAt", "-id").first()

    def clean(self):
        super().clean()
        # admin / ModelForm 的普通保存路径同样走这条业务规则。
        error = ready_rule_error(self)
        if error is not None:
            raise error

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    @classmethod
    def status_label(cls, status):
        return dict(cls.STATUS_CHOICES).get(status, status)


class WitherBatch(models.Model):
    trough = models.ForeignKey(
        Trough,
        on_delete=models.CASCADE,
        related_name="batches",
        verbose_name="萎凋槽",
    )
    startedAt = models.DateTimeField("开始时间")
    targetMoisture = models.DecimalField(
        "目标含水率(%)", max_digits=5, decimal_places=2
    )
    actualMoisture = models.DecimalField(
        "实测含水率(%)",
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )
    rollGrade = models.CharField("揉捻等级", max_length=40)

    class Meta:
        ordering = ["-startedAt", "-id"]
        verbose_name = "萎凋批次"
        verbose_name_plural = "萎凋批次"

    def __str__(self):
        return f"{self.trough} @ {self.startedAt:%Y-%m-%d %H:%M}"


# ---------------------------------------------------------------------------
# 状态变更的唯一受控入口：行级锁 + 条件守卫，供视图 / 管理命令 / 测试共用。
# ---------------------------------------------------------------------------


def ready_rule_error(trough):
    """设为「可下槽」需最新批次实测含水率已填写且 <= 40%；不满足返回错误。"""
    if trough.status != Trough.STATUS_READY:
        return None
    latest = (
        WitherBatch.objects.filter(trough_id=trough.pk)
        .order_by("-startedAt", "-id")
        .first()
    )
    if latest is None or latest.actualMoisture is None or latest.actualMoisture > Decimal(
        "40"
    ):
        return InvalidStatusTransition(
            "无法设为可下槽：最新萎凋批次的实测含水率为空或高于 40%。"
        )
    return None


def _evaluate_transition(trough, new_status, expected_status):
    """在已持锁/已快照的 trough 上做全部校验，任何不满足都抛 InvalidStatusTransition。"""
    if new_status not in dict(Trough.STATUS_CHOICES):
        raise InvalidStatusTransition(f"未知状态：{new_status}")

    current = trough.status
    if expected_status is not None and current != expected_status:
        # 乐观锁：提交方基于旧页面操作，期间状态已被他人改走。
        raise InvalidStatusTransition(
            "状态已被其他操作改变（当前为「%s」），请刷新列表后重试。"
            % Trough.status_label(current)
        )

    if current == new_status:
        # 重复双击 / 两个完全相同的提交：第二次必须拒绝。
        raise InvalidStatusTransition(
            "槽位已是「%s」状态，请勿重复提交。" % Trough.status_label(current)
        )

    if new_status not in Trough.LEGAL_TRANSITIONS.get(current, set()):
        raise InvalidStatusTransition(
            "非法状态迁移：「%s」不能直接变为「%s」。"
            % (
                Trough.status_label(current),
                Trough.status_label(new_status),
            )
        )

    trough.status = new_status
    error = ready_rule_error(trough)
    if error is not None:
        raise error


def transition_trough_status(trough_id, new_status, *, expected_status=None, using="default"):
    """在一个事务内完成「锁行 -> 校验状态机 -> 落库」。

    并发保证（PostgreSQL）：SELECT ... FOR UPDATE 锁住该槽行，后到的事务阻塞，
    拿到锁后重读 status，前一笔已提交 -> expected/current 不再匹配 -> 整体回滚
    拒绝。因此同一槽两人几乎同时提交时，最多一笔合法迁移成功，另一笔必然拒绝，
    且不会留下任何半更新。

    SQLite 不支持行级锁（开发库），退化为事务内重读 + 带状态条件的 UPDATE
    （WHERE status = 旧值）做并发守卫；写竞争本身也受 SQLite 库级写锁串行化。
    """
    from django.db import connections

    connection = connections[using]

    if connection.features.has_select_for_update:
        return _transition_with_row_lock(
            trough_id, new_status, expected_status, using
        )
    return _transition_with_conditional_update(
        trough_id, new_status, expected_status, using
    )


def _transition_with_row_lock(trough_id, new_status, expected_status, using):
    try:
        with transaction.atomic(using=using):
            trough = (
                Trough.objects.using(using)
                .select_for_update()
                .filter(pk=trough_id)
                .first()
            )
            if trough is None:
                raise InvalidStatusTransition("槽位不存在或已被删除。")
            _evaluate_transition(trough, new_status, expected_status)
            trough.save(using=using, update_fields=["status"])
            return trough
    except DatabaseError as exc:
        # 个别后端声明了能力但执行期仍拒绝（如旧驱动），统一走条件更新兜底，
        # 绝不在拿不到互斥时裸奔放行。
        if "for update" in str(exc).lower():
            return _transition_with_conditional_update(
                trough_id, new_status, expected_status, using
            )
        raise


def _transition_with_conditional_update(trough_id, new_status, expected_status, using):
    with transaction.atomic(using=using):
        trough = Trough.objects.using(using).filter(pk=trough_id).first()
        if trough is None:
            raise InvalidStatusTransition("槽位不存在或已被删除。")
        current_status = trough.status
        _evaluate_transition(trough, new_status, expected_status)
        # 条件 UPDATE 是最后一道并发守卫：只有 status 仍是判断时的旧值才落库。
        updated = (
            Trough.objects.using(using)
            .filter(pk=trough_id, status=current_status)
            .update(status=new_status)
        )
        if updated == 0:
            raise InvalidStatusTransition(
                "状态刚被其他操作改变，请刷新列表后重试。"
            )
        trough.status = new_status
        return trough
