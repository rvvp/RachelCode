"""Server rendered, permission-checked phase-one rotation workspace."""
from __future__ import annotations

import cgi
import hashlib
import hmac
import html
import json
import math
from pathlib import Path
from time import monotonic
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from planning_center import rotation as rot
from planning_center.db import get_connection
from catalog_backend.concurrency import FileSlotPool, TaskCapacityError


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


class RotationViews:
    def rotation_readiness(self):
        return rot.readiness(self.db_path)

    def rotation_csrf(self, user):
        return hmac.new(str(user["password_hash"]).encode(), ("rotation:" + str(user["id"])).encode(), hashlib.sha256).hexdigest()

    def rotation_hidden(self, user):
        return f'<input type="hidden" name="csrf" value="{self.rotation_csrf(user)}">'

    def rotation_download(self, start_response, content, filename):
        start_response("200 OK", [("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), ("Content-Disposition", f'attachment; filename="{filename}"'), ("Content-Length", str(len(content))), ("Cache-Control", "no-store")])
        return [content]

    def sync_rotation(self, user):
        pool = FileSlotPool(Path(self.db_path).parent / ".runtime-locks", "rotation-sync", 1, wait_seconds=0)
        try:
            with pool.acquire():
                return self._sync_rotation_unlocked(user)
        except TaskCapacityError as error:
            raise ValueError("已有轮转同步正在进行，请稍后重试；现有资料未受影响。") from error

    def _sync_rotation_unlocked(self, user):
        if not self.catalog_api_token:
            raise ValueError("尚未配置藏宝阁内部 Token，无法同步轮转资料。")
        items, after, revision = [], 0, None
        started = monotonic()
        for _ in range(10000):
            if monotonic() - started > 180:
                raise ValueError("轮转同步超过三分钟，未更新本地资料，请稍后重试。")
            request = Request(self.catalog_api_url + "/api/internal/planning/rotation-products?" + urlencode({"after": after, "limit": 500}), headers={"Authorization": "Bearer " + self.catalog_api_token})
            try:
                with urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read(12 * 1024 * 1024 + 1))
            except Exception as error:
                raise ValueError("轮转资料同步失败，原有资料未改动。请检查藏宝阁服务后重试。") from error
            if payload.get("schema_version") != 1 or not isinstance(payload.get("items"), list):
                raise ValueError("藏宝阁轮转接口版本不匹配，原有资料未改动。")
            if not payload.get("revision"):
                raise ValueError("轮转接口缺少版本校验，原有资料未改动。")
            if revision is not None and revision != payload["revision"]:
                raise ValueError("同步期间藏宝阁资料发生更新，未保存不完整快照；请重试同步。")
            revision = payload["revision"]
            items.extend(payload["items"])
            if not payload.get("has_more"):
                return rot.sync_sources(self.db_path, items, user["display_name"])
            cursor = int(payload.get("next_after") or 0)
            if cursor <= after:
                raise ValueError("轮转接口分页异常，原有资料未改动。")
            after = cursor
        raise ValueError("资料超过单次同步上限，请联系维护人员。")

    def handle_rotation(self, environ, start_response, user, path, method, query_values):
        if user.get("role") not in {"planner", "admin"}:
            raise PermissionError("只有商品部初审人员和企划管理员可以使用货品轮转。")
        query = {k: v if k in {"supplier", "category"} else v[0] for k, v in query_values.items()}
        if method == "GET":
            if path == "/rotation":
                return self.html_response(start_response, self.render_rotation(user, query))
            if path == "/rotation/rules":
                return self.html_response(start_response, self.render_rotation_rules(user))
            if path == "/rotation/sales-template.xlsx":
                return self.rotation_download(start_response, rot.workbook_bytes(rot.SALES_HEADERS, [], "轮转销量"), "rotation-sales-template.xlsx")
            parts = path.strip("/").split("/")
            if len(parts) in {2, 3} and parts[1].isdigit():
                plan = rot.plan_detail(self.db_path, int(parts[1]))
                if len(parts) == 2:
                    return self.html_response(start_response, self.render_rotation_plan(user, plan))
                if parts[2] == "export.xlsx":
                    headers = ("方案ID", "方案状态", "款号", "全部款色", "来源商品ID", "当前渠道（来源）", "目标渠道（人工执行）", "接收参考日期", "参考天数", "销售期间净销量", "天猫净销量", "唯品净销量", "人工执行时间", "执行说明")
                    rows = []
                    for item in plan["items"]:
                        if not item["active"]:
                            continue
                        s = item["snapshot"]
                        rows.append((plan["id"], rot.STATE_LABELS[plan["state"]], item["style_code"], "、".join(s["colors"]), ",".join(map(str, s["source_ids"])), s["channel"], item["target"], str(rot.local_day(s["reference_at"]) or ""), s["age_days"], s["sales_quantity"] if s["sales_quantity"] is not None else "缺失／未使用销量条件", s["sales"].get("tmall", {}).get("quantity", "缺失"), s["sales"].get("vip", {}).get("quantity", "缺失"), item["executed_at"], item["feedback"]))
                    return self.rotation_download(start_response, rot.workbook_bytes(headers, rows, "轮转执行清单"), f"rotation-plan-{plan['id']}.xlsx")
        if method == "POST":
            if path == "/rotation/sales-import":
                length = int(environ.get("CONTENT_LENGTH") or 0)
                if length <= 0 or length > 12 * 1024 * 1024 or not str(environ.get("CONTENT_TYPE") or "").startswith("multipart/form-data"):
                    raise ValueError("请上传 10MB 以内的销量 .xlsx 文件。")
                storage = cgi.FieldStorage(fp=environ["wsgi.input"], environ=environ, keep_blank_values=True)
                csrf = storage.getfirst("csrf", "")
                if not hmac.compare_digest(csrf, self.rotation_csrf(user)):
                    raise PermissionError("页面验证失效，请刷新后重试。")
                upload = storage["workbook"] if "workbook" in storage else None
                if upload is None or isinstance(upload, list) or not getattr(upload, "filename", "").lower().endswith(".xlsx"):
                    raise ValueError("请选择有效的 .xlsx 文件。")
                content = upload.file.read(10 * 1024 * 1024 + 1)
                count = rot.import_sales(self.db_path, content, user["display_name"], storage.getfirst("replace") == "1")
                return self.redirect(start_response, "/rotation?notice=" + self.q(f"已导入 {count} 条整款／体系销量，历史批次永久保留。"))
            values = self.parse_form_values(environ)
            form = {k: v[0] for k, v in values.items()}
            if not hmac.compare_digest(form.get("csrf", ""), self.rotation_csrf(user)):
                raise PermissionError("页面验证失效，请刷新后重试。")
            if path == "/rotation/sync":
                count = self.sync_rotation(user)
                return self.redirect(start_response, "/rotation?notice=" + self.q(f"已读取 {count} 条藏宝阁资料及接收记录；未修改原商品资料和审核流程。"))
            if path == "/rotation/create":
                criteria = json.loads(form.get("criteria") or "{}")
                selected = None if form.get("scope") == "filtered" else values.get("style_code", [])
                plan_id = rot.create_plan(self.db_path, criteria, selected, form.get("target", ""), user["display_name"], form.get("signature", ""))
                return self.redirect(start_response, f"/rotation/{plan_id}")
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[1].isdigit() and parts[2] == "action":
                if not form.get("plan_signature"):
                    raise ValueError("缺少方案版本校验，请刷新页面后重试。")
                if form.get("action") == "confirm":
                    if user.get("role") != "admin":
                        raise PermissionError("轮转方案由企划管理员确认。")
                    self.sync_rotation(user)
                rot.update_plan(self.db_path, int(parts[1]), form.get("action"), user, form)
                return self.redirect(start_response, f"/rotation/{parts[1]}")
        raise LookupError("轮转页面或操作不存在。")

    def rotation_frame(self, user, title, body):
        css = """<style>.rot-head{display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap}.rot-tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.rot-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.rot-grid label{display:flex;flex-direction:column;gap:6px;font-size:13px;font-weight:600}.rot-grid input,.rot-grid select{width:100%;min-width:0}.rot-scroll{overflow:auto;max-width:100%}.rot-table{width:100%;border-collapse:collapse;white-space:nowrap;font-size:13px}.rot-table td,.rot-table th{padding:13px 12px;border-bottom:1px solid var(--line);text-align:left}.rot-table th{background:#f1f4f0}.rot-table input[type=checkbox],.rot-tools input[type=checkbox]{width:17px;min-width:17px}.rot-table select{min-width:120px}.rot-note{color:var(--muted);font-size:13px;line-height:1.8}.rot-pages{display:flex;justify-content:center;gap:12px;align-items:center;margin:20px 0}.rot-grid select[multiple]{height:90px}.rot-panel{margin-top:20px}.rot-feedback{width:100%;min-height:70px;margin:12px 0}.rot-table td small{display:block;color:var(--muted)}@media(max-width:1000px){.rot-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:600px){.rot-grid{grid-template-columns:1fr}.rot-tools>*{max-width:100%}.rot-head{align-items:flex-start}.rot-panel{padding:16px}}</style>"""
        css += '<style>header nav a{flex-shrink:0;white-space:nowrap}header nav{overflow-x:auto}</style>'
        heading = f'<section class="page-heading rot-head"><div><div class="eyebrow">MERCHANDISE ROTATION / PHASE 1</div><h1>{esc(title)}</h1><p>整款跨体系轮转 · 系统提供方案，商品部人工执行</p></div><div class="rot-tools"><a class="button" href="/rotation">货品轮转</a><a class="button" href="/rotation/rules">轮转规则</a></div></section>'
        return self.shell(title, css + heading + body, user, "rotation")

    def render_rotation(self, user, query):
        criteria = rot.clean_criteria(query)
        rows = rot.candidates(self.db_path, criteria)
        with get_connection(self.db_path) as connection:
            styles = rot.grouped_styles(connection)
            sync = connection.execute("SELECT * FROM rotation_syncs ORDER BY id DESC LIMIT 1").fetchone()
            plans = connection.execute("SELECT p.*, (SELECT COUNT(*) FROM rotation_plan_items i WHERE i.plan_id=p.id AND i.active=1) AS item_count FROM rotation_plans p ORDER BY id DESC LIMIT 100").fetchall()
            batches = connection.execute("SELECT * FROM rotation_sales_batches ORDER BY id DESC LIMIT 5").fetchall()
        hidden = self.rotation_hidden(user)
        body = self.alert(str(query["notice"]), "success") if query.get("notice") else ""
        body += f'<section class="panel rot-panel"><div class="rot-head"><div><h2>资料与销量</h2><p class="rot-note">最近同步：{esc(sync["synced_at"] if sync else "尚未同步")}。同步为独立只读资料池，不将已完成资料送回上新审核。</p></div><form method="post" action="/rotation/sync">{hidden}<button class="primary">同步轮转资料</button></form></div><details><summary>导入真实销量</summary><p class="rot-note">模板每行为“整款 + 天猫体系／唯品体系 + 完全一致的日期区间 + 净销量”。请先在业务报表中汇总全部款色、天猫店铺；不导入库存。缺失不当作零，重复／重叠期间不相加。可显式替换相同期间，旧批次仍保留。</p><form class="rot-tools" method="post" action="/rotation/sales-import" enctype="multipart/form-data">{hidden}<a class="button" href="/rotation/sales-template.xlsx">下载销量模板</a><input type="file" name="workbook" accept=".xlsx" required><label><input type="checkbox" name="replace" value="1">替换完全相同期间</label><button>导入销量</button></form><p class="rot-note">{esc("；".join(str(b["created_at"]) + " 导入 " + str(b["row_count"]) + " 行" for b in batches) or "暂无销量批次")}</p></details></section>'

        def options(items, selected, all_option=True):
            selected = selected if isinstance(selected, (list, tuple)) else [selected]
            return ('<option value="">全部</option>' if all_option else '') + ''.join(f'<option value="{esc(v)}" {"selected" if v in selected else ""}>{esc(v)}</option>' for v in sorted(set(items)))

        filters = '<form method="get" action="/rotation"><div class="rot-grid">'
        filters += '<label>年份季节<select name="season_year">' + options([v for s in styles for v in s["season_years"]], criteria["season_year"]) + '</select></label>'
        filters += '<label>当前渠道<select name="channel">' + options(rot.CHANNELS, criteria["channel"]) + '</select></label>'
        filters += '<label>判断条件<select name="mode">' + ''.join(f'<option value="{v}" {"selected" if criteria["mode"] == v else ""}>{label}</option>' for v, label in (("time", "仅接收时长"), ("sales", "仅净销量"), ("both", "时长且净销量"))) + '</select></label>'
        filters += f'<label>接收至少自然日<input type="number" name="min_days" min="0" max="10000" value="{criteria["min_days"]}"></label>'
        for name, label, key in (("supplier", "供应商（多选）", "suppliers"), ("category", "品类（多选）", "categories")):
            filters += f'<label>{label}<select multiple name="{name}">' + options([v for s in styles for v in s[key]], criteria[name], False) + '</select></label>'
        filters += f'<label>销量开始日期<input type="date" name="start_date" value="{esc(query.get("start_date", ""))}"></label><label>销量结束日期<input type="date" name="end_date" value="{esc(query.get("end_date", ""))}"></label><label>期间净销量不高于<input type="number" name="max_sales" value="{esc(query.get("max_sales", "0"))}"></label></div><div class="rot-tools" style="margin-top:16px"><button class="primary">筛选并预览</button><a href="/rotation">清除筛选</a></div></form>'
        excluded = sum(bool(s["reason"]) for s in styles)
        body += f'<section class="panel rot-panel"><h2>批量筛选</h2>{filters}<p class="rot-note">接收时间只是上新参考，不代表实际开售日期。多选按住 Ctrl／Command；未选表示全部。当前同步 {len(styles)} 款，其中 {excluded} 款因召回／接收／渠道条件暂不参与。</p></section>'
        page = min(max(1, int(query.get("page") or 1)), max(1, math.ceil(len(rows) / 50)))
        page_rows = rows[(page - 1) * 50: page * 50]
        table = ''
        for s in page_rows:
            sales_label = str(s["sales_quantity"]) if s["sales_quantity"] is not None else "缺失／未使用销量条件"
            table += f'<tr><td><input type="checkbox" name="style_code" value="{esc(s["style_code"])}"></td><td><strong>{esc(s["style_code"])}</strong><small>{esc("、".join(s["colors"]))}</small></td><td>{esc("、".join(s["suppliers"]))}</td><td>{esc("、".join(s["categories"]))}</td><td>{esc(s["channel"])}</td><td>{esc(rot.local_day(s["reference_at"]))}<small>{s["age_days"]} 自然日</small></td><td>{esc(sales_label)}</td></tr>'
        serialized = esc(json.dumps(criteria, ensure_ascii=False))
        body += f'<section class="panel rot-panel"><div class="rot-head"><h2>候选货品 · 合计 {len(rows)} 款（整款）</h2><span class="rot-note">已在草案／执行中的款式不重复推荐</span></div><form method="post" action="/rotation/create">{hidden}<input type="hidden" name="criteria" value="{serialized}"><input type="hidden" name="signature" value="{rot.fingerprint(rows)}"><div class="rot-tools"><label><input type="checkbox" onclick="document.querySelectorAll(\'[name=style_code]\').forEach(c=>c.checked=this.checked)">勾选本页</label><label><input type="checkbox" name="scope" value="filtered">选择全部筛选资料（跨页 {len(rows)} 款）</label><select name="target" required><option value="">选择目标渠道</option>{options(rot.TMALL | {"唯品"}, [], False)}</select><button class="primary">生成轮转草案</button></div><div class="rot-scroll"><table class="rot-table"><thead><tr><th>选择</th><th>款号／全部款色</th><th>供应商</th><th>品类</th><th>当前渠道</th><th>接收参考日期</th><th>期间净销量</th></tr></thead><tbody>{table or "<tr><td colspan=7>暂无符合条件的候选款；请先同步资料，或调整筛选条件。</td></tr>"}</tbody></table></div></form>'
        pages = []
        for p in sorted({1, max(1, page - 1), page, min(max(1, math.ceil(len(rows) / 50)), page + 1), max(1, math.ceil(len(rows) / 50))}):
            q = {**query, "page": p}
            pages.append(f'<a href="/rotation?{esc(urlencode(q, doseq=True))}">{"[" if p == page else ""}{p}{"]" if p == page else ""}</a>')
        body += '<nav class="rot-pages">' + ''.join(pages) + '</nav></section>'
        if excluded:
            body += '<section class="panel rot-panel"><details><summary>查看暂不参与的款式（最多显示 100 款）</summary><div class="rot-scroll"><table class="rot-table">' + ''.join(f'<tr><td>{esc(s["style_code"])}</td><td>{esc(s["reason"])}</td></tr>' for s in [s for s in styles if s["reason"]][:100]) + '</table></div></details></section>'
        body += '<section class="panel rot-panel"><h2>轮转方案 · 最近 100 份</h2><div class="rot-scroll"><table class="rot-table"><tr><th>方案</th><th>状态</th><th>整款数量</th><th>生成人员</th><th>创建时间</th></tr>' + ''.join(f'<tr><td><a href="/rotation/{p["id"]}">方案 #{p["id"]}</a></td><td>{rot.STATE_LABELS[p["state"]]}</td><td>{p["item_count"]} 款</td><td>{esc(p["actor"])}</td><td>{esc(p["created_at"])}</td></tr>' for p in plans) + '</table></div></section>'
        return self.rotation_frame(user, "货品轮转", body)

    def render_rotation_plan(self, user, plan):
        state, pid = plan["state"], plan["id"]
        body = f'<section class="panel rot-panel"><div class="rot-head"><h2>方案 #{pid} · {rot.STATE_LABELS[state]}</h2><a class="button" href="/rotation/{pid}/export.xlsx">导出Excel</a></div><p class="rot-note">本方案不会修改藏宝阁或上新审核中的价格、渠道、品类、流程。导出是可编辑执行清单；执行结果仅为人工反馈，不代表平台实际已上架。</p><details><summary>生成条件与记录</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{esc(plan["criteria"])}</pre></details><form method="post" action="/rotation/{pid}/action">{self.rotation_hidden(user)}<input type="hidden" name="plan_signature" value="{rot.plan_signature(plan)}"><div class="rot-scroll"><table class="rot-table"><tr><th>款号／全部款色</th><th>当前渠道</th><th>目标渠道</th><th>参考接收日期</th><th>操作／反馈</th></tr>'
        for item in plan["items"]:
            s = item["snapshot"]
            target = esc(item["target"])
            operation = "已移出草案" if not item["active"] else esc(item["executed_at"] or "待人工执行")
            if state == "draft" and item["active"]:
                target = f'<select name="target_{item["id"]}">' + ''.join(f'<option value="{esc(v)}" {"selected" if v == item["target"] else ""}>{esc(v)}</option>' for v in sorted(rot.CHANNELS) if rot.valid_target(s["channel"], v)) + '</select>'
                operation = f'<label><input type="checkbox" name="remove_{item["id"]}" value="1">移出草案（整款）</label>'
            elif state in {"confirmed", "executing"} and item["active"] and not item["executed_at"]:
                operation = f'<label><input type="checkbox" name="done_{item["id"]}" value="1">已人工执行整款</label>'
            body += f'<tr><td>{esc(item["style_code"])}<small>{esc("、".join(s["colors"]))}</small></td><td>{esc(s["channel"])}</td><td>{target}</td><td>{esc(rot.local_day(s["reference_at"]))}<small>{s["age_days"]} 自然日</small></td><td>{operation}<small>{esc(item["feedback"])}</small></td></tr>'
        body += '</table></div><div class="rot-tools" style="margin-top:20px">'
        if state == "draft":
            body += '<button name="action" value="save">保存草案修改</button>'
            if user["role"] == "admin":
                body += '<button class="primary" name="action" value="confirm">确认已保存方案</button>'
        if state == "confirmed":
            body += '<button name="action" value="start">标记开始执行</button>'
        if state in {"draft", "confirmed", "executing"}:
            body += '<button name="action" value="cancel" onclick="return confirm(\'取消只关闭方案，不会撤销平台已执行动作。是否继续？\')">取消方案</button>'
        body += '</div>'
        if state in {"confirmed", "executing"}:
            body += '<textarea class="rot-feedback" name="feedback" maxlength="1000" placeholder="填写执行人员、实际执行情况等说明"></textarea><button class="primary" name="action" value="feedback">保存所选款式人工执行反馈</button>'
        body += '</form></section><section class="panel rot-panel"><h2>操作记录（永久保留）</h2>' + ''.join(f'<p class="rot-note">{esc(e["created_at"])} · {esc(e["actor"])} · {esc(e["action"])}</p>' for e in plan["events"]) + '</section>'
        return self.rotation_frame(user, f"轮转方案 #{pid}", body)

    def render_rotation_rules(self, user):
        rules = [
            ("一、范围与权限", "一期只做天猫／天猫官／天猫奥与唯品之间跨体系轮转，及同款转出为一个单渠道；不做天猫体系内部互转。所有操作以款号为单位，全部款色一起处理。商品部可导入销量、生成和调整草案、人工反馈；企划管理员确认方案。"),
            ("二、接收参考时间", "只使用接收时保存的天猫体系(tmall)／唯品体系(vip)运营账号记录，不使用全渠道(all)和其他账号。先取有效接收日期再评估方向；同款跨两个体系及整款多个颜色均取最早日期。召回后全款只取最后一轮召回更新后的接收记录，旧颜色的早期记录不能恢复旧计时。没有新接收记录时显示召回更新后待接收，不进入推荐。"),
            ("三、自然日与筛选", "按上海时区的自然日计算，接收当天为第0天。支持70／90或自定天数，以及年份季节、供应商、品类、当前渠道批量筛选。可仅按时长、仅按销量或两者同时满足。接收日期不是实际开售日期。召回／未提交运营／停用或渠道不一致的整款暂不推荐。"),
            ("四、销量口径", "销售资料由人工先汇总为整款、体系、同一日期区间的净销量；天猫官和天猫奥计入天猫体系。缺失不等于零，不将唯品累计销量与天猫期间销量混算。同款必须两个体系在完全相同期间都有销量；总净销量用于低销量筛选，两边分项供人工决定退出方向。重复及重叠数据不叠加，只允许显式替换相同期间并永久保留旧批次。"),
            ("五、方案与人工执行", "跨页选择全部筛选资料生成草案；单款存在未结束方案时不重复推荐，改过款号也按来源ID防重。草案记录筛选条件、全部来源商品ID、款色、接收依据、销量批次及人员时间。调整后先保存，再由管理员确认；确认前重新同步核验，关键资料变化时拒绝确认。确认后导出可编辑Excel执行清单，商品部人工分配执行并反馈。本轮已经反馈执行的款式不再重复推荐，直到来源渠道、召回或发布轮次出现新的依据；这不代表平台已实际换渠道。取消不自动撤销已执行的动作。"),
            ("六、数据保护与后续", "轮转使用独立数据表和只读接口，不覆盖现有上新价格、上新渠道、品类或任何流程节点，不自动召回或回传藏宝阁。已有资料及方案历史永久保留。首次升级前先做SQLite一致性备份，失败即停止升级。第二阶段再单独设计轮转到藏宝阁的上新流程；本阶段不涉及库存等复杂因素。"),
        ]
        body = ''.join(f'<section class="panel rot-panel"><h2>{esc(title)}</h2><p style="line-height:1.9">{esc(text)}</p></section>' for title, text in rules)
        return self.rotation_frame(user, "货品轮转系统规则", body)
