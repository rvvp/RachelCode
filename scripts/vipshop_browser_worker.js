const fs = require("fs");
const os = require("os");
const path = require("path");
const { chromium } = require("playwright");

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function exactText(value) {
  const escaped = String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`^\\s*${escaped}\\s*$`);
}

async function checkedLabel(page, text) {
  const label = page.locator("label:visible").filter({ hasText: exactText(text) }).first();
  if (!(await label.count())) throw new Error(`唯品报表页缺少筛选项：${text}`);
  const input = label.locator("input").first();
  if (!(await input.isChecked())) await label.click();
}

async function setDateInput(input, value) {
  await input.evaluate((element, nextValue) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(element, nextValue);
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
    element.blur();
  }, value);
}

function newestFinishedReport(directories, range, startedAt) {
  const candidates = [];
  for (const directory of directories) {
    if (!directory || !fs.existsSync(directory)) continue;
    for (const name of fs.readdirSync(directory)) {
      if (!name.endsWith(".xlsx") || !name.includes(range)) continue;
      const filePath = path.join(directory, name);
      const stat = fs.statSync(filePath);
      if (stat.isFile() && stat.size > 0 && stat.mtimeMs >= startedAt - 2000) {
        candidates.push({ filePath, mtimeMs: stat.mtimeMs, size: stat.size });
      }
    }
  }
  return candidates.sort((left, right) => right.mtimeMs - left.mtimeMs)[0] || null;
}

async function receiveDownload(download, downloadDir, range, startedAt) {
  fs.mkdirSync(downloadDir, { recursive: true });
  const suggestedName = download.suggestedFilename();
  const target = path.join(downloadDir, suggestedName);
  try {
    await download.saveAs(target);
  } catch (_) {
    // Chrome CDP sessions may save directly to the profile's default download directory.
  }
  let previous = null;
  for (let attempt = 0; attempt < 90; attempt += 1) {
    const candidate = newestFinishedReport([downloadDir, path.join(os.homedir(), "Downloads")], range, startedAt);
    if (candidate && previous && candidate.filePath === previous.filePath && candidate.size === previous.size) {
      if (candidate.filePath !== target) fs.copyFileSync(candidate.filePath, target);
      return { filePath: target, fileName: path.basename(target), fileSize: fs.statSync(target).size };
    }
    previous = candidate;
    await sleep(1000);
  }
  throw new Error(`唯品报表已生成，但未在下载目录找到 ${range} 的完整文件。`);
}

async function exportProductDetail(context, page, command) {
  const startDate = String(command.startDate || "");
  const endDate = String(command.endDate || "");
  const brand = String(command.brand || "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(startDate) || !/^\d{4}-\d{2}-\d{2}$/.test(endDate)) {
    throw new Error("自动导出缺少正确的14天起止日期。");
  }
  if (command.url && page.url() !== command.url) {
    await page.goto(command.url, { waitUntil: "domcontentloaded", timeout: 30000 });
  }
  await page.bringToFront();
  await page.locator("button:visible").filter({ hasText: exactText("查询") }).first().waitFor({ timeout: 60000 });

  const brandInput = page.locator('input[placeholder="请选择品牌"]:visible').first();
  const selectedBrand = await brandInput.inputValue().catch(() => "");
  if (!selectedBrand || (brand && !selectedBrand.includes(brand))) {
    throw new Error(`唯品报表页未锁定目标品牌，当前为“${selectedBrand || "未选择"}”。`);
  }

  const dateInputs = page.locator("input.el-range-input:visible");
  if ((await dateInputs.count()) < 2) throw new Error("唯品报表页缺少统计日期输入框。");
  await setDateInput(dateInputs.nth(0), startDate);
  await setDateInput(dateInputs.nth(1), endDate);
  await page.keyboard.press("Escape").catch(() => {});
  await checkedLabel(page, "分天查看");
  await checkedLabel(page, "跨天不去重");
  await checkedLabel(page, "货号");
  await checkedLabel(page, "特卖会主站");

  const queryButton = page.locator("button:visible").filter({ hasText: exactText("查询") }).first();
  await queryButton.click({ timeout: 15000 });
  await page.locator(".el-loading-mask:visible").last().waitFor({ state: "hidden", timeout: 120000 }).catch(() => {});
  await sleep(1500);
  const actualDates = [await dateInputs.nth(0).inputValue(), await dateInputs.nth(1).inputValue()];
  if (actualDates[0] !== startDate || actualDates[1] !== endDate) {
    throw new Error(`唯品统计日期未生效，当前为 ${actualDates.join(" ~ ")}。`);
  }

  await page.locator("button:visible").filter({ hasText: exactText("下载数据") }).first().click();
  const dialog = page.locator(".el-dialog__wrapper:visible").filter({ hasText: "下载维度" }).first();
  await dialog.waitFor({ timeout: 15000 });
  await dialog.locator("label").filter({ hasText: exactText("条码粒度") }).first().click();
  const modalBrand = (await dialog.locator(".el-select__tags-text").allTextContents()).join(" ");
  if (brand && !modalBrand.includes(brand)) {
    throw new Error(`唯品下载窗口品牌不匹配，当前为“${modalBrand || "未选择"}”。`);
  }
  await dialog.locator("button").filter({ hasText: exactText("下载") }).first().click();
  await page.locator("text=报表生成中").first().waitFor({ timeout: 15000 }).catch(() => {});

  const existingDownloadPages = context.pages().filter((item) => item.url().includes("downloadCenter"));
  const newPagePromise = context.waitForEvent("page", { timeout: 10000 }).catch(() => null);
  await page.locator("button:visible").filter({ hasText: exactText("查看下载列表") }).first().click();
  const openedPage = await newPagePromise;
  const downloadPage = openedPage
    || context.pages().find((item) => item.url().includes("downloadCenter") && !existingDownloadPages.includes(item))
    || context.pages().find((item) => item.url().includes("downloadCenter"));
  if (!downloadPage) throw new Error("唯品下载中心未打开。");
  await downloadPage.waitForLoadState("domcontentloaded", { timeout: 30000 }).catch(() => {});
  await downloadPage.bringToFront();

  const range = `${startDate.replaceAll("-", "")}-${endDate.replaceAll("-", "")}`;
  let reportRow = null;
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const query = downloadPage.locator("button:visible").filter({ hasText: exactText("查询") }).first();
    await query.click().catch(() => {});
    await sleep(1500);
    const row = downloadPage.locator("tr").filter({ hasText: range }).filter({ hasText: brand }).first();
    if (await row.count()) {
      const rowText = await row.innerText();
      if (rowText.includes("失败")) throw new Error(`唯品报表生成失败：${rowText}`);
      const downloadButton = row.locator("button").filter({ hasText: exactText("下载") }).first();
      if (await downloadButton.count()) {
        reportRow = { rowText, downloadButton };
        break;
      }
    }
    await sleep(3500);
  }
  if (!reportRow) throw new Error(`唯品报表 ${range} 在5分钟内未生成完成。`);

  const startedAt = Date.now();
  const downloadPromise = downloadPage.waitForEvent("download", { timeout: 60000 });
  await reportRow.downloadButton.click();
  const download = await downloadPromise;
  const received = await receiveDownload(download, command.downloadDir, range, startedAt);
  return {
    action: "export_product_detail",
    startDate,
    endDate,
    brand: selectedBrand,
    reportRow: reportRow.rowText,
    ...received,
  };
}

