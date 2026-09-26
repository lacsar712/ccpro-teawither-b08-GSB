from django.core.exceptions import ValidationError
from django.db import models


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

    # 合法状态迁移图：装叶中→萎凋中→可下槽→装叶中（萎凋中可退回装叶中）。
    # 并发下由 services.update_trough 以行级锁 + 条件更新保证“最多一回成功”。
    STATUS_TRANSITIONS = {
        STATUS_LOADING: {STATUS_WITHERING},
        STATUS_WITHERING: {STATUS_READY, STATUS_LOADING},
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

    @classmethod
    def is_legal_transition(cls, from_status, to_status):
        """状态迁移是否合法（同状态不算迁移，始终允许）。"""
        if from_status == to_status:
            return True
        return to_status in cls.STATUS_TRANSITIONS.get(from_status, set())

    def latest_batch(self):
        return self.batches.order_by("-startedAt", "-id").first()

    def clean(self):
        super().clean()
        if self.pk:
            current_status = (
                Trough.objects.filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )
            if (
                current_status is not None
                and not self.is_legal_transition(current_status, self.status)
            ):
                labels = dict(self.STATUS_CHOICES)
                raise ValidationError(
                    {
                        "status": (
                            "非法状态迁移：{src} → {dst}。允许路径："
                            "装叶中→萎凋中→可下槽→装叶中（萎凋中可退回装叶中）。"
                        ).format(
                            src=labels.get(current_status, current_status),
                            dst=labels.get(self.status, self.status),
                        )
                    }
                )
        if self.status != self.STATUS_READY:
            return
        latest = None
        if self.pk:
            latest = (
                WitherBatch.objects.filter(trough_id=self.pk)
                .order_by("-startedAt", "-id")
                .first()
            )
        if latest is None or latest.actualMoisture is None or latest.actualMoisture > 40:
            raise ValidationError(
                {
                    "status": "无法设为可下槽：最新萎凋批次的实测含水率为空或高于 40%。"
                }
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


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
