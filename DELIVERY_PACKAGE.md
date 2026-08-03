# 思安娜商品资料中心交付说明

## 交付内容

当前目录已经具备一套可直接移交的本地部署版本，核心包括：

- `app.py`
  应用启动入口
- `catalog_backend/`
  业务代码、权限规则、Excel 处理、上传处理
- `requirements.txt`
  Python 依赖清单
- `.env.example`
  正式环境配置模板
- `scripts/start.sh`
  启动脚本，优先使用 `.venv/bin/python3`，也支持 `PYTHON_BIN`
- `scripts/check.sh`
  环境自检、编译检查、健康检查
- `scripts/backup.sh`
  手动备份数据库和上传文件
- `scripts/restore.sh`
  从备份恢复
- `scripts/package_release.sh`
  生成交付压缩包
- `scripts/deploy_local.sh`
  本机开发目录安全同步到正式运行目录，默认保护 `.env`、数据库、上传目录和日志
- `scripts/install_launchd.sh`
  安装为当前用户的 macOS 常驻服务
- `scripts/install_backup_launchd.sh`
  安装每日自动备份任务
- `README.md`
  使用与运维说明
- `DEPLOYMENT_CHECKLIST.md`
  上线和交接检查单
- `FINAL_HANDOFF_SIANA.md`
  思安娜商品资料中心的最终交付说明

## 建议交付方式

推荐交付时执行一次：

```bash
chmod +x scripts/*.sh
./scripts/package_release.sh
```

生成文件位于：

```text
dist/
```

把最新的 `tar.gz` 文件交付给部署方即可。

如果是当前这台电脑继续迭代并同步到本机正式运行目录，推荐不要手写 `rsync`，统一使用：

```bash
./scripts/deploy_local.sh
```

这样不会覆盖正式运行目录里的 `.env` 和数据目录。

## 部署方落地步骤

```bash
tar -xzf catalog-backend-delivery-时间戳.tar.gz
cd catalog-backend-delivery-时间戳
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install -r requirements.txt
cp .env.example .env
chmod +x scripts/*.sh
./scripts/check.sh
./scripts/start.sh
```

## 正式环境建议

- 设置 `CATALOG_SEED_DEMO=0`
- 设置 `CATALOG_SEED_SAMPLES=0`
- 配置首个管理员账号
- 按内部品牌规范填写 `.env` 里的品牌配置项
- 把数据库和上传目录放在可备份路径
- 本机双目录模式下，代码目录和数据目录要分离，避免正式服务直接读取 `Documents` 下的开发数据库
- 启用 `launchd` 常驻运行和定时备份
- 首次验收时验证 A、B、C 三类权限边界

## 交付定制建议

如果这套系统要交给内部团队正式使用，建议在交付前至少补齐下面这些配置：

- `CATALOG_BRAND_NAME`
- `CATALOG_BRAND_MARK`
- `CATALOG_BRAND_TAGLINE`
- `CATALOG_BRAND_SUBTITLE`
- `CATALOG_BRAND_EYEBROW`
- `CATALOG_CONSOLE_EYEBROW`
- `CATALOG_BRAND_ACCENT`
- `CATALOG_BRAND_ACCENT_STRONG`
- `CATALOG_BRAND_ACCENT_DEEP`

当前仓库默认已经对齐为 `思安娜商品资料中心`，如果后续还要换成你们正式内部品牌，也可以继续通过这些配置项调整。
