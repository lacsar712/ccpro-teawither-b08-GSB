from django.apps import AppConfig


class GardensConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.gardens"
    verbose_name = "茶园萎凋"

    def ready(self):
        # Seed is also available via management command; AppConfig ready
        # only auto-seeds when TEAWITHER_AUTO_SEED=1 (used by entrypoint).
        import os

        if os.environ.get("TEAWITHER_AUTO_SEED") == "1":
            # Defer until Django apps are fully loaded
            from django.db.models.signals import post_migrate

            def _seed(sender, **kwargs):
                from apps.gardens.seed import ensure_seed_data

                ensure_seed_data()

            post_migrate.connect(_seed, sender=self)
