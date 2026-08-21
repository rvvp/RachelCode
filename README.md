# 思安娜商品资料中心

这是一个围绕 `A / B / C` 三个部门协作场景搭建的本地轻量后台，用来替代共享 Excel 在多人协作、权限管理和资料调用上的不足。

## 商品企划中心

仓库现在同时包含独立的“商品企划中心”，用于商品部在上新前完成企划与定价决策。当前划分为两个业务板块：

- `品类企划`：第二阶段预留，用于新季开始前规划品类结构与各品类 SKU 数
- `上新定价`：当前可用，负责新款定价、价格带统计及结果回传

上新定价板块当前包含：

- 从藏宝阁读取已经由跟单部提交到商品部的新款资料
- 采购成本只读，统一采用藏宝阁的含税价
- 连衣裙不区分成本金额，使用固定倍率；其他品类按含税采购成本区间匹配倍率
- 定价规则支持按实际业务新增、编辑和删除；只有企划管理员可以维护，普通企划账号只读
- 在品类倍率基础上再乘供应商浮动系数
- 默认向下取最近的 `9` 结尾整数，例如 `150 × 4 × 1 = 600 → 599`
- 确认定价后发布品类和上新价格回藏宝阁
- 回传时核对藏宝阁资料版本，版本不一致时阻止覆盖
- 实时统计已确认和已发布款式的价格带分布
- 上新定价工作台按款色合并展示藏宝阁来源资料与定价结果，资料字段按年份季节、款号、款色、图片、商品名称、供应商、含税成本、来源状态排列
- 定价流程分为商品部确认后提交管理员审核，企划管理员可在审核阶段修改上新价，审核通过后才允许回传藏宝阁

开发环境可使用同一个随机 Token 启动两个服务：

```bash
CATALOG_PLANNING_API_TOKEN='replace-with-random-token' ./scripts/start.sh --port 8765
PLANNING_CATALOG_API_TOKEN='replace-with-random-token' ./scripts/start_planning.sh --port 8785
```

然后访问 `http://127.0.0.1:8785`。商品企划中心使用独立数据库 `planning_data/planning.db`，该目录已排除在 Git 之外。演示模式提供 `planner / demo123` 和 `planning_admin / demo123`；正式环境应设置 `PLANNING_SEED_DEMO=0`，并通过 `PLANNING_BOOTSTRAP_ADMIN_USERNAME`、`PLANNING_BOOTSTRAP_ADMIN_PASSWORD` 和 `PLANNING_BOOTSTRAP_ADMIN_NAME` 初始化首个管理员。

## 为什么推荐做软件，不建议继续只用共享文件

共享 Excel 适合单人维护或低权限复杂度场景，但不适合你这次的需求，原因很直接：

- `A` 和 `B` 需要同时录入，并且只能修改自己填写的资料
- `C` 只能查看部分字段，且没有任何修改权限
- `C` 还需要调用部分资料，这意味着后续最好能提供接口或结构化导出

这些能力在共享 Excel 里都很难稳定实现，尤其是“按录入人限制编辑”和“按字段限制可见性”。所以更合适的方式是：

- 用软件做权限和协作
- 继续保留 Excel 作为导入导出桥梁

## 当前版本包含的能力

- `A / B / C` 三类账号登录
- `系统管理员` 账号登录
- 管理员账号管理
- 用户首次登录/重置后强制改密
- 商品图片多图上传、预览与排序
- `C` 部门字段开放后台可配置
- `C` 部门字段开放支持命名模板保存、套用、删除和自动命中规则
- `C` 部门只读 API 令牌可由管理员生成和轮换
- 商品资料归档、删除与恢复
- 商品资料列表、详情、新建、编辑
- `A` 负责录入主体资料，`B` 只负责补充 `上新价格` 与 `上新渠道`
- `A` 只能编辑自己发起的主体资料，`B` 只能编辑被交接到自己阶段的两项字段
- `A` 完成后可提交给 `B`，`B` 完成后可开放给 `C`
- `系统管理员` 可人工干预流转、查看流转看板、处理批量完成与归档
- `系统管理员` 可创建账号、编辑角色分配、启用/停用账号、重置密码
- `A / B / ADMIN` 可在商品资料中上传多张本地图片、维护顺序，或继续填写图片链接
- `C` 只能查看已完成且开放字段，且开放字段可由管理员后台调整
- `A / B / ADMIN` 可管理资料生命周期：归档、删除、恢复
- `C` 的 Excel 导出和 JSON 接口同样只返回开放字段
- `/api/products` 支持网页登录或 C 部门 Bearer 令牌只读调用
- 每条资料都有独立操作日志，并支持字段级前后差异记录
- 单条资料日志支持筛选和 CSV 导出
- 提供跨商品的全局日志中心
- 管理员的账号、令牌、字段开放配置等管理动作会进入统一审计日志
- 管理员支持批量完成开放、批量归档
- 首页提供近 7 天运营看板
- 流转看板支持筛选与批量完成开放
- 资料详情页支持快捷复制与 JSON 调用片段
- 兼容桌面参考模板 `上新模板.xlsx` 的导入

