# 思安娜商品资料中心交付清单

## 上线前检查

1. 创建虚拟环境并安装依赖：
   `python3 -m venv .venv`
   `source .venv/bin/activate`
   `python3 -m pip install -U pip`
   `python3 -m pip install -r requirements.txt`
2. 复制 `.env.example` 为 `.env`
3. 确认 `CATALOG_DB` 指向正式数据库目录
4. 确认 `CATALOG_UPLOADS` 指向正式图片上传目录
5. 确认自动备份参数：
   `CATALOG_BACKUP_KEEP`
   `CATALOG_BACKUP_HOUR`
   `CATALOG_BACKUP_MINUTE`
6. 正式环境建议设置：
   `CATALOG_SEED_DEMO=0`
   `CATALOG_SEED_SAMPLES=0`
7. 按内部交付要求设置品牌化配置：
   `CATALOG_BRAND_NAME`
   `CATALOG_BRAND_MARK`
   `CATALOG_BRAND_TAGLINE`
   `CATALOG_BRAND_SUBTITLE`
   `CATALOG_BRAND_EYEBROW`
   `CATALOG_CONSOLE_EYEBROW`
   `CATALOG_BRAND_ACCENT`
   `CATALOG_BRAND_ACCENT_STRONG`
   `CATALOG_BRAND_ACCENT_DEEP`
8. 设置首个管理员：
   `CATALOG_BOOTSTRAP_ADMIN_USERNAME`
   `CATALOG_BOOTSTRAP_ADMIN_PASSWORD`
   `CATALOG_BOOTSTRAP_ADMIN_NAME`
9. 首次启动前执行：
   `chmod +x scripts/*.sh`
10. 本机开发可使用 `./scripts/start.sh`；Linux 正式服务器必须使用
    `deploy/systemd/rachel-catalog.service` 中的 Gunicorn 多进程配置，不能继续使用单进程开发服务器。
11. 健康检查：
   `http://127.0.0.1:8765/healthz`
12. 启动前后建议执行：
   `./scripts/check.sh`
13. 如果要常驻运行，可安装 launchd：
   `./scripts/install_launchd.sh`
14. 如果要开启每日自动备份：
   `./scripts/install_backup_launchd.sh`

## 第一阶段并发容量

当前正式配置按实际使用量的约 2 倍预留：

- 8 个 Gunicorn 工作进程，每个进程 4 个线程，共 32 个请求位置
- 最多 2 个商品导入任务同时解析，数据库写入按 1 个任务依次执行
- 最多 4 个普通 Excel 同时生成
- 最多 2 个含图片 Excel 同时生成
- 登录、页面浏览和普通查询不进入重任务队列

建议服务器至少 4 核 CPU、4GB 可用内存；同时运行商品企划中心等其他应用时建议 8GB 或以上。上线后用 `/healthz` 核对版本，并确认服务进程由 Gunicorn 启动。

客户端以 Windows 10/11 的 Edge、Chrome 以及 Microsoft Office/WPS 为兼容基线。用户电脑不需要安装 Python；导入、导出和计算都在服务器完成。

## 强制发布验收

代码同步到正式服务器后，不能把“文件已更新”当作“公网已上线”。必须在服务器代码目录执行：

```bash
./scripts/activate_production_release.sh http://203.205.90.232:8765
```

该命令会更新依赖和 systemd 配置、强制重启服务，再对本机和公网连续验证。只有以下项目全部一致才算发布成功：

1. 构建版本号与待发布代码一致。
2. 源码指纹与待发布文件一致。
3. 服务进程启动时间已更新。
4. 正式环境由 Gunicorn 提供服务，不是 Python WSGIServer 开发服务。
5. 连续多次公网请求均返回相同版本与指纹，避免部分工作进程仍为旧版。

任何一项失败，本次发布都必须按失败处理，不得报告“已更新完成”。

注意：交付包必须同时包含 `catalog_wsgi.py` 和 `deploy/`。缺少任何一项都会让服务器继续使用旧启动入口或旧 systemd 配置。

## 本机安全同步建议

如果代码开发目录和正式运行目录分开，后续发布时不要直接手写 `rsync` 全量覆盖，更不要覆盖正式运行目录的 `.env`。

推荐统一使用：

```bash
./scripts/deploy_local.sh
```

这个脚本会：

1. 同步代码到 `/Users/apple/CatalogBackendDeploy`
2. 自动排除 `.env`
3. 自动排除 `data/`
4. 自动排除 `logs/`
5. 自动排除 `backups/`
6. 自动排除 `.venv`
7. 自动尝试重启 `com.catalogbackend.app`
8. 自动检查 `/healthz`

正式运行目录的配置文件与数据目录建议保持：

- 代码目录：`/Users/apple/CatalogBackendDeploy`
- 运行配置：`/Users/apple/CatalogBackendDeploy/.env`
- 正式数据库：`/Users/apple/CatalogBackendData/catalog.db`
- 正式上传目录：`/Users/apple/CatalogBackendData/uploads`

## 首次验收

1. 用管理员账号登录
2. 立即修改管理员初始密码
3. 创建 A、B、C 正式账号
4. 进入 `/settings/c-fields` 配置 C 部门字段开放范围
5. 如有外部系统调用，生成 C 部门 API 令牌
6. 导入 1 份测试 Excel
7. 验证 A/B 只能编辑自己录入的数据
8. 验证 A 提交后进入 `待B填写`，B 完成后进入 `已完成`
9. 验证 C 只能查看开放字段，且无法修改
10. 检查 `/logs` 是否已有管理审计记录

## 日常巡检

1. 访问 `/healthz` 确认服务状态
2. 检查数据库目录剩余空间
3. 检查上传目录剩余空间
4. 检查 `/logs` 是否存在异常管理动作
5. 定期轮换 C 部门 API 令牌
6. 定期检查被停用账号和长期未使用账号
7. 定期确认 `backups/` 目录是否正常生成并符合保留数量
8. 如果使用 launchd，定期执行：
   `./scripts/launchd_status.sh`
9. 如果使用自动备份，定期执行：
   `./scripts/backup_launchd_status.sh`

## 备份与恢复

备份：

```bash
./scripts/backup.sh
```

恢复：

```bash
./scripts/restore.sh "/绝对路径/某次备份目录"
```

建议：

1. 每次大批量导入前做一次备份
2. 每次权限模板大改前做一次备份
3. 恢复前先停止服务，恢复后再重启
4. 正式环境建议启用每日自动备份

## 当前已具备的安全措施

1. A/B 仅能编辑自己录入的资料
2. C 仅能查看开放字段，无修改权限
3. 首次登录/重置后强制改密
4. 登录失败过多会临时锁定
5. 删除资料、停用账号、重置密码、停用 API 令牌都需要二次确认
6. 商品操作日志与管理审计日志都可追踪
