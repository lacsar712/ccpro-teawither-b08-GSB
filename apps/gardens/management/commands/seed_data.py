from django.core.management.base import BaseCommand

from apps.gardens.seed import ensure_seed_data


class Command(BaseCommand):
    help = "写入演示账号与样例茶园/槽位/萎凋批次数据（幂等）"

    def handle(self, *args, **options):
        ensure_seed_data()
        self.stdout.write(self.style.SUCCESS("种子数据已就绪（admin / witherer）"))