async function main() {
  const command = JSON.parse(process.env.VIPSHOP_BROWSER_COMMAND || "{}");
  const endpoint = command.endpoint || "http://127.0.0.1:9223";
  const browser = await chromium.connectOverCDP(endpoint);
  const contexts = browser.contexts();
  const context = contexts[0];
  if (!context) throw new Error("专用浏览器没有可用的上下文");
  const officialHosts = new Set(["vis.vip.com", "compass.vip.com"]);
  const hostOf = (url) => {
    try { return new URL(url).hostname; } catch { return ""; }
  };
  let pages = context.pages();
  const requestedHost = hostOf(command.url || "");
  let page = pages.find((item) => command.url && item.url() === command.url)
    || pages.find((item) => requestedHost && hostOf(item.url()) === requestedHost)
    || pages.find((item) => officialHosts.has(hostOf(item.url())))
    || pages[pages.length - 1];
  if (!page) page = await context.newPage();

  if (command.downloadDir) {
    const session = await context.newCDPSession(page);
    await session.send("Browser.setDownloadBehavior", {
      behavior: "allow",
      downloadPath: command.downloadDir,
      eventsEnabled: true,
    });
    await session.detach();
  }
  let capture = null;
  if (command.action === "open" && command.url) {
    await page.bringToFront();
    await page.goto(command.url, { waitUntil: "domcontentloaded", timeout: 30000 });
  } else if (command.action === "export_product_detail") {
    capture = await exportProductDetail(context, page, command);
  }
  pages = context.pages();
  const pageRows = [];
  for (const item of pages) {
    pageRows.push({
      url: item.url(),
      title: await item.title().catch(() => ""),
      visible: await item.evaluate(() => document.visibilityState === "visible").catch(() => false),
      focused: await item.evaluate(() => document.hasFocus()).catch(() => false),
      target: item === page,
    });
  }
  const cookies = await context.cookies(["https://vis.vip.com/", "https://compass.vip.com/"]);
  const officialPages = pageRows.filter((item) => officialHosts.has(hostOf(item.url)));
  const current = (command.action === "open" && officialPages.find((item) => item.target))
    || officialPages.find((item) => item.focused)
    || officialPages.find((item) => item.visible)
    || officialPages[officialPages.length - 1]
    || pageRows[pageRows.length - 1]
    || { url: "", title: "" };
  const loginRequired = !officialHosts.has(hostOf(current.url)) || current.url.includes("/login.php") || current.url.includes("/login/");
  process.stdout.write(JSON.stringify({
    ok: true,
    current,
    pages: pageRows,
    vipCookieCount: cookies.length,
    loginRequired,
    sessionStatus: loginRequired ? "login_required" : "session_present",
    capture,
  }));
  await browser.close();
}

main().catch((error) => {
  process.stderr.write(error.stack || error.message || String(error));
  process.exit(1);
});
