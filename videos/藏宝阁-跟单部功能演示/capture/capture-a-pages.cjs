const { chromium } = require("playwright");
const { mkdir, writeFile } = require("node:fs/promises");
const path = require("node:path");

const baseUrl = "http://127.0.0.1:8765";
const outputDir = path.resolve("videos/藏宝阁-跟单部功能演示/capture/screenshots/a-pages");

const box = async (locator) => {
  if (!locator || !(await locator.count())) return null;
  const rect = await locator.first().boundingBox();
  if (!rect) return null;
  return Object.fromEntries(
    Object.entries(rect).map(([key, value]) => [key, Math.round(value * 10) / 10]),
  );
};

(async () => {
  await mkdir(outputDir, { recursive: true });

  const browser = await chromium.launch({
    headless: true,
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  const page = await browser.newPage({
    viewport: { width: 1920, height: 1080 },
    deviceScaleFactor: 1,
  });
  const metadata = {};

  await page.goto(`${baseUrl}/login`, { waitUntil: "networkidle" });
  await page.screenshot({ path: path.join(outputDir, "00-login-full.png"), fullPage: true });
  metadata.login = {
    pageHeight: await page.evaluate(() => document.documentElement.scrollHeight),
    loginPanel: await box(page.locator(".login-panel")),
    username: await box(page.locator('input[name="username"]')),
    loginButton: await box(page.locator('button[type="submit"]')),
  };

  await page.locator('input[name="username"]').fill("a_editor");
  await page.locator('input[name="password"]').fill("demo123");
  await Promise.all([
    page.waitForURL("**/modules"),
    page.locator('button[type="submit"]').click(),
  ]);

  const capture = async (key, name, url, targets = {}) => {
    await page.goto(`${baseUrl}${url}`, { waitUntil: "networkidle" });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: path.join(outputDir, name), fullPage: true });
    metadata[key] = {
      url,
      pageHeight: await page.evaluate(() => document.documentElement.scrollHeight),
      viewport: await page.evaluate(() => ({ width: window.innerWidth, height: window.innerHeight })),
      targets: {},
    };
    for (const [targetKey, locatorFactory] of Object.entries(targets)) {
      metadata[key].targets[targetKey] = await box(locatorFactory(page));
    }
  };

  await capture("modules", "01-a-home-full.png", "/modules", {
    productCard: (currentPage) => currentPage.getByRole("heading", { name: "商品资料后台" }).locator(".."),
    productEnter: (currentPage) => currentPage.getByRole("link", { name: "进入板块一" }),
    billingCard: (currentPage) => currentPage.getByRole("heading", { name: "账单与结算" }).locator(".."),
    billingEnter: (currentPage) => currentPage.getByRole("link", { name: "进入板块二" }),
  });

  await capture("products", "02-a-products-full.png", "/products", {
    workspaceCard: (currentPage) => currentPage.locator(".products-overview-card"),
    searchCard: (currentPage) => currentPage.locator("#products-search-filter"),
    overviewCard: (currentPage) => currentPage.locator(".products-stats-panel"),
    listCard: (currentPage) => currentPage.locator(".products-main-stack > section.panel"),
    newProduct: (currentPage) => currentPage.getByRole("link", { name: "新建资料" }),
    importExcel: (currentPage) => currentPage.getByRole("link", { name: "导入 Excel" }),
    exportExcel: (currentPage) => currentPage.locator(".export-menu").first(),
    bulkSubmit: (currentPage) => currentPage.getByRole("button", { name: "批量提交给商品部" }),
    firstCheckbox: (currentPage) => currentPage.locator('tbody input[name="product_ids"]').first(),
    firstView: (currentPage) => currentPage.locator('tbody a[href^="/products/"]').filter({ hasText: "查看" }).first(),
  });

  await page.goto(`${baseUrl}/products`, { waitUntil: "networkidle" });
  await page.evaluate(() => {
    window.scrollTo(0, 790);
    const tableWrap = document.querySelector(".products-list-scroll-wrap");
    if (tableWrap) tableWrap.scrollLeft = tableWrap.scrollWidth;
  });
  await page.waitForTimeout(250);
  await page.screenshot({ path: path.join(outputDir, "02-a-products-operations.png"), fullPage: false });
  metadata.productsOperations = {
    viewport: { width: 1920, height: 1080 },
    targets: {
      firstView: await box(page.locator('tbody a[href^="/products/"]').filter({ hasText: "查看" }).first()),
      firstEdit: await box(page.getByRole("link", { name: "编辑" }).first()),
      firstDelete: await box(page.getByRole("button", { name: "删除" }).first()),
      firstLog: await box(page.getByRole("link", { name: "日志" }).first()),
    },
  };

  await capture("newProduct", "03-a-new-product-full.png", "/products/new", {
    introCard: (currentPage) => currentPage.getByRole("heading", { name: "新建商品资料" }).locator(".."),
    reminderCard: (currentPage) => currentPage.getByRole("heading", { name: "填写提醒" }).locator(".."),
    formCard: (currentPage) => currentPage.getByRole("heading", { name: "资料填写区" }).locator(".."),
    basicSection: (currentPage) => currentPage.getByRole("heading", { name: "商品基础" }).locator(".."),
  });

  await page.goto(`${baseUrl}/products`, { waitUntil: "networkidle" });
  const detailHref = await page
    .locator('a[href^="/products/"]')
    .evaluateAll((links) => links.map((link) => link.getAttribute("href")).find((href) => /^\/products\/\d+$/.test(href || "")) || "");
  if (detailHref) {
    await capture("productDetail", "04-a-product-detail-full.png", detailHref, {
      detailCard: (currentPage) => currentPage.getByRole("heading", { name: "资料详情" }).locator(".."),
      status: (currentPage) => currentPage.getByText("当前状态", { exact: true }).first().locator(".."),
      version: (currentPage) => currentPage.getByText("修改版本", { exact: true }).first().locator(".."),
      days: (currentPage) => currentPage.getByText("历时天数", { exact: true }).first().locator(".."),
      log: (currentPage) => currentPage.getByRole("link", { name: "查看日志" }),
    });
  }

  await capture("billingHome", "05-a-billing-home-full.png", "/billing", {
    monthlyBoard: (currentPage) => currentPage.locator("#billing-monthly-board"),
    brandCard: (currentPage) => currentPage.getByRole("heading", { name: "品牌月账单" }).locator(".."),
    supplierCard: (currentPage) => currentPage.getByRole("heading", { name: "供应商结算" }).locator(".."),
    save: (currentPage) => currentPage.getByRole("button", { name: "保存" }),
  });

  await capture("brandBills", "06-a-brand-bills-full.png", "/billing/brand-bills", {
    billCard: (currentPage) => currentPage.locator(".brand-bills-primary-panel"),
    status: (currentPage) => currentPage.getByRole("heading", { name: "当前流程状态" }).locator(".."),
    currentBill: (currentPage) => currentPage.getByRole("heading", { name: /当月账单$/ }).locator(".."),
    history: (currentPage) => currentPage.getByRole("heading", { name: "历史账单查询" }).locator(".."),
    dashboard: (currentPage) => currentPage.locator(".brand-dashboard-panel"),
  });

  await capture("supplierSettlements", "07-a-supplier-settlements-full.png", "/billing/supplier-settlements", {
    settlementCard: (currentPage) => currentPage.getByRole("heading", { name: "供应商结算" }).locator(".."),
    billImport: (currentPage) => currentPage.getByRole("heading", { name: "账单导入" }).locator(".."),
    supplierManagement: (currentPage) => currentPage.getByRole("heading", { name: "供应商管理" }).locator(".."),
    billQuery: (currentPage) => currentPage.getByRole("heading", { name: "账单查询" }).locator(".."),
    querySummary: (currentPage) => currentPage.getByRole("heading", { name: "查询汇总" }).locator(".."),
  });

  await writeFile(path.join(outputDir, "capture-meta.json"), `${JSON.stringify(metadata, null, 2)}\n`);
  await browser.close();
})();
