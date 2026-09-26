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

**业务规则**：将槽位状态设为 `ready`（可下槽）时，若最新批次的 `actualMoisture` 为空或大于 40，抛出中文 `ValidationError`。

## 状态机与并发互斥

槽位状态只能沿状态机单向流转：

```
装叶中(loading) ──▶ 萎凋中(withering) ──▶ 可下槽(ready) ──▶ 装叶中(loading，出槽重装)
```

状态变更只有一个受控入口：列表每行的状态按钮 → `POST /troughs/<id>/change-status/`
→ `apps.gardens.models.transition_trough_status()`。编辑表单、Django admin 均不含可写的
`status` 字段，无法绕过。

**并发预期（两人几乎同时对同一槽提交）**：

- **最多一回合法迁移成功，另一回必然被拒绝**，不出现双成功、不出现非法态。
- 机制：单条事务内 `SELECT ... FOR UPDATE`（PostgreSQL 行级锁）锁行 → 锁内重读当前
  `status` → 校验状态机、重复提交与 `expected_status`（乐观条件）→ 落库。后到事务在
  前一笔提交后才拿到锁，此时状态已变，`expected_status`/当前态不再匹配，**整笔事务
  回滚**，返回中文错误消息并跳回列表；不会留下半更新，槽列表与首页始终可正常打开。
- SQLite（`USE_SQLITE=1` 开发库）不支持行级锁，等价地退化为「事务内重读 + 带状态
  条件的 `UPDATE ... WHERE status = 旧值`」，配合库级写锁串行化；条件不满足即回滚拒绝。
  生产 PostgreSQL 走真正的行级锁。
- 可复现演示（双线程屏障对齐后并发提交，应恰为一成一拒）：

  ```bash
  python manage.py demo_status_race            # 默认演示种子槽 A-01（萎凋中）
  python manage.py demo_status_race --keep     # 保留结果为「可下槽」
  python manage.py demo_status_race --trough-id N
  ```

**计数对账**：首页三张状态卡与「萎凋槽」列表顶部按状态过滤 chip 的行数，来自同一个
聚合口径（`_trough_status_counts()`），逐态相等、总和等于槽总数；点击状态卡即跳到
对应过滤列表，可直接核对。

并发与回滚的自动化测试见 `apps/gardens/tests.py`：
`ConcurrencyTests`（真实双线程，断言恰好一成一拒、无非法态、败者无半更新）、
`ViewTests`（失败后首页/列表 200、首页卡与过滤行数对账）。


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
