# 商品资料后台交付清单

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
7. 设置首个管理员：
   `CATALOG_BOOTSTRAP_ADMIN_USERNAME`
   `CATALOG_BOOTSTRAP_ADMIN_PASSWORD`
   `CATALOG_BOOTSTRAP_ADMIN_NAME`
8. 首次启动前执行：
   `chmod +x scripts/*.sh`
9. 启动服务：
   `./scripts/start.sh`
10. 健康检查：
   `http://127.0.0.1:8765/healthz`
11. 启动前后建议执行：
   `./scripts/check.sh`
12. 如果要常驻运行，可安装 launchd：
   `./scripts/install_launchd.sh`
13. 如果要开启每日自动备份：
   `./scripts/install_backup_launchd.sh`

## 首次验收

1. 用管理员账号登录
2. 立即修改管理员初始密码
3. 创建 A、B、C 正式账号
4. 进入 `/settings/c-fields` 配置 C 部门字段开放范围
5. 如有外部系统调用，生成 C 部门 API 令牌
6. 导入 1 份测试 Excel
7. 验证 A/B 只能编辑自己录入的数据
8. 验证 C 只能查看开放字段，且无法修改
9. 检查 `/logs` 是否已有管理审计记录

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