## 字段基准

系统字段参考了桌面模板 `上新模板.xlsx`，包含以下信息：

- 商品基础：检测报告、发货仓库、品牌名称、年份季节、图片、款色、款号、颜色名称、商品名称、品类、是否有配饰
- 价格与供应：供应商、吊牌价、上新价格、上新渠道
- 尺码与数量：尺码段、F、S、M、L、XL、2XL、3XL、合计
- 材质与合规：材质、成分、洗涤方式、洗涤方式（英文）、安全技术类别、执行标准

其中对 `C` 默认开放的是商品基础中的对外可读字段，以及材质与合规信息；价格、供应商、仓库、检测报告和数量等敏感信息默认不开放。

## 如何启动

在项目目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install -r requirements.txt
./scripts/start.sh
```

启动后访问：

```text
http://127.0.0.1:8765
```

默认上传目录：

```text
/Users/apple/Documents/商品资料后台/data/uploads
```

默认演示账号：

- `a_editor / demo123`
- `b_editor / demo123`
- `c_viewer / demo123`
- `admin_reviewer / demo123`

也支持通过环境变量或启动参数调整：

- `CATALOG_HOST`
- `CATALOG_PORT`
- `CATALOG_DB`
- `CATALOG_UPLOADS`
- `CATALOG_BACKUP_KEEP`
- `CATALOG_BACKUP_HOUR`
- `CATALOG_BACKUP_MINUTE`
- `CATALOG_SEED_DEMO`
- `CATALOG_SEED_SAMPLES`
- `CATALOG_BOOTSTRAP_ADMIN_USERNAME`
- `CATALOG_BOOTSTRAP_ADMIN_PASSWORD`
- `CATALOG_BOOTSTRAP_ADMIN_NAME`
- `CATALOG_BOOTSTRAP_ADMIN_FORCE_RESET`
- `CATALOG_BRAND_NAME`
- `CATALOG_BRAND_MARK`
- `CATALOG_BRAND_TAGLINE`
- `CATALOG_BRAND_SUBTITLE`
- `CATALOG_BRAND_EYEBROW`
- `CATALOG_CONSOLE_EYEBROW`
- `CATALOG_BRAND_ACCENT`
- `CATALOG_BRAND_ACCENT_STRONG`
- `CATALOG_BRAND_ACCENT_DEEP`

推荐做法：

1. 复制 `.env.example` 为 `.env`
2. 创建 `.venv` 并安装 `requirements.txt`
3. 按你的正式环境修改数据库路径、上传目录和管理员初始化信息
4. 用 `scripts/start.sh` 启动
5. 用 `/healthz` 检查服务是否正常

如果是“开发目录”发布到“正式运行目录”的本机模式，后续建议不要手工覆盖正式目录，而是使用：

```bash
./scripts/deploy_local.sh
```

这个脚本会保护正式运行目录中的 `.env`、`data/`、`logs/`、`backups/` 和 `.venv`，避免把正式数据库路径、上传目录或日志目录覆盖坏。

例如正式试跑时，可以直接这样启动：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install -r requirements.txt
CATALOG_SEED_DEMO=0 \
CATALOG_SEED_SAMPLES=0 \
CATALOG_BOOTSTRAP_ADMIN_USERNAME=owner_admin \
CATALOG_BOOTSTRAP_ADMIN_PASSWORD='ChangeMe123' \
CATALOG_BOOTSTRAP_ADMIN_NAME='系统管理员' \
./scripts/start.sh --host 0.0.0.0 --port 8765
```

这时系统不会写入演示账号和演示商品，而是只创建一个首个管理员账号，更适合内部试运行。

## 品牌化定制

当前版本已经支持把界面里的品牌信息做成环境变量配置，不需要改代码就能切成你们内部交付版。

常用配置项：

- `CATALOG_BRAND_NAME`
  系统名称，例如 `思安娜的藏寶閣`
