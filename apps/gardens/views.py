from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    ListView,
    UpdateView,
)

from .forms import GardenForm, TroughForm, WitherBatchForm
from .models import (
    Garden,
    InvalidStatusTransition,
    Trough,
    WitherBatch,
    transition_trough_status,
)


def _wants_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _trough_status_counts():
    """首页状态卡与列表过滤共用的唯一计数口径，保证两边可对账。"""
    rows = {
        row["status"]: row["n"]
        for row in Trough.objects.values("status").annotate(n=Count("id"))
    }
    return {status: rows.get(status, 0) for status, _ in Trough.STATUS_CHOICES}


@login_required
def home(request):
    status_counts = _trough_status_counts()
    context = {
        "garden_count": Garden.objects.count(),
        "trough_count": Trough.objects.count(),
        "batch_count": WitherBatch.objects.count(),
        "ready_count": status_counts[Trough.STATUS_READY],
        "withering_count": status_counts[Trough.STATUS_WITHERING],
        "loading_count": status_counts[Trough.STATUS_LOADING],
    }
    return render(request, "home.html", context)


# ---- Garden ----


class GardenListView(LoginRequiredMixin, ListView):
    model = Garden
    template_name = "gardens/list.html"
    context_object_name = "gardens"

    def get(self, request, *args, **kwargs):
        self.object_list = self.get_queryset()
        if _wants_htmx(request):
            html = render_to_string(
                "gardens/_table.html",
                {"gardens": self.object_list},
                request=request,
            )
            return HttpResponse(html)
        return super().get(request, *args, **kwargs)


class GardenCreateView(LoginRequiredMixin, CreateView):
    model = Garden
    form_class = GardenForm
    template_name = "gardens/form.html"
    success_url = reverse_lazy("garden_list")

    def form_valid(self, form):
        messages.success(self.request, "茶园已创建")
        response = super().form_valid(form)
        if _wants_htmx(self.request):
            return redirect("garden_list")
        return response


class GardenUpdateView(LoginRequiredMixin, UpdateView):
    model = Garden
    form_class = GardenForm
    template_name = "gardens/form.html"
    success_url = reverse_lazy("garden_list")

    def form_valid(self, form):
        messages.success(self.request, "茶园已更新")
        return super().form_valid(form)


class GardenDeleteView(LoginRequiredMixin, DeleteView):
    model = Garden
    template_name = "gardens/confirm_delete.html"
    success_url = reverse_lazy("garden_list")

    def form_valid(self, form):
        messages.success(self.request, "茶园已删除")
        return super().form_valid(form)


# ---- Trough ----


class TroughListView(LoginRequiredMixin, ListView):
    model = Trough
    template_name = "troughs/list.html"
    context_object_name = "troughs"

    def _filter_status(self):
        status = self.request.GET.get("status", "")
        return status if status in dict(Trough.STATUS_CHOICES) else ""

    def get_queryset(self):
        qs = Trough.objects.select_related("garden")
        status = self._filter_status()
        if status:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        status_counts = _trough_status_counts()
        labels = dict(Trough.STATUS_CHOICES)
        context["current_status"] = self._filter_status()
        context["status_counts"] = status_counts
        context["total_count"] = sum(status_counts.values())
        context["status_filters"] = [
            {"value": value, "label": labels[value], "count": status_counts[value]}
            for value, _label in Trough.STATUS_CHOICES
        ]
        return context

    def get(self, request, *args, **kwargs):
        self.object_list = self.get_queryset()
        if _wants_htmx(request):
            html = render_to_string(
                "troughs/_table.html",
                self.get_context_data(),
                request=request,
            )
            return HttpResponse(html)
        return super().get(request, *args, **kwargs)


class TroughCreateView(LoginRequiredMixin, CreateView):
    model = Trough
    form_class = TroughForm
    template_name = "troughs/form.html"
    success_url = reverse_lazy("trough_list")

    def form_valid(self, form):
        messages.success(self.request, "萎凋槽已创建")
        return super().form_valid(form)


class TroughUpdateView(LoginRequiredMixin, UpdateView):
    model = Trough
    form_class = TroughForm
    template_name = "troughs/form.html"
    success_url = reverse_lazy("trough_list")

    def form_valid(self, form):
        messages.success(self.request, "萎凋槽已更新")
        return super().form_valid(form)


class TroughDeleteView(LoginRequiredMixin, DeleteView):
    model = Trough
    template_name = "troughs/confirm_delete.html"
    success_url = reverse_lazy("trough_list")

    def form_valid(self, form):
        messages.success(self.request, "萎凋槽已删除")
        return super().form_valid(form)


@login_required
def trough_change_status(request, pk):
    """槽位状态变更的唯一入口：服务端行级锁互斥，见 models.transition_trough_status。

    任何人都只能通过这里改 status；expected_status 是列表渲染时该槽的当前态，
    并发下他人先改走状态时，后到的提交会被整体回滚并以中文消息拒绝。
    """
    if request.method != "POST":
        return redirect("trough_list")

    trough = get_object_or_404(Trough, pk=pk)
    new_status = request.POST.get("new_status", "")
    expected_status = request.POST.get("expected_status") or None
    filter_status = request.POST.get("filter_status", "")
    if filter_status not in dict(Trough.STATUS_CHOICES):
        filter_status = ""

    def _redirect_to_list():
        url = reverse("trough_list")
        if filter_status:
            url += f"?status={filter_status}"
        return redirect(url)

    try:
        transition_trough_status(
            trough.pk, new_status, expected_status=expected_status
        )
    except InvalidStatusTransition as exc:
        # 业务拒绝或并发落败：事务已回滚，无半更新，列表照常打开。
        messages.error(request, "「%s」状态变更被拒绝：%s"
                       % (trough, " ".join(exc.messages)))
        return _redirect_to_list()

    messages.success(
        request,
        "「%s」已变为「%s」。" % (trough, Trough.status_label(new_status)),
    )
    return _redirect_to_list()


# ---- WitherBatch ----


class BatchListView(LoginRequiredMixin, ListView):
    model = WitherBatch
    template_name = "batches/list.html"
    context_object_name = "batches"

    def get_queryset(self):
        return WitherBatch.objects.select_related("trough", "trough__garden").all()

    def get(self, request, *args, **kwargs):
        self.object_list = self.get_queryset()
        if _wants_htmx(request):
            html = render_to_string(
                "batches/_table.html",
                {"batches": self.object_list},
                request=request,
            )
            return HttpResponse(html)
        return super().get(request, *args, **kwargs)


class BatchCreateView(LoginRequiredMixin, CreateView):
    model = WitherBatch
    form_class = WitherBatchForm
    template_name = "batches/form.html"
    success_url = reverse_lazy("batch_list")

    def form_valid(self, form):
        messages.success(self.request, "萎凋批次已创建")
        return super().form_valid(form)


class BatchUpdateView(LoginRequiredMixin, UpdateView):
    model = WitherBatch
    form_class = WitherBatchForm
    template_name = "batches/form.html"
    success_url = reverse_lazy("batch_list")

    def form_valid(self, form):
        messages.success(self.request, "萎凋批次已更新")
        return super().form_valid(form)


class BatchDeleteView(LoginRequiredMixin, DeleteView):
    model = WitherBatch
    template_name = "batches/confirm_delete.html"
    success_url = reverse_lazy("batch_list")

    def form_valid(self, form):
        messages.success(self.request, "萎凋批次已删除")
        return super().form_valid(form)
