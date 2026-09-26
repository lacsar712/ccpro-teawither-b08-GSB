# TeaWither-01 · 茶萎凋台账

Django 5 + PostgreSQL 服务端渲染应用：Templates + HTMX + 自定义 CSS，无 Vue/React SPA。

## 技术栈

- Django 5、PostgreSQL
- Session 登录
- HTMX（CDN）局部刷新列表
- Docker Compose：`web` + `db`

## 端口与数据库

| 服务 | 端口 |
|------|------|
| Web  | **4100** |
| Postgres | **5440**（容器内 5432） |

数据库账号：`teawither` / `teawither` / 库名 `teawither`

## 快速启动

```bash
cd TeaWither/TeaWither-01
docker compose up --build -d
```

浏览器打开：http://localhost:4100

演示账号：

- `admin` / `123456`（超级用户）
- `witherer` / `123456`（普通用户）

容器启动时会自动：`migrate` → `seed_data` → `collectstatic` → `gunicorn`

## 本地开发（可选）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
# 确保本机 Postgres 监听 5440，或先 docker compose up -d db
set POSTGRES_HOST=localhost
set POSTGRES_PORT=5440
python manage.py migrate
python manage.py seed_data
python manage.py runserver 0.0.0.0:4100
```

## 业务模型

1. **Garden（茶园）**：`name`、`altitudeBand`、`notes`
2. **Trough（萎凋槽）**：归属茶园、`troughCode`、`cultivar`、`loadKg`、状态 `loading|withering|ready`；同一茶园内槽位编号唯一
3. **WitherBatch（萎凋批次）**：归属槽位、`startedAt`、`targetMoisture`、`actualMoisture`（可空）、`rollGrade`

**业务规则**：
- 状态迁移路径（非法迁移在表单与保存路径都会被拒绝）：`装叶中 → 萎凋中 → 可下槽 → 装叶中`，且 `萎凋中` 可退回 `装叶中`。
- 将槽位状态设为 `ready`（可下槽）时，若最新批次的 `actualMoisture` 为空或大于 40，抛出中文 `ValidationError`。

## 并发与状态迁移（并发预期）

**场景**：两人几乎同时打开同一槽的编辑页（此时状态如「萎凋中」），随后都提交状态变更。

**预期语义：先提交者胜（first-commit-wins）**

1. 最多一回合法迁移成功；另一回被拒绝，页面提示「该槽状态刚被他人修改……请刷新后重试」，不会静默覆盖。
2. 被拒绝的一回整事务回滚，不留半更新；槽列表与首页照常打开，首页状态卡（装叶中/萎凋中/可下槽）与「萎凋槽」列表按态过滤的行数始终可对账（三态计数之和 = 槽位总数）。
3. 非法迁移（如 `装叶中→可下槽`、`可下槽→萎凋中`）无论是否并发都被拒绝。

**实现（保存路径生效，非前端防抖）**：

- 编辑表单带隐藏字段 `expected_status`（用户打开页面时看到的状态）。
- `apps/gardens/services.py:update_trough()` 在单事务内完成：
  1. `select_for_update()` **行级锁**（PostgreSQL）：后到者阻塞至先到者提交，随后读到新状态，与 `expected_status` 不符即抛 `TroughStatusConflict`；
  2. **条件更新 CAS**（`UPDATE ... WHERE pk=? AND status=expected`）作为提交闸：任何后端（含 SQLite）上都是单条原子语句，后到者命中 0 行即拒绝；
  3. 迁移路径与含水率校验在加锁后的实例上执行，任一失败整体回滚。
- `Trough.clean()` 对比库中当前状态校验迁移图，admin / shell 等所有保存路径同样受约束。

**并发演示**（管理命令，可重复执行，演示槽每次复位为「萎凋中」）：

```bash
docker compose exec web python manage.py demo_concurrency
# 本地 SQLite 亦可：USE_SQLITE=1 python manage.py demo_concurrency
```

两个线程基于同一「萎凋中」分别提交 `→可下槽` 与 `→装叶中`（各自均为合法迁移）。预期输出：恰一回成功、另一回被拒绝，最终状态与成功方一致，末尾打印状态计数对账。SQLite 下 `select_for_update` 为空操作，互斥由条件更新（CAS）保证，拒绝原因可能显示为 `database is locked`；PostgreSQL（compose 默认）下为行级锁 + CAS 双保险。

**对账**：「萎凋槽」列表页提供按状态过滤（全部/装叶中/萎凋中/可下槽）并显示行数，可与首页状态卡逐一核对。

## 种子数据

```bash
python manage.py seed_data
```

幂等：已有茶园则只保证账号存在。亦可在环境变量 `TEAWITHER_AUTO_SEED=1` 时于 `post_migrate` 自动播种。

## 目录结构

```
TeaWither-01/
  manage.py
  requirements.txt
  Dockerfile
  entrypoint.sh
  docker-compose.yml
  config/           # 项目配置
  apps/gardens/     # 模型、视图、种子命令
  templates/        # Django 模板
  static/css/       # 自定义样式（茶绿色顶栏）
```