- `CATALOG_BRAND_MARK`
  头部角标简称，例如 `Sienna`
- `CATALOG_BRAND_TAGLINE`
  登录页主文案
- `CATALOG_BRAND_SUBTITLE`
  登录页副文案
- `CATALOG_BRAND_EYEBROW`
  登录页英文副标题
- `CATALOG_CONSOLE_EYEBROW`
  后台首页英文副标题
- `CATALOG_BRAND_ACCENT`
  主色
- `CATALOG_BRAND_ACCENT_STRONG`
  深主色
- `CATALOG_BRAND_ACCENT_DEEP`
  辅助色

例如：

```bash
CATALOG_BRAND_NAME="思安娜的\n藏寶閣" \
CATALOG_BRAND_MARK="Sienna" \
CATALOG_BRAND_TAGLINE="让商品资料从分散表格进入统一底库" \
CATALOG_BRAND_SUBTITLE="面向商品、运营与渠道协作的内部资料后台，支持 A 主体填写、B 补充渠道价格、C 只读开放与结构化调用。" \
CATALOG_BRAND_EYEBROW="Sienna Treasure Pavilion" \
CATALOG_CONSOLE_EYEBROW="Sienna Treasure Workspace" \
CATALOG_BRAND_ACCENT="#bc6c25" \
CATALOG_BRAND_ACCENT_STRONG="#7f3b08" \
CATALOG_BRAND_ACCENT_DEEP="#355f52" \
./scripts/start.sh
```

如果后面你们有公司正式名称、品牌缩写、主色规范，直接填进 `.env` 即可。

## 部署辅助文件

当前目录已经补了几份可直接用的交付文件：

- `.env.example`
  用来生成 `.env` 配置文件
- `requirements.txt`
  Python 依赖清单
- `DEPLOYMENT_CHECKLIST.md`
  用来做上线前检查、巡检和交接
- `DELIVERY_PACKAGE.md`
  交付包生成和移交流程说明
- `FINAL_HANDOFF_SIANA.md`
  思安娜商品资料中心的最终交付说明
- `scripts/start.sh`
  统一启动脚本，优先使用 `.venv/bin/python3`，也支持 `PYTHON_BIN`
- `scripts/check.sh`
  启动前后自检脚本，会检查 `.env`、数据库、上传目录、代码编译和健康接口
- `scripts/backup.sh`
  备份当前数据库和上传目录
- `scripts/restore.sh <备份目录>`
  从某次备份恢复数据库和上传目录
- `scripts/package_release.sh`
  生成可交付压缩包
- `scripts/deploy_local.sh`
  把开发目录安全同步到本机正式运行目录，默认不覆盖 `.env`、数据和日志
- `launchd/com.catalogbackend.backup.plist.template`
  macOS 定时备份模板
- `launchd/com.catalogbackend.app.plist.template`
  macOS 常驻服务模板
- `scripts/install_launchd.sh`
  安装为当前用户的开机自启服务
- `scripts/uninstall_launchd.sh`
  卸载开机自启服务
- `scripts/launchd_status.sh`
  查看 launchd 装载状态
- `scripts/install_backup_launchd.sh`
  安装每日自动备份任务
- `scripts/uninstall_backup_launchd.sh`
  卸载每日自动备份任务
- `scripts/backup_launchd_status.sh`
  查看自动备份任务状态

建议的日常操作方式：

```bash
cd "/Users/apple/Documents/商品资料后台"
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install -r requirements.txt
cp .env.example .env
chmod +x scripts/*.sh
./scripts/check.sh
./scripts/start.sh
```

备份示例：

```bash
./scripts/backup.sh
```

如需自动备份：

```bash
./scripts/install_backup_launchd.sh
./scripts/backup_launchd_status.sh
```

如需生成交付包：

```bash
./scripts/package_release.sh
```

恢复示例：

```bash
./scripts/restore.sh "/Users/apple/Documents/商品资料后台/backups/20260614-120000"
```

健康检查示例：

```text
http://127.0.0.1:8765/healthz
```

## macOS 常驻运行

如果你希望这套后台在 Mac 上长期运行，而不是每次手动执行启动命令，可以直接使用 `launchd`：

```bash
chmod +x scripts/*.sh
./scripts/install_launchd.sh
./scripts/launchd_status.sh
```

卸载：

```bash
./scripts/uninstall_launchd.sh
```

卸载自动备份：

```bash
./scripts/uninstall_backup_launchd.sh
```

默认日志输出目录：

```text
/Users/apple/Documents/商品资料后台/logs
```

