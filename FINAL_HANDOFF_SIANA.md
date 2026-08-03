# 思安娜商品资料中心交付说明

## 当前交付状态

当前目录已经是一套可直接运行的本地交付版，系统品牌默认配置为：

- 系统名称：`思安娜商品资料中心`
- 头部简称：`SA`
- 登录页英文副标题：`Siana Merchandise Console`
- 后台英文副标题：`Unified Catalog Workspace`

当前默认配置已经写入：

- [.env](/Users/apple/Documents/商品资料后台/.env)
- [.env.example](/Users/apple/Documents/商品资料后台/.env.example)

## 一键启动

在项目目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install -r requirements.txt
chmod +x scripts/*.sh
./scripts/check.sh
./scripts/start.sh
```

启动后访问：

```text
http://127.0.0.1:8765
```

## 当前演示账号

- `a_editor / demo123`
- `b_editor / demo123`
- `c_viewer / demo123`
- `admin_reviewer / demo123`

## 权限说明

- `A`
  发起并填写主体资料字段，完成后转交 `B`
- `B`
  只填写 `上新价格` 和 `上新渠道`，完成后开放给 `C`
- `ADMIN`
  账号管理、字段开放、批量操作、日志中心，以及必要时人工干预流转
- `C`
  只读查看已完成且开放的字段，可导出 Excel，可调用 JSON，无修改权限

## 推荐验收顺序

1. 用 `admin_reviewer` 登录，检查首页、流转看板、账号管理、字段开放
2. 用 `a_editor` 登录，检查新建资料、编辑资料、图片上传、提交给 B
3. 用 `b_editor` 登录，检查只显示 `上新价格` 与 `上新渠道`，并完成开放给 C
4. 用 `c_viewer` 登录，检查只读列表、只读详情、Excel 导出、JSON 调用
5. 访问 `/healthz`，确认服务状态正常
6. 执行一次 `./scripts/backup.sh`，确认备份链路可用

## 推荐演示路径

如果要面对业务同事或管理层做现场演示，建议按下面顺序操作：

1. 先打开登录页，说明系统是围绕 `A -> B -> C` 三部门协作设计的
2. 用 `a_editor` 演示主体资料录入，只能填写 A 阶段字段
3. 提交到 `待B填写` 后，用 `b_editor` 演示只看到 `上新价格` 和 `上新渠道`
4. B 完成后，用 `c_viewer` 演示只读查看，以及仅返回开放字段的 Excel / JSON
5. 最后用 `admin_reviewer` 演示流转看板、字段开放配置、日志中心和人工干预能力

## 交付包生成

```bash
./scripts/package_release.sh
```

生成目录：

```text
dist/
```

## 常用运维脚本

- `./scripts/start.sh`
- `./scripts/check.sh`
- `./scripts/deploy_local.sh`
- `./scripts/backup.sh`
- `./scripts/restore.sh <备份目录>`
- `./scripts/install_launchd.sh`
- `./scripts/install_backup_launchd.sh`

## 补充说明

- 这套系统以软件方式替代共享 Excel，多人协作、权限分层和只读调用都已经落地
- Excel 仍然保留为导入导出桥梁，兼容现有模板使用习惯
- 当前品牌、主色、欢迎文案都可继续通过 `.env` 调整
- 如果继续在本机边开发边发布，后续统一使用 `./scripts/deploy_local.sh`，不要直接全量覆盖正式运行目录