## 如何使用

1. 用 `A` 账号登录，录入商品主体资料或导入 Excel
2. 系统会自动把资料绑定到当前发起账号，资料发起部门默认为 `A`
3. `A` 录入后的资料默认为 `A填写中`
4. `A` 可以把资料提交为 `待B填写`，并填写交接说明
5. `B` 登录后只会看到 `上新价格` 与 `上新渠道` 两个可编辑字段
6. `B` 完成后可把资料标记为 `已完成` 并开放给 `C`
7. `系统管理员` 可以在流转看板里查看待B填写资料，并在必要时人工干预流转
8. `C` 账号登录后，只能查看 `已完成` 且开放的字段，也可以通过 `/api/products` 读取 JSON 数据
9. `A / B / 管理员` 可以在资料详情页查看该条记录的操作日志和字段变更前后差异
10. 日志页支持按动作、操作人筛选，并可把筛选结果导出为 CSV
11. `A / B / 管理员` 还可以进入“日志中心”查看当前账号有权限看到的全局操作日志
12. `系统管理员` 可以在资料列表页批量完成并开放给 C，或批量归档资料
13. `A / B / 管理员` 首页会展示近 7 天新增、近 7 天操作量、待B填写量等运营看板
14. `系统管理员` 可以在流转看板里按发起部门、关键词筛选待B填写资料，并支持批量完成开放
15. 资料详情页可以直接复制商品核心信息、当前详情 JSON、图片地址
16. `系统管理员` 可以进入 `/users` 管理后台账号
17. `系统管理员` 可以进入 `/settings/c-fields` 配置 C 部门可见字段，并把常用开放组合保存成模板，按发起部门/品牌/品类/关键词自动切换
18. `A / B / 管理员` 可以在资料详情页管理资料生命周期
19. 所有账号都可以在顶部导航进入“修改密码”，管理员重置后的账号会被强制先改密再继续使用
20. 管理员可以在字段开放页生成或停用 C 部门 API 令牌，供外部系统只读调用资料

## 账号管理

当前版本已经支持管理员在后台直接管理账号。

管理员可以做的事：

- 创建新账号
- 指定账号属于 `A / B / C / ADMIN`
- 修改显示名称
- 停用或重新启用账号
- 重置账号密码
- 设置“首次登录后需要修改密码”

默认入口：

- 登录管理员账号后，顶部导航点击 `账号管理`

当前规则：

- 被停用的账号不能登录
- 连续输错密码过多会被临时锁定登录
- 管理员不能停用自己当前正在使用的账号
- 重置密码后，默认要求用户下次登录后修改密码
- 被设置为“首次登录需改密”的账号，登录后会先跳转到修改密码页，完成后才可继续访问系统
- 停用账号前需要输入 `DISABLE` 确认
- 重置密码前需要输入 `RESET` 确认
- 删除资料前需要输入 `DELETE` 确认
- 停用 C 部门 API 令牌前需要输入 `DISABLE` 确认

## 图片上传

当前版本的 `图片` 字段同时支持两种方式：

- 直接填写图片链接
- 上传本地图片文件
- 一次上传多张图片并维护顺序

上传后的图片会：

- 保存到本地上传目录
- 第一张自动写入商品资料的 `图片` 主字段
- 同步保存为有序图片组，供详情页和 JSON 接口调用
- 在录入页和详情页中直接预览
- 通过系统内部的 `/media/...` 路径访问

当前支持格式：

- `JPG / JPEG`
- `PNG`
- `WEBP`
- `GIF`

当前限制：

- 单张图片最大 `5MB`
- 多图顺序以编辑页中从上到下的顺序为准

## C 部门字段开放设置

当前版本里，`C` 部门可见字段已经不是写死在代码里，而是可以由管理员在后台配置。

入口：

- 登录管理员账号后，顶部导航点击 `字段开放`

管理员可以：

- 勾选允许 `C` 部门查看的字段
- 把当前勾选保存为命名模板
- 一键套用已有模板
- 删除不再需要的模板
- 为模板补充自动命中规则，例如按发起部门、品牌、品类、款号关键词自动切换
- 保存后立即生效

配置会同步影响：

- `C` 部门商品列表页
- `C` 部门商品详情页
- `C` 部门 Excel 导出
- `C` 部门 JSON 接口 `/api/products`
- 当某个商品命中模板规则时，会优先按命中的模板返回字段；未命中时再回退到默认开放字段

这意味着以后如果你想让 `C` 多看一个字段，或者隐藏一个字段，不需要再改代码。

当前版本还支持在同一页面管理 C 部门 API 令牌：

- 管理员可以生成令牌
- 管理员可以重新轮换令牌
- 管理员可以停用令牌
- 外部系统可以用 `Authorization: Bearer <token>` 或 `?access_token=<token>` 调用 `/api/products`
- 无论通过网页登录还是令牌调用，返回字段都只会按 C 部门开放范围输出

## 资料生命周期

当前版本已经支持资料的生命周期管理，和“协作阶段状态”分开存在。

协作阶段状态：

- `A填写中`
- `待B填写`
- `已完成`

生命周期状态：

- `正常`
- `已归档`
- `已删除`

当前规则：

- `A / B` 可以归档自己录入的资料
- `A / B / 管理员` 可以把资料标记为删除
- `管理员` 可以恢复已删除资料
- `C` 只能看到“正常且已完成”的资料
- 被归档或删除的资料会保留日志，不会直接从数据库彻底抹掉

这样做的好处是更安全：

- 误操作可以恢复
- 历史记录不会丢
- 审计链可以保留完整

## 状态流转

当前版本的状态流转为：

- `A填写中`
- `待B填写`
- `已完成`

规则如下：

- `A` 新建资料后默认进入 `A填写中`
- `A` 可以把自己发起的资料提交到 `待B填写`
- `A` 提交时可以附带交接说明
- `B` 只能补充 `上新价格` 和 `上新渠道`
- `B` 完成后可以把资料标记为 `已完成`
- `B` 也可以把资料退回 `A填写中`
- `系统管理员` 可以在必要时把资料在三个阶段之间人工流转
- `A` 再次编辑已完成资料时，会回到 `A填写中`
- `B` 再次编辑已完成资料时，会回到 `待B填写`

这样可以避免“已完成内容被悄悄修改却没有重新进入协作流转”的问题。

## 操作日志

每条商品资料都会记录关键动作，例如：

- 创建资料
- 更新资料
- 提交给B填写
- 填写完成，开放给C
- 退回A补充

日志里会保留操作时间、操作人、部门、动作和说明，便于多人协作追踪。

对于状态流转，日志还会额外记录：

- `A` 填写的交接说明
- `B` 或 `系统管理员` 填写的完成/处理说明
- 字段修改前后差异

当前日志页还支持：

- 按动作关键字筛选
- 按操作人或部门关键字筛选
- 将当前筛选结果导出为 CSV

当前版本还提供“全局日志中心”：

- `管理员` 可以查看全部商品的操作日志
- `A / B` 只能查看自己录入商品的操作日志
- 支持按商品、动作、操作人筛选
- 支持导出当前筛选结果为 CSV

## 批量操作

当前版本里，管理员已经可以在资料列表页进行基础批量操作：

- 批量完成并开放给 C 的资料
- 批量归档可归档资料
- 不符合当前操作条件的资料会自动跳过

这样在资料量变大以后，管理员不需要逐条点开详情页处理。

## 流转看板

当前流转看板除了单条进入详情页处理，还支持：

- 按商品关键词筛选
- 按 A 部门发起资料筛选
- 默认按最近提交时间优先查看
- 勾选多条待B填写资料后批量完成并开放给 C

## 详情页快捷操作

当前资料详情页还支持快速复制：

- 商品核心信息摘要
- 当前资料详情 JSON
- 图片地址

这样在和同事沟通、发给供应链、或者给外部系统联调时，不需要手动再整理一遍资料内容。

## 首页看板

当前首页除了总量统计，还增加了近 7 天运营概览：

- 近 7 天新增资料数
- 近 7 天操作日志总量
- 当前待 B 填写资料数
- A / B 部门近 7 天各自新增量

这样管理者打开后台首页时，就能更快判断最近资料录入和 A/B 交接是否积压。

对于“更新资料”这类动作，当前版本还会额外记录：

- 哪个字段被修改了
- 修改前是什么
- 修改后是什么

这样在多人协作时，就不只知道“谁改了这条资料”，还知道“具体改了哪些内容”。

## Excel 导入规则

- 读取第一张工作表
- 读取第一行表头并按参考模板匹配字段
- 如果发现“同一录入人”下已有相同 `款号 + 颜色名称 + 商品名称` 的记录，则更新
- 否则新增一条资料

## 当前实现方式

- 后端：Python 标准库 WSGI + SQLite
- Excel：`openpyxl`
- 前端：服务端渲染 HTML

这样做的好处是依赖少、启动快，适合先把协作流程和权限模型跑通。
